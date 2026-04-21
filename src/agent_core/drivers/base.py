"""
drivers/base.py — Abstract base for all platform-specific agent drivers.

Design contract:
- All platform drivers must subclass BaseAgentDriver and implement invoke().
- The core package (orchestrator, repo_analyzer) interacts ONLY with
  BaseAgentDriver. No platform-specific code leaks into core logic.
- invoke() is async. Drivers must handle their own retry logic internally
  (use the tenacity helpers below) but expose a clean async interface.
- Drivers are STATELESS. They receive a SwarmContext, return a StructuredResult.
  Nothing is stored between calls.
"""

from __future__ import annotations

import abc
import ast
import json
import logging
import operator as _op
from typing import Any, Optional, final

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from agent_core.schemas import AgentSpec, StructuredResult, SwarmContext, TaskStatus

logger = logging.getLogger(__name__)


class DriverError(Exception):
    """Raised when a driver encounters an unrecoverable error."""


class RateLimitError(DriverError):
    """Raised on HTTP 429 / quota errors. Triggers exponential backoff retry."""


class MalformedResponseError(DriverError):
    """Raised when the platform returns a response that cannot be parsed into StructuredResult."""


class BaseAgentDriver(abc.ABC):
    """
    Abstract driver. One concrete subclass per AI platform.

    Subclasses must implement:
        _build_messages() — translates SwarmContext into platform message format
        _call_api()       — makes the HTTP call and returns raw response text
        _parse_response() — parses raw text into StructuredResult

    The public invoke() method handles retry, logging, and result validation.
    """

    def __init__(self, spec: AgentSpec, api_key: str, **kwargs: object) -> None:
        self.spec = spec
        self._api_key = api_key
        self._extra = kwargs

    @property
    def role(self) -> str:
        return str(self.spec.role)

    # ------------------------------------------------------------------
    # Public interface — called by the orchestrator
    # ------------------------------------------------------------------

    @final
    async def invoke(self, context: SwarmContext) -> StructuredResult:
        """
        Invoke the agent with the provided context. Returns a validated StructuredResult.
        Handles rate-limit retries with exponential backoff.
        """
        attempts = 0
        last_error: Optional[Exception] = None

        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(RateLimitError),
                stop=stop_after_attempt(self.spec.escalation.max_retries + 1),
                wait=wait_exponential(multiplier=1, min=2, max=60),
                reraise=True,
            ):
                with attempt:
                    attempts += 1
                    logger.debug("Driver %s attempt %d", self.role, attempts)
                    messages = self._build_messages(context)
                    raw = await self._call_api(messages, context)
                    result = self._parse_response(raw, context)
                    self._enforce_quality_gates(result)
                    return result
        except RateLimitError as exc:
            last_error = exc
            logger.warning("Rate limit exhausted for %s after %d attempt(s).", self.role, attempts)
        except (MalformedResponseError, DriverError) as exc:
            last_error = exc
            logger.error("Driver %s failed: %s", self.role, exc)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.error("Driver %s unexpected error: %s", self.role, exc)

        # All retries exhausted or unrecoverable error — return an escalation result
        return StructuredResult(
            task_id=context.task_id,
            role=self.role,
            status=TaskStatus.ESCALATED,
            summary=f"Driver failed after {attempts} attempt(s).",
            escalation_reason=str(last_error),
        )

    # ------------------------------------------------------------------
    # Abstract methods — must be implemented per platform
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def _build_messages(self, context: SwarmContext) -> list[dict]:
        """
        Translate SwarmContext into the platform's native message format.

        The system prompt MUST include:
        1. The agent's role description and responsibilities from AgentSpec
        2. The quality gates this agent must satisfy
        3. An explicit instruction to return ONLY valid JSON conforming to
           StructuredResult (or the custom output_json_schema if set)
        4. The token budget constraint from context.constraints

        The user message MUST include:
        1. The task description
        2. Serialised relevant_files (path + content)
        3. Serialised previous_outputs (summaries only, not full content)
        """

    @abc.abstractmethod
    async def _call_api(self, messages: list[dict], context: SwarmContext) -> str:
        """
        Make the platform API call. Return raw response text.
        Raise RateLimitError on 429. Raise DriverError on other failures.
        """

    @abc.abstractmethod
    def _parse_response(self, raw: str, context: SwarmContext) -> StructuredResult:
        """
        Parse raw platform response into a StructuredResult.
        Raise MalformedResponseError if parsing fails.
        """

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _build_system_prompt(self, context: SwarmContext) -> str:
        """
        Builds the platform-agnostic core of every system prompt.
        Platforms may prepend or append their own framing.
        """
        gates = "\n".join(f"- [ ] {g.description}" for g in self.spec.quality_gates)
        out_of_scope = "\n".join(f"- {s}" for s in self.spec.out_of_scope)
        token_budget = context.constraints.get("token_budget", "unset")

        return f"""You are the {self.spec.name} agent. {self.spec.description}

## Responsibilities
{chr(10).join(f"- {r}" for r in self.spec.responsibilities)}

## Quality gates
You must satisfy ALL of the following before returning output:
{gates}

## Out of scope
You must NOT do any of the following. If asked, return status=escalated.
{out_of_scope}

## Output format (MANDATORY)
Return ONLY valid JSON. No prose before or after the JSON block.
Your response must validate against the StructuredResult schema:
{{
  "task_id": string,
  "role": "{self.role}",
  "status": "done" | "failed" | "escalated",
  "summary": string (max 500 chars),
  "diffs": [...],
  "findings": [...],
  "suggested_commands": [...],
  "payload": {{}},
  "escalation_reason": string | null,
  "next_agent": string | null
}}

Token budget for this invocation: {token_budget} tokens.
Do not exceed it. Prefer shorter summaries over truncated diffs.
"""

    def _parse_json_result(self, raw: str, context: SwarmContext) -> StructuredResult:
        """
        Common JSON extraction used by most drivers.
        Strips markdown fences if present, then validates with Pydantic.
        """
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedResponseError(
                f"Could not parse JSON from {self.role} response: {exc}\n"
                f"Raw (first 500 chars): {raw[:500]}"
            ) from exc

        # Inject task_id and role if the model omitted them (common failure mode)
        data.setdefault("task_id", context.task_id)
        data.setdefault("role", self.role)

        try:
            return StructuredResult.model_validate(data)
        except Exception as exc:
            raise MalformedResponseError(
                f"StructuredResult validation failed for {self.role}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Safe expression evaluator (m-03 / L-03 — zero eval())
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_eval_gate(expr: str, result: StructuredResult) -> bool:
        """
        Evaluates a quality gate expression against a StructuredResult.

        Allowed operations:
          - Attribute access on `result` (e.g. result.status == 'done')
          - Comparisons: ==, !=, <, <=, >, >=, in, not in
          - Boolean logic: and, or, not
          - Integer and string literals
          - len() call on result attributes

        Execution is via a pure Python AST interpreter (_GateInterpreter).
        There is NO eval() call — no CPython interpreter dispatch at all.
        Any disallowed node type or dunder attribute access raises ValueError.
        """
        _ALLOWED_OPS = (
            ast.Expression,
            ast.BoolOp,
            ast.And,
            ast.Or,
            ast.UnaryOp,
            ast.Not,
            ast.Compare,
            ast.Eq,
            ast.NotEq,
            ast.Lt,
            ast.LtE,
            ast.Gt,
            ast.GtE,
            ast.In,
            ast.NotIn,
            ast.Attribute,
            ast.Name,
            ast.Constant,
            ast.Call,
            # Context nodes — AST bookkeeping, not operations
            ast.Load,
            ast.Store,
            ast.Del,
        )
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"Invalid gate expression syntax: {expr!r}") from exc

        # Belt: whitelist check before execution
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_OPS):
                raise ValueError(
                    f"Disallowed operation {type(node).__name__!r} in gate expression: {expr!r}. "
                    "Only comparisons and attribute access on 'result' are permitted."
                )
            if isinstance(node, ast.Call):
                if not (isinstance(node.func, ast.Name) and node.func.id == "len"):
                    raise ValueError(
                        f"Only len() calls are permitted in gate expressions, got: {expr!r}"
                    )
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                raise ValueError(
                    f"Dunder/private attribute access is not permitted: '.{node.attr}' in {expr!r}"
                )

        # Suspenders: pure AST interpreter — no eval(), no compile()
        _names = {"result": result, "len": len, "True": True, "False": False}
        try:
            return bool(_GateInterpreter(_names).visit(tree))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Gate expression evaluation failed: {exc}") from exc

    def _enforce_quality_gates(self, result: StructuredResult) -> None:
        """
        Mechanically evaluate quality gates that have eval_expr set.
        Failing a gate with strict mode enabled causes an escalation.
        """
        for gate in self.spec.quality_gates:
            if gate.eval_expr is None:
                continue
            try:
                passed = self._safe_eval_gate(gate.eval_expr, result)
            except ValueError as exc:
                logger.error("Gate expression rejected for %s: %s", self.role, exc)
                passed = False
            if not passed:
                logger.warning("Quality gate failed for %s: %s", self.role, gate.description)
                result.status = TaskStatus.ESCALATED
                result.escalation_reason = (
                    f"Quality gate failed: {gate.description} (eval_expr: {gate.eval_expr})"
                )
                break


class _GateInterpreter:
    """
    Minimal pure-Python AST interpreter for quality gate expressions.

    Executes ONLY the node types whitelisted by _safe_eval_gate's walker.
    No eval(), no compile(), no CPython interpreter dispatch.
    Called only after the whitelist check has already validated the AST.
    """

    _CMP_OPS: dict = {
        ast.Eq: _op.eq,
        ast.NotEq: _op.ne,
        ast.Lt: _op.lt,
        ast.LtE: _op.le,
        ast.Gt: _op.gt,
        ast.GtE: _op.ge,
        ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b,
    }

    def __init__(self, names: dict) -> None:
        self._names = names

    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise ValueError(f"Unhandled AST node in interpreter: {type(node).__name__!r}")
        return method(node)

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id not in self._names:
            raise ValueError(f"Unknown name {node.id!r} in gate expression")
        return self._names[node.id]

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        # Dunder guard is redundant here (whitelist check runs first) but kept
        # as defence-in-depth in case the interpreter is ever called directly.
        if node.attr.startswith("_"):
            raise ValueError(f"Dunder/private attribute access not permitted: '.{node.attr}'")
        return getattr(self.visit(node.value), node.attr)

    def visit_Call(self, node: ast.Call) -> Any:
        # Only len() is permitted; enforced by whitelist check before this runs
        args = [self.visit(a) for a in node.args]
        return len(*args)

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        result = True
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            op_fn = self._CMP_OPS.get(type(op))
            if op_fn is None:
                raise ValueError(f"Unsupported comparison operator: {type(op).__name__!r}")
            if not op_fn(left, right):
                result = False
                break
            left = right
        return result

    def visit_BoolOp(self, node: ast.BoolOp) -> bool:
        if isinstance(node.op, ast.And):
            # Short-circuit AND
            for value in node.values:
                if not self.visit(value):
                    return False
            return True
        if isinstance(node.op, ast.Or):
            # Short-circuit OR
            for value in node.values:
                if self.visit(value):
                    return True
            return False
        raise ValueError(f"Unsupported BoolOp: {type(node.op).__name__!r}")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        raise ValueError(f"Unsupported UnaryOp: {type(node.op).__name__!r}")
