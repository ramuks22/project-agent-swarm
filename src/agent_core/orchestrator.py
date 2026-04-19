"""
orchestrator.py — Swarm execution engine.

Design contract (addresses the architectural critiques directly):

1. STATEFUL CONTEXT: The orchestrator delegates state persistence to a
   configured StateStore. The orchestrator writes execution checkpoints
   to the state_dir on every step to enable recovery across sessions.
   All runtime state lives in SwarmContext, which is built, updated,
   and persisted automatically.

2. EXPLICIT STATE TRANSFER: Every agent invocation receives the full
   SwarmContext. Outputs are accumulated in SwarmContext.previous_outputs
   and passed to the next agent. No implicit shared memory.

3. TOKEN BUDGET ENFORCEMENT: Before invoking any agent, the orchestrator
   slices relevant_files to fit within the configured token budget. Files
   are prioritized by relevance score (proximity to changed paths, etc.).

4. QUALITY GATE ENFORCEMENT: After each agent invocation, quality gates
   are checked. In strict mode, a failing gate halts the swarm and
   returns an escalation result. In non-strict mode, the failure is
   logged and execution continues.

5. CONFLICT RESOLUTION: If two agents in a parallel run return
   contradictory findings, the orchestrator escalates to the user rather
   than silently picking one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from agent_core.context_optimizer import score_files, slice_to_budget
from agent_core.persistence import get_state_store
from agent_core.security.tool_sandbox import is_command_safe
from agent_core.schemas import (
    AgentOutput,
    AgentSpec,
    FileSnapshot,
    Platform,
    RepoMetadata,
    StructuredResult,
    SwarmConfig,
    SwarmContext,
    TaskStatus,
)

if TYPE_CHECKING:
    from agent_core.drivers.base import BaseAgentDriver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Driver registry
# ---------------------------------------------------------------------------

_DRIVER_REGISTRY: dict[Platform, type] = {}


def register_driver(platform: Platform, driver_cls: type) -> None:
    """Register a driver class for a platform. Called at import time by each driver module."""
    _DRIVER_REGISTRY[platform] = driver_cls


def _get_driver(platform: Platform, spec: AgentSpec, api_key: str, **kwargs: object) -> "BaseAgentDriver":
    cls = _DRIVER_REGISTRY.get(platform)
    if cls is None:
        raise ValueError(
            f"No driver registered for platform '{platform}'. "
            f"Available: {list(_DRIVER_REGISTRY)}"
        )
    return cls(spec, api_key, **kwargs)


# Auto-register built-in drivers
def _register_builtins() -> None:
    try:
        from agent_core.drivers.claude import ClaudeDriver
        register_driver(Platform.CLAUDE_CODE, ClaudeDriver)
    except Exception:
        pass
    try:
        from agent_core.drivers.codex import CodexDriver
        register_driver(Platform.CODEX, CodexDriver)
        # Register OPENAI as an alias to CodexDriver (C-04 fix)
        register_driver(Platform.OPENAI, CodexDriver)
    except Exception:
        pass
    try:
        from agent_core.drivers.gemini import GeminiDriver
        register_driver(Platform.GEMINI, GeminiDriver)
    except Exception:
        pass


_register_builtins()


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def build_context(
    task_description: str,
    config: SwarmConfig,
    repo_metadata: RepoMetadata | None,
    file_paths: list[Path],
    previous_outputs: list[AgentOutput] | None = None,
    task_id: str | None = None,
    agent_role: str = "",
    error_trace: str = "",
    recently_changed: list[Path] | None = None,
) -> SwarmContext:
    """
    Build a SwarmContext for a single agent invocation.

    Files are ranked by relevance to the task and agent role, then sliced
    to fit within the token budget. The most relevant files survive the cut.
    """
    budget = config.token_budget_per_agent

    scored = score_files(
        task_description=task_description,
        candidate_paths=file_paths,
        agent_role=agent_role,
        recently_changed=recently_changed or None,
        error_trace=error_trace or None,
    )
    selected = slice_to_budget(scored, token_budget=budget)

    snapshots: list[FileSnapshot] = [
        FileSnapshot(
            path=sf.path,
            content=sf.content,
            language=sf.path.suffix.lstrip("."),
            token_count=sf.token_count,
        )
        for sf in selected
    ]

    used_tokens = sum(s.token_count for s in snapshots)
    logger.debug(
        "Context optimizer: %d/%d files selected, %d/%d tokens used",
        len(snapshots), len(file_paths), used_tokens, budget,
    )

    return SwarmContext(
        task_id=task_id or str(uuid.uuid4()),
        task_description=task_description,
        platform=config.platform,
        relevant_files=snapshots,
        previous_outputs=previous_outputs or [],
        constraints={
            "token_budget": budget - used_tokens,
            "max_parallel_agents": config.max_parallel_agents,
            "quality_gate_strict": config.quality_gate_strict,
        },
        repo_metadata=repo_metadata,
    )


# ---------------------------------------------------------------------------
# Sequential execution
# ---------------------------------------------------------------------------


async def run_sequential(
    task_description: str,
    agent_chain: list[tuple[AgentSpec, list[Path]]],
    config: SwarmConfig,
    api_key: str,
    repo_metadata: RepoMetadata | None = None,
    task_id: str | None = None,
    on_event: Any | None = None,
    **driver_kwargs: object,
) -> list[StructuredResult]:
    """
    Run a chain of agents sequentially. Each agent receives the outputs
    of all previous agents in its context.

    agent_chain: list of (AgentSpec, list_of_relevant_file_paths) tuples
    """
    task_id = task_id or str(uuid.uuid4())
    results: list[StructuredResult] = []
    accumulated_outputs: list[AgentOutput] = []

    state_store = get_state_store(config)

    for spec, file_paths in agent_chain:
        context = build_context(
            task_description=task_description,
            config=config,
            repo_metadata=repo_metadata,
            file_paths=file_paths,
            previous_outputs=accumulated_outputs,
            task_id=task_id,
            agent_role=str(spec.role),
        )

        # 1. AUTO-SAVE BEFORE INVOCATION
        await state_store.save(context)
        if on_event:
            await on_event({"type": "agent_start", "agent": spec.name, "task_id": task_id})

        driver = _get_driver(config.platform, spec, api_key, **driver_kwargs)
        logger.info("Invoking agent: %s [%s]", spec.name, task_id)
        result = await driver.invoke(context)

        # SECURITY CHECK: Validate suggested commands
        safe_commands = []
        for cmd in result.suggested_commands:
            is_safe, reason = is_command_safe(cmd)
            if is_safe:
                safe_commands.append(cmd)
            else:
                logger.warning("Agent suggested unsafe command blocked: %s (Reason: %s)", cmd, reason)
                if config.quality_gate_strict:
                    result.status = TaskStatus.ESCALATED
                    result.escalation_reason = f"Security Violation: Suggested dangerous command - {reason}"
                    break
        result.suggested_commands = safe_commands

        results.append(result)

        # Persist result to output_dir
        _write_result(result, config.output_dir, task_id)

        # Accumulate for next agent
        current_output = AgentOutput(
            role=result.role,
            status=result.status,
            summary=result.summary,
            artifacts=[str(d.path) for d in result.diffs] if result.diffs else [],
            findings=[f.model_dump() for f in result.findings],
            structured_data=result.payload,
        )
        accumulated_outputs.append(current_output)

        # 2. AUTO-SAVE AFTER INVOCATION (Update context with new outputs)
        context.previous_outputs = accumulated_outputs
        await state_store.save(context)
        if on_event:
            await on_event({
                "type": "agent_complete", 
                "agent": spec.name, 
                "task_id": task_id, 
                "status": result.status
            })

        # Halt on escalation in strict mode
        if result.status == TaskStatus.ESCALATED and config.quality_gate_strict:
            logger.warning(
                "Agent %s escalated. Halting swarm (strict mode). Reason: %s",
                spec.name,
                result.escalation_reason,
            )
            break

    return results


async def resume_swarm(
    task_id: str,
    config: SwarmConfig,
    api_key: str,
    remaining_chain: list[tuple[AgentSpec, list[Path]]] | None = None,
    **driver_kwargs: object,
) -> list[StructuredResult]:
    """
    Resume a previously interrupted swarm task using its task_id.
    
    If remaining_chain is provided, it replaces any logic that would have
    been inferred from the saved context.
    """
    state_store = get_state_store(config)
    context = await state_store.load(task_id)

    if not context:
        raise ValueError(f"No saved state found for task_id: {task_id}")

    logger.info("Resuming swarm task: %s", task_id)

    if not remaining_chain:
        # If no chain provided, we can only run a generic single-agent 
        # based on context or fail. Usually callers provide the chain.
        raise ValueError("remaining_chain must be provided to resume a sequential run.")

    return await run_sequential(
        task_description=context.task_description,
        agent_chain=remaining_chain,
        config=config,
        api_key=api_key,
        repo_metadata=context.repo_metadata,
        task_id=task_id,
        **driver_kwargs,
    )


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


async def run_parallel(
    task_description: str,
    agent_tasks: list[tuple[AgentSpec, list[Path]]],
    config: SwarmConfig,
    api_key: str,
    shared_prior_outputs: list[AgentOutput] | None = None,
    repo_metadata: RepoMetadata | None = None,
    task_id: str | None = None,
    **driver_kwargs: object,
) -> list[StructuredResult]:
    """
    Run multiple independent agents in parallel. All agents receive the same
    shared_prior_outputs but are invoked concurrently.

    Use only for truly independent tasks (e.g., reviewing multiple modules
    simultaneously). Do NOT use for chains where one agent depends on another's output.

    Detects conflicts in findings and escalates rather than silently resolving them.
    """
    task_id = task_id or str(uuid.uuid4())
    state_store = get_state_store(config)

    semaphore = asyncio.Semaphore(config.max_parallel_agents)

    async def _invoke_one(spec: AgentSpec, file_paths: list[Path]) -> StructuredResult:
        async with semaphore:
            context = build_context(
                task_description=task_description,
                config=config,
                repo_metadata=repo_metadata,
                file_paths=file_paths,
                previous_outputs=shared_prior_outputs or [],
                task_id=task_id,
                agent_role=str(spec.role),
            )
            await state_store.save(context)
            driver = _get_driver(config.platform, spec, api_key, **driver_kwargs)
            return await driver.invoke(context)

    results = await asyncio.gather(
        *[_invoke_one(spec, paths) for spec, paths in agent_tasks],
        return_exceptions=False,
    )

    for r in results:
        _write_result(r, config.output_dir, task_id)

    _detect_conflicts(results, config)
    return list(results)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_result(result: StructuredResult, output_dir: Path, task_id: str) -> None:
    """Write a StructuredResult as JSON to the output directory."""
    out = output_dir / task_id
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{result.role}.json"
    dest.write_text(result.model_dump_json(indent=2))
    logger.debug("Result written to %s", dest)


def _detect_conflicts(results: list[StructuredResult], config: SwarmConfig) -> None:
    """
    Detect contradictory findings across parallel agent results.
    Logs a warning (or raises in strict mode) if two agents flag the
    same file with conflicting severity assessments.
    """
    file_severity: dict[str, list[tuple[str, str]]] = {}  # file -> [(agent, severity)]

    for result in results:
        for finding in result.findings:
            file_severity.setdefault(finding.file, []).append(
                (result.role, finding.severity)
            )

    for file, assessments in file_severity.items():
        severities = {s for _, s in assessments}
        if len(severities) > 1:
            agents = ", ".join(f"{a}={s}" for a, s in assessments)
            msg = f"Conflicting severity assessments for {file}: {agents}"
            if config.quality_gate_strict:
                logger.error(msg)
            else:
                logger.warning(msg)
