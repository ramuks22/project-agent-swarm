"""test_drivers.py — Tests for BaseAgentDriver mechanics."""

from __future__ import annotations

import json

from agent_core.drivers.base import (
    BaseAgentDriver,
    RateLimitError,
)
from agent_core.schemas import (
    AgentSpec,
    EscalationPolicy,
    QualityGate,
    StructuredResult,
    SwarmContext,
    TaskStatus,
    ToolPermission,
)

# ---------------------------------------------------------------------------
# Concrete test driver (minimal subclass)
# ---------------------------------------------------------------------------


class EchoDriver(BaseAgentDriver):
    """Test driver that returns a preset response."""

    def __init__(self, spec: AgentSpec, raw_response: str = "", **kwargs: object) -> None:
        super().__init__(spec, api_key="test-key", **kwargs)
        self._raw_response = raw_response
        self.call_count = 0

    def _build_messages(self, context: SwarmContext) -> list[dict]:
        return [{"role": "user", "content": context.task_description}]

    async def _call_api(self, messages: list[dict], context: SwarmContext) -> str:
        self.call_count += 1
        return self._raw_response

    def _parse_response(self, raw: str, context: SwarmContext) -> StructuredResult:
        return self._parse_json_result(raw, context)


class RateLimitDriver(BaseAgentDriver):
    """Driver that raises RateLimitError on first N calls, then succeeds."""

    def __init__(self, spec: AgentSpec, fail_times: int = 1) -> None:
        super().__init__(spec, api_key="test-key")
        self._fail_times = fail_times
        self._call_count = 0

    def _build_messages(self, context: SwarmContext) -> list[dict]:
        return []

    async def _call_api(self, messages: list[dict], context: SwarmContext) -> str:
        self._call_count += 1
        if self._call_count <= self._fail_times:
            raise RateLimitError("429 Too Many Requests")
        return '{"task_id":"t1","role":"implementer","status":"done","summary":"ok"}'

    def _parse_response(self, raw: str, context: SwarmContext) -> StructuredResult:
        return self._parse_json_result(raw, context)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEchoDriver:
    def _make_valid_json(self, task_id: str = "t1") -> str:
        return json.dumps(
            {
                "task_id": task_id,
                "role": "implementer",
                "status": "done",
                "summary": "All done.",
            }
        )

    async def test_successful_invoke(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        driver = EchoDriver(minimal_spec, raw_response=self._make_valid_json())
        result = await driver.invoke(minimal_context)
        assert result.status == TaskStatus.DONE
        assert result.summary == "All done."

    async def test_injects_task_id_when_missing(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        # Response omits task_id — driver should inject it
        raw = '{"role":"implementer","status":"done","summary":"ok"}'
        driver = EchoDriver(minimal_spec, raw_response=raw)
        result = await driver.invoke(minimal_context)
        assert result.task_id == minimal_context.task_id

    async def test_injects_role_when_missing(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        raw = '{"task_id":"t1","status":"done","summary":"ok"}'
        driver = EchoDriver(minimal_spec, raw_response=raw)
        result = await driver.invoke(minimal_context)
        assert result.role == str(minimal_spec.role)

    async def test_strips_markdown_fences(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        fenced = "```json\n" + self._make_valid_json() + "\n```"
        driver = EchoDriver(minimal_spec, raw_response=fenced)
        result = await driver.invoke(minimal_context)
        assert result.status == TaskStatus.DONE

    async def test_malformed_json_escalates(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        driver = EchoDriver(minimal_spec, raw_response="not json at all")
        result = await driver.invoke(minimal_context)
        assert result.status == TaskStatus.ESCALATED
        assert result.escalation_reason is not None

    async def test_escalated_without_reason_escalates(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        # Model returns escalated but omits escalation_reason — validation should catch it
        raw = '{"task_id":"t1","role":"implementer","status":"escalated","summary":"stuck"}'
        driver = EchoDriver(minimal_spec, raw_response=raw)
        result = await driver.invoke(minimal_context)
        # Should escalate because Pydantic validation failed
        assert result.status == TaskStatus.ESCALATED


class TestQualityGateEnforcement:
    async def test_gate_with_eval_expr_passes(self, minimal_context: SwarmContext) -> None:
        spec = AgentSpec(
            name="test",
            role="implementer",
            description="x",
            responsibilities=["x"],
            quality_gates=[
                QualityGate(
                    description="Must have diffs.",
                    eval_expr="len(result.diffs) > 0",
                )
            ],
            tools_allowed=[ToolPermission(name="Read")],
            out_of_scope=[],
        )
        raw = (
            '{"task_id":"t1","role":"implementer","status":"done","summary":"done",'
            '"diffs":[{"path":"src/a.py","operation":"modify","unified_diff":"---","explanation":"x"}]}'
        )
        driver = EchoDriver(spec, raw_response=raw)
        result = await driver.invoke(minimal_context)
        assert result.status == TaskStatus.DONE

    async def test_gate_with_eval_expr_fails_escalates(self, minimal_context: SwarmContext) -> None:
        spec = AgentSpec(
            name="test",
            role="implementer",
            description="x",
            responsibilities=["x"],
            quality_gates=[
                QualityGate(
                    description="Must have diffs.",
                    eval_expr="len(result.diffs) > 0",
                )
            ],
            tools_allowed=[ToolPermission(name="Read")],
            out_of_scope=[],
        )
        raw = '{"task_id":"t1","role":"implementer","status":"done","summary":"done"}'
        driver = EchoDriver(spec, raw_response=raw)
        result = await driver.invoke(minimal_context)
        assert result.status == TaskStatus.ESCALATED
        assert "Quality gate failed" in (result.escalation_reason or "")


class TestRateLimitRetry:
    async def test_retries_on_rate_limit_and_succeeds(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        driver = RateLimitDriver(minimal_spec, fail_times=1)
        result = await driver.invoke(minimal_context)
        assert result.status == TaskStatus.DONE
        assert driver._call_count == 2

    async def test_escalates_after_max_retries(self, minimal_context: SwarmContext) -> None:
        spec = AgentSpec(
            name="test",
            role="implementer",
            description="x",
            responsibilities=["x"],
            quality_gates=[QualityGate(description="x")],
            tools_allowed=[],
            out_of_scope=[],
            escalation=EscalationPolicy(max_retries=1),
        )
        # Fails more times than max_retries allows
        driver = RateLimitDriver(spec, fail_times=10)
        result = await driver.invoke(minimal_context)
        assert result.status == TaskStatus.ESCALATED


class TestBuildSystemPrompt:
    def test_system_prompt_includes_role_name(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        driver = EchoDriver(minimal_spec)
        prompt = driver._build_system_prompt(minimal_context)
        assert minimal_spec.name in prompt

    def test_system_prompt_includes_quality_gates(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        driver = EchoDriver(minimal_spec)
        prompt = driver._build_system_prompt(minimal_context)
        assert "Thing was done." in prompt

    def test_system_prompt_includes_out_of_scope(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        driver = EchoDriver(minimal_spec)
        prompt = driver._build_system_prompt(minimal_context)
        assert "Nothing else." in prompt

    def test_system_prompt_includes_token_budget(
        self, minimal_spec: AgentSpec, minimal_context: SwarmContext
    ) -> None:
        driver = EchoDriver(minimal_spec)
        prompt = driver._build_system_prompt(minimal_context)
        assert "8000" in prompt  # from minimal_context constraints
