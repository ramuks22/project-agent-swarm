"""
orchestrator.py — Swarm execution engine.

This module supports two execution modes:
1. Legacy fixed chains (`run_sequential`, `run_parallel`)
2. Autonomous coordinator flow (`run_autonomous`, `resume_autonomous`)

The orchestrator itself remains a coordinator. It manages plans, gates,
specialist routing, retries, and synthesis, while optional workspace mutation
is delegated to the executor layer behind `execute=true`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent_core.context_optimizer import (
    get_eligible_candidates,
    pass_1_metadata_score,
    pass_2_content_refinement,
    slice_to_budget,
)
from agent_core.executor import AutonomousExecutor
from agent_core.persistence import get_state_store
from agent_core.schemas import (
    AgentOutput,
    AgentRole,
    AgentSpec,
    ApprovalMode,
    AutonomousFlow,
    ClarificationQuestion,
    ExecutionPlan,
    FileSnapshot,
    GateDecision,
    GateRecord,
    GateStatus,
    GateType,
    PlanStep,
    Platform,
    RepoMetadata,
    RunPhase,
    Severity,
    StructuredResult,
    SwarmConfig,
    SwarmContext,
    SwarmRunState,
    TaskStatus,
)
from agent_core.security.tool_sandbox import is_command_safe

if TYPE_CHECKING:
    from agent_core.drivers.base import BaseAgentDriver

logger = logging.getLogger(__name__)


class ParallelConflictError(RuntimeError):
    """Raised when strict parallel execution produces contradictory findings."""


# ---------------------------------------------------------------------------
# Driver registry
# ---------------------------------------------------------------------------

_DRIVER_REGISTRY: dict[Platform, type[BaseAgentDriver]] = {}


def register_driver(platform: Platform, driver_cls: type[BaseAgentDriver]) -> None:
    """Register a driver class for a platform. Called at import time by each driver module."""
    _DRIVER_REGISTRY[platform] = driver_cls


def _get_driver(
    platform: Platform, spec: AgentSpec, api_key: str, **kwargs: object
) -> BaseAgentDriver:
    cls = _DRIVER_REGISTRY.get(platform)
    if cls is None:
        raise ValueError(
            f"No driver registered for platform '{platform}'. Available: {list(_DRIVER_REGISTRY)}"
        )
    return cls(spec, api_key, **kwargs)


def _register_builtins() -> None:
    try:
        from agent_core.drivers.claude import ClaudeDriver

        register_driver(Platform.CLAUDE_CODE, ClaudeDriver)
    except Exception:  # noqa: S110
        pass
    try:
        from agent_core.drivers.codex import CodexDriver

        register_driver(Platform.CODEX, CodexDriver)
        register_driver(Platform.OPENAI, CodexDriver)
    except Exception:  # noqa: S110
        pass
    try:
        from agent_core.drivers.gemini import GeminiDriver

        register_driver(Platform.GEMINI, GeminiDriver)
    except Exception:  # noqa: S110
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

    scored_p1 = pass_1_metadata_score(
        task_description=task_description,
        candidate_paths=file_paths,
        agent_role=agent_role,
        recently_changed=recently_changed or None,
    )

    scored_p2 = pass_2_content_refinement(
        candidates=scored_p1,
        task_description=task_description,
        error_trace=error_trace or None,
        max_reads=25,
        preview_bytes=8192,
    )

    selected = slice_to_budget(scored_p2, token_budget=budget)

    snapshots: list[FileSnapshot] = [
        FileSnapshot(
            path=sf.path,
            content=sf.content or "",
            language=sf.path.suffix.lstrip("."),
            token_count=sf.token_count,
        )
        for sf in selected
    ]

    used_tokens = sum(s.token_count for s in snapshots)
    logger.debug(
        "Context optimizer: %d/%d files selected, %d/%d tokens used",
        len(snapshots),
        len(file_paths),
        used_tokens,
        budget,
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
# Shared invocation helpers
# ---------------------------------------------------------------------------


def _write_result(result: StructuredResult, output_dir: Path, task_id: str) -> None:
    """Write a StructuredResult as JSON to the output directory."""
    out = output_dir / task_id
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{result.role}.json"
    dest.write_text(result.model_dump_json(indent=2))
    logger.debug("Result written to %s", dest)


def _result_to_agent_output(result: StructuredResult) -> AgentOutput:
    return AgentOutput(
        role=result.role,
        status=result.status,
        summary=result.summary,
        artifacts=[str(d.path) for d in result.diffs] if result.diffs else [],
        findings=[f.model_dump() for f in result.findings],
        structured_data=result.payload,
    )


def _apply_security_filter(result: StructuredResult, config: SwarmConfig) -> StructuredResult:
    safe_commands = []
    for cmd in result.suggested_commands:
        is_safe, reason = is_command_safe(cmd)
        if is_safe:
            safe_commands.append(cmd)
            continue

        logger.warning("Agent suggested unsafe command blocked: %s (Reason: %s)", cmd, reason)
        if config.quality_gate_strict:
            result.status = TaskStatus.ESCALATED
            result.escalation_reason = (
                f"Security Violation: Suggested dangerous command - {reason}"
            )
            break

    result.suggested_commands = safe_commands
    return result


async def _invoke_agent(
    *,
    spec: AgentSpec,
    task_description: str,
    file_paths: list[Path],
    config: SwarmConfig,
    api_key: str,
    state_store: Any,
    repo_metadata: RepoMetadata | None = None,
    previous_outputs: list[AgentOutput] | None = None,
    task_id: str | None = None,
    agent_role: str = "",
    error_trace: str = "",
    recently_changed: list[Path] | None = None,
    on_event: Any | None = None,
    **driver_kwargs: object,
) -> tuple[StructuredResult, SwarmContext]:
    context = build_context(
        task_description=task_description,
        config=config,
        repo_metadata=repo_metadata,
        file_paths=file_paths,
        previous_outputs=previous_outputs,
        task_id=task_id,
        agent_role=agent_role or str(spec.role),
        error_trace=error_trace,
        recently_changed=recently_changed,
    )

    await state_store.save(context)
    if on_event:
        await on_event({"type": "agent_start", "agent": spec.name, "task_id": context.task_id})

    driver = _get_driver(config.platform, spec, api_key, **driver_kwargs)
    logger.info("Invoking agent: %s [%s]", spec.name, context.task_id)
    result = await driver.invoke(context)
    result = _apply_security_filter(result, config)

    context.previous_outputs = (previous_outputs or []) + [_result_to_agent_output(result)]
    await state_store.save(context)

    if on_event:
        await on_event(
            {
                "type": "agent_complete",
                "agent": spec.name,
                "task_id": context.task_id,
                "status": result.status,
            }
        )

    return result, context


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
    extra_driver_kwargs = cast(dict[str, Any], driver_kwargs)

    for spec, file_paths in agent_chain:
        result, _ = await _invoke_agent(
            spec=spec,
            task_description=task_description,
            file_paths=file_paths,
            config=config,
            api_key=api_key,
            state_store=state_store,
            repo_metadata=repo_metadata,
            previous_outputs=accumulated_outputs,
            task_id=task_id,
            agent_role=str(spec.role),
            on_event=on_event,
            **extra_driver_kwargs,
        )

        results.append(result)
        _write_result(result, config.output_dir, task_id)
        accumulated_outputs.append(_result_to_agent_output(result))

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
    extra_driver_kwargs = cast(dict[str, Any], driver_kwargs)

    async def _invoke_one(spec: AgentSpec, file_paths: list[Path]) -> StructuredResult:
        async with semaphore:
            result, _ = await _invoke_agent(
                spec=spec,
                task_description=task_description,
                file_paths=file_paths,
                config=config,
                api_key=api_key,
                state_store=state_store,
                repo_metadata=repo_metadata,
                previous_outputs=shared_prior_outputs or [],
                task_id=task_id,
                agent_role=str(spec.role),
                **extra_driver_kwargs,
            )
            return result

    results = await asyncio.gather(
        *[_invoke_one(spec, paths) for spec, paths in agent_tasks],
        return_exceptions=False,
    )

    for result in results:
        _write_result(result, config.output_dir, task_id)

    _detect_conflicts(results, config)
    return list(results)


def _detect_conflicts(results: list[StructuredResult], config: SwarmConfig) -> None:
    """
    Detect contradictory findings across parallel agent results.

    In strict mode, conflicting severities on the same file are raised as a
    hard orchestration error rather than being logged and ignored.
    """
    file_severity: dict[str, list[tuple[str, str]]] = {}

    for result in results:
        for finding in result.findings:
            file_severity.setdefault(finding.file, []).append((result.role, finding.severity))

    conflicts: list[str] = []
    for file, assessments in file_severity.items():
        severities = {severity for _, severity in assessments}
        if len(severities) <= 1:
            continue

        agents = ", ".join(f"{agent}={severity}" for agent, severity in assessments)
        conflicts.append(f"{file}: {agents}")

    if not conflicts:
        return

    msg = "Conflicting severity assessments detected: " + " | ".join(conflicts)
    if config.quality_gate_strict:
        logger.error(msg)
        raise ParallelConflictError(msg)

    logger.warning(msg)


# ---------------------------------------------------------------------------
# Autonomous orchestration
# ---------------------------------------------------------------------------


def _normalize_task(task_description: str) -> str:
    return " ".join(task_description.strip().split())


def _extract_bullets(task_description: str) -> list[str]:
    bullets = []
    for line in task_description.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            bullets.append(stripped[2:].strip())
    return bullets


def _determine_autonomous_flow(task_description: str) -> AutonomousFlow:
    normalized = _normalize_task(task_description).lower()

    if any(term in normalized for term in ("review", "audit", "inspect")):
        return AutonomousFlow.REVIEW_ONLY
    if any(term in normalized for term in ("bug", "fix", "broken", "error", "failure", "debug")):
        return AutonomousFlow.BUGFIX
    if any(term in normalized for term in ("test", "coverage", "assertion", "verify")):
        return AutonomousFlow.TEST_GENERATION
    return AutonomousFlow.FEATURE


def _generate_clarification_questions(
    task_description: str, flow: AutonomousFlow
) -> list[ClarificationQuestion]:
    normalized = _normalize_task(task_description)
    lowered = normalized.lower()
    questions: list[ClarificationQuestion] = []

    if len(normalized.split()) < 5:
        questions.append(
            ClarificationQuestion(
                id="goal",
                prompt="What exact user-visible or developer-visible outcome should this work produce?",
                rationale="The task description is too short to safely lock requirements.",
            )
        )

    if flow == AutonomousFlow.FEATURE and not _extract_bullets(task_description):
        target_markers = ("endpoint", "api", "ui", "button", "field", "class", "function", "file")
        if not any(marker in lowered for marker in target_markers):
            questions.append(
                ClarificationQuestion(
                    id="feature-scope",
                    prompt="Which concrete surface should change for this feature, and what are the acceptance criteria?",
                    rationale="Feature work needs a target surface and lockable acceptance criteria.",
                )
            )

    ambiguous_terms = ("this", "that", "it", "something", "stuff")
    if any(term in lowered.split() for term in ambiguous_terms) and not _extract_bullets(task_description):
        questions.append(
            ClarificationQuestion(
                id="references",
                prompt="What specific file, module, endpoint, or workflow does the task refer to?",
                rationale="The request uses ambiguous references that could fan out into the wrong scope.",
            )
        )

    return questions[:3]


def _default_acceptance_criteria(flow: AutonomousFlow) -> list[str]:
    if flow == AutonomousFlow.BUGFIX:
        return [
            "The root cause is identified and fixed.",
            "Regression coverage or equivalent verification exists.",
            "Review finds no remaining blocker issues.",
        ]
    if flow == AutonomousFlow.REVIEW_ONLY:
        return [
            "The relevant code paths are reviewed in full.",
            "Findings are severity-labelled and actionable.",
            "The final summary is coherent and actionable.",
        ]
    if flow == AutonomousFlow.TEST_GENERATION:
        return [
            "Tests cover the requested behavior and edge cases.",
            "Tests align with the current implementation and repo conventions.",
            "Review finds no blocker issues in the added tests.",
        ]
    return [
        "The requested behavior is implemented end-to-end.",
        "Relevant verification passes or clear failures are surfaced.",
        "Review finds no remaining blocker issues.",
    ]


def _phase_sequence(flow: AutonomousFlow) -> list[RunPhase]:
    if flow == AutonomousFlow.BUGFIX:
        return [RunPhase.CLARIFY, RunPhase.DEBUG, RunPhase.VERIFY, RunPhase.REVIEW, RunPhase.FINALIZE]
    if flow == AutonomousFlow.REVIEW_ONLY:
        return [RunPhase.CLARIFY, RunPhase.REVIEW, RunPhase.FINALIZE]
    if flow == AutonomousFlow.TEST_GENERATION:
        return [RunPhase.CLARIFY, RunPhase.VERIFY, RunPhase.REVIEW, RunPhase.FINALIZE]
    return [RunPhase.CLARIFY, RunPhase.DESIGN, RunPhase.IMPLEMENT, RunPhase.VERIFY, RunPhase.REVIEW, RunPhase.FINALIZE]


def _role_for_phase(phase: RunPhase) -> str:
    mapping = {
        RunPhase.CLARIFY: AgentRole.ORCHESTRATOR.value,
        RunPhase.DESIGN: AgentRole.ARCHITECT.value,
        RunPhase.IMPLEMENT: AgentRole.IMPLEMENTER.value,
        RunPhase.VERIFY: AgentRole.QA_ENGINEER.value,
        RunPhase.DEBUG: AgentRole.DEBUGGER.value,
        RunPhase.REVIEW: AgentRole.REVIEWER.value,
        RunPhase.FINALIZE: AgentRole.ORCHESTRATOR.value,
    }
    return mapping[phase]


def _build_execution_plan(task_description: str, flow: AutonomousFlow) -> ExecutionPlan:
    requirements = _extract_bullets(task_description) or [task_description.strip()]
    questions = _generate_clarification_questions(task_description, flow)
    steps = [
        PlanStep(
            phase=phase,
            role=_role_for_phase(phase),
            description=f"{phase.value.replace('-', ' ').title()} phase for {flow.value} work.",
        )
        for phase in _phase_sequence(flow)
    ]

    return ExecutionPlan(
        flow=flow,
        summary=f"Autonomous {flow.value} flow for: {_normalize_task(task_description)}",
        requirements=requirements,
        acceptance_criteria=_default_acceptance_criteria(flow),
        clarification_questions=questions,
        steps=steps,
    )


def _build_role_map(config: SwarmConfig, repo_metadata: RepoMetadata | None) -> dict[str, AgentSpec]:
    specs = config.agents or (repo_metadata.agent_specs if repo_metadata else [])
    return {str(spec.role).lower(): spec for spec in specs}


def _find_next_phase(plan: ExecutionPlan, current_phase: RunPhase) -> RunPhase:
    phases = [step.phase for step in plan.steps]
    try:
        idx = phases.index(current_phase)
    except ValueError:
        return RunPhase.FINALIZE

    if idx + 1 >= len(phases):
        return RunPhase.FINALIZE
    return phases[idx + 1]


def _plan_contains(plan: ExecutionPlan, phase: RunPhase) -> bool:
    return any(step.phase == phase for step in plan.steps)


def _next_validation_phase(plan: ExecutionPlan) -> RunPhase:
    if _plan_contains(plan, RunPhase.VERIFY):
        return RunPhase.VERIFY
    if _plan_contains(plan, RunPhase.REVIEW):
        return RunPhase.REVIEW
    return RunPhase.FINALIZE


def _next_phase_after_success(run_state: SwarmRunState, phase: RunPhase) -> RunPhase:
    if phase in {RunPhase.DEBUG, RunPhase.IMPLEMENT}:
        return _next_validation_phase(run_state.plan)
    return _find_next_phase(run_state.plan, phase)


def _set_step_status(plan: ExecutionPlan, phase: RunPhase, status: TaskStatus) -> None:
    for step in plan.steps:
        if step.phase == phase:
            step.status = status
            return


def _gate_seen(run_state: SwarmRunState, gate_type: GateType) -> bool:
    return any(record.gate_type == gate_type for record in run_state.gate_history)


def _make_gate(run_state: SwarmRunState, gate_type: GateType) -> GateRecord:
    gate_id = f"{gate_type.value}-{len(run_state.gate_history) + 1}"
    return GateRecord(gate_id=gate_id, gate_type=gate_type, status=GateStatus.PENDING)


async def _emit(on_event: Any | None, payload: dict[str, Any]) -> None:
    if on_event:
        await on_event(payload)


async def _pause_for_gate(
    run_state: SwarmRunState,
    gate_type: GateType,
    *,
    next_phase: RunPhase,
    state_store: Any,
    on_event: Any | None,
) -> SwarmRunState:
    run_state.pending_gate = _make_gate(run_state, gate_type)
    run_state.current_phase = next_phase
    run_state.status = TaskStatus.PENDING
    await state_store.save_run_state(run_state)
    await _emit(
        on_event,
        {
            "type": "clarification_required"
            if gate_type == GateType.CLARIFICATION_REQUIRED
            else "approval_requested",
            "task_id": run_state.task_id,
            "gate": run_state.pending_gate.model_dump(),
            "phase": run_state.current_phase,
        },
    )
    return run_state


def _auto_accept_gate(run_state: SwarmRunState, gate_type: GateType) -> None:
    run_state.gate_history.append(
        GateRecord(
            gate_id=f"{gate_type.value}-{len(run_state.gate_history) + 1}",
            gate_type=gate_type,
            status=GateStatus.AUTO_APPROVED,
        )
    )


def _append_changed_artifacts(run_state: SwarmRunState, result: StructuredResult) -> None:
    seen = set(run_state.changed_artifacts)
    for artifact in _result_to_agent_output(result).artifacts:
        if artifact in seen:
            continue
        seen.add(artifact)
        run_state.changed_artifacts.append(artifact)


def _latest_result(run_state: SwarmRunState, role: str | None = None) -> StructuredResult | None:
    for result in reversed(run_state.phase_results):
        if role is None or result.role == role:
            return result
    return None


def _has_blocking_findings(result: StructuredResult) -> bool:
    return any(finding.severity in {Severity.BLOCKER, Severity.MAJOR} for finding in result.findings)


def _retry_key(phase: RunPhase) -> str:
    if phase == RunPhase.REVIEW:
        return AgentRole.IMPLEMENTER.value
    if phase == RunPhase.VERIFY:
        return AgentRole.DEBUGGER.value
    return str(phase.value)


def _increment_retry(run_state: SwarmRunState, key: str) -> int:
    run_state.retry_counts[key] = run_state.retry_counts.get(key, 0) + 1
    return run_state.retry_counts[key]


def _max_retries(role_map: dict[str, AgentSpec], role: str) -> int:
    spec = role_map.get(role)
    return spec.escalation.max_retries if spec else 0


def _phase_task_description(run_state: SwarmRunState, phase: RunPhase) -> str:
    lines = [
        f"Autonomous flow: {run_state.plan.flow.value}",
        f"Original task: {run_state.task_description}",
        "Locked requirements:",
        *[f"- {item}" for item in run_state.plan.requirements],
        "Acceptance criteria:",
        *[f"- {item}" for item in run_state.plan.acceptance_criteria],
    ]

    if run_state.changed_artifacts:
        lines.append("Changed artifacts:")
        lines.extend(f"- {path}" for path in run_state.changed_artifacts)

    if phase == RunPhase.DESIGN:
        lines.append("Produce the technical approach, module boundaries, and risk notes.")
    elif phase == RunPhase.IMPLEMENT:
        architect = _latest_result(run_state, AgentRole.ARCHITECT.value)
        if architect:
            lines.append(f"Architect guidance: {architect.summary}")
        lines.append("Implement the requested behavior and return concrete diffs.")
    elif phase == RunPhase.VERIFY:
        lines.append("Verify the current implementation against the locked requirements.")
        latest_debug = _latest_result(run_state, AgentRole.DEBUGGER.value)
        if latest_debug:
            lines.append(f"Latest debug summary: {latest_debug.summary}")
    elif phase == RunPhase.DEBUG:
        latest_verify = _latest_result(run_state, AgentRole.QA_ENGINEER.value)
        latest_review = _latest_result(run_state, AgentRole.REVIEWER.value)
        if latest_verify:
            lines.append(f"Verification failure summary: {latest_verify.summary}")
        if latest_review and latest_review.findings:
            lines.append("Review findings to address:")
            lines.extend(
                f"- [{finding.severity}] {finding.file}: {finding.description}"
                for finding in latest_review.findings
            )
        lines.append("Identify the root cause and return the minimal corrective diff.")
    elif phase == RunPhase.REVIEW:
        lines.append("Review only the changed artifacts and report blocker/major issues precisely.")

    return "\n".join(lines)


def _phase_error_trace(run_state: SwarmRunState, phase: RunPhase) -> str:
    if phase == RunPhase.DEBUG:
        latest_verify = _latest_result(run_state, AgentRole.QA_ENGINEER.value)
        if latest_verify and latest_verify.escalation_reason:
            return latest_verify.escalation_reason
        if latest_verify and latest_verify.findings:
            return "\n".join(f.description for f in latest_verify.findings)
    return ""


def _completion_summary(run_state: SwarmRunState) -> str:
    completed = [step.phase.value for step in run_state.plan.steps if step.status == TaskStatus.DONE]
    changed = ", ".join(run_state.changed_artifacts) or "none"
    return (
        f"Completed phases: {', '.join(completed)}. "
        f"Changed artifacts: {changed}. "
        f"Final status: {run_state.status}."
    )


def _executor_commands_for_phase(
    executor: AutonomousExecutor, phase: RunPhase, result: StructuredResult
) -> list[str]:
    if result.suggested_commands:
        return result.suggested_commands
    if phase == RunPhase.VERIFY:
        return executor.default_commands_for_verification()
    return []


async def _continue_autonomous(
    run_state: SwarmRunState,
    *,
    config: SwarmConfig,
    api_key: str,
    repo_root: Path,
    state_store: Any,
    role_map: dict[str, AgentSpec],
    on_event: Any | None = None,
    **driver_kwargs: object,
) -> SwarmRunState:
    executor = AutonomousExecutor(repo_root, run_state.repo_metadata) if run_state.execute else None

    while True:
        await state_store.save_run_state(run_state)

        if run_state.status in {TaskStatus.DONE, TaskStatus.ESCALATED}:
            return run_state

        if run_state.current_phase == RunPhase.COMPLETED:
            run_state.status = TaskStatus.DONE
            run_state.completion_summary = _completion_summary(run_state)
            await state_store.save_run_state(run_state)
            await _emit(
                on_event,
                {"type": "run_completed", "task_id": run_state.task_id, "status": run_state.status},
            )
            return run_state

        if run_state.current_phase == RunPhase.CLARIFY:
            _set_step_status(run_state.plan, RunPhase.CLARIFY, TaskStatus.IN_PROGRESS)
            if run_state.plan.clarification_questions and not _gate_seen(
                run_state, GateType.CLARIFICATION_REQUIRED
            ):
                return await _pause_for_gate(
                    run_state,
                    GateType.CLARIFICATION_REQUIRED,
                    next_phase=RunPhase.CLARIFY,
                    state_store=state_store,
                    on_event=on_event,
                )

            _set_step_status(run_state.plan, RunPhase.CLARIFY, TaskStatus.DONE)
            next_phase = _find_next_phase(run_state.plan, RunPhase.CLARIFY)
            if run_state.approval_mode == ApprovalMode.MAJOR_GATES and not _gate_seen(
                run_state, GateType.REQUIREMENTS_LOCKED
            ):
                return await _pause_for_gate(
                    run_state,
                    GateType.REQUIREMENTS_LOCKED,
                    next_phase=next_phase,
                    state_store=state_store,
                    on_event=on_event,
                )
            if not _gate_seen(run_state, GateType.REQUIREMENTS_LOCKED):
                _auto_accept_gate(run_state, GateType.REQUIREMENTS_LOCKED)
            run_state.current_phase = next_phase
            run_state.status = TaskStatus.IN_PROGRESS
            continue

        if run_state.current_phase == RunPhase.FINALIZE:
            _set_step_status(run_state.plan, RunPhase.FINALIZE, TaskStatus.IN_PROGRESS)
            _set_step_status(run_state.plan, RunPhase.FINALIZE, TaskStatus.DONE)
            run_state.current_phase = RunPhase.COMPLETED
            run_state.status = TaskStatus.DONE
            run_state.completion_summary = _completion_summary(run_state)
            continue

        phase = run_state.current_phase
        role_name = _role_for_phase(phase)
        spec = role_map.get(role_name)
        if spec is None:
            run_state.status = TaskStatus.ESCALATED
            run_state.escalation_reason = f"Required role '{role_name}' is not available in config."
            _set_step_status(run_state.plan, phase, TaskStatus.ESCALATED)
            continue

        _set_step_status(run_state.plan, phase, TaskStatus.IN_PROGRESS)
        await _emit(
            on_event,
            {
                "type": "phase_started",
                "task_id": run_state.task_id,
                "phase": phase,
                "role": role_name,
            },
        )

        candidate_files = get_eligible_candidates(repo_root)
        recently_changed = [repo_root / path for path in run_state.changed_artifacts]

        result, _ = await _invoke_agent(
            spec=spec,
            task_description=_phase_task_description(run_state, phase),
            file_paths=candidate_files,
            config=config,
            api_key=api_key,
            state_store=state_store,
            repo_metadata=run_state.repo_metadata,
            previous_outputs=run_state.previous_outputs,
            task_id=run_state.task_id,
            agent_role=role_name,
            error_trace=_phase_error_trace(run_state, phase),
            recently_changed=recently_changed,
            on_event=on_event,
            **driver_kwargs,
        )

        run_state.phase_results.append(result)
        run_state.previous_outputs.append(_result_to_agent_output(result))
        _append_changed_artifacts(run_state, result)
        _write_result(result, config.output_dir, run_state.task_id)

        await _emit(
            on_event,
            {
                "type": "phase_completed",
                "task_id": run_state.task_id,
                "phase": phase,
                "role": role_name,
                "status": result.status,
            },
        )

        if executor and phase in {RunPhase.IMPLEMENT, RunPhase.DEBUG, RunPhase.VERIFY}:
            outcome = await executor.execute(
                result, default_commands=_executor_commands_for_phase(executor, phase, result)
            )
            run_state.executor_outcomes.append(outcome)
            if outcome.status == TaskStatus.ESCALATED:
                _set_step_status(run_state.plan, phase, TaskStatus.FAILED)
                run_state.escalation_reason = outcome.failure_reason
                if phase == RunPhase.VERIFY:
                    retries = _increment_retry(run_state, AgentRole.DEBUGGER.value)
                    if retries <= _max_retries(role_map, AgentRole.DEBUGGER.value):
                        run_state.current_phase = RunPhase.DEBUG
                        run_state.status = TaskStatus.IN_PROGRESS
                        continue
                run_state.status = TaskStatus.ESCALATED
                continue

        if result.status == TaskStatus.ESCALATED:
            _set_step_status(run_state.plan, phase, TaskStatus.ESCALATED)
            run_state.escalation_reason = result.escalation_reason
            if phase == RunPhase.VERIFY:
                retries = _increment_retry(run_state, AgentRole.DEBUGGER.value)
                if retries <= _max_retries(role_map, AgentRole.DEBUGGER.value):
                    run_state.current_phase = RunPhase.DEBUG
                    run_state.status = TaskStatus.IN_PROGRESS
                    continue
            run_state.status = TaskStatus.ESCALATED
            continue

        _set_step_status(run_state.plan, phase, TaskStatus.DONE)

        if phase == RunPhase.VERIFY and _has_blocking_findings(result):
            retries = _increment_retry(run_state, AgentRole.DEBUGGER.value)
            if retries <= _max_retries(role_map, AgentRole.DEBUGGER.value):
                run_state.current_phase = RunPhase.DEBUG
                run_state.status = TaskStatus.IN_PROGRESS
                continue
            run_state.status = TaskStatus.ESCALATED
            run_state.escalation_reason = "Verification failed repeatedly and exhausted debugger retries."
            _set_step_status(run_state.plan, phase, TaskStatus.ESCALATED)
            continue

        if phase == RunPhase.REVIEW and _has_blocking_findings(result):
            retries = _increment_retry(run_state, AgentRole.IMPLEMENTER.value)
            if retries <= _max_retries(role_map, AgentRole.IMPLEMENTER.value):
                run_state.current_phase = RunPhase.IMPLEMENT
                run_state.status = TaskStatus.IN_PROGRESS
                continue
            run_state.status = TaskStatus.ESCALATED
            run_state.escalation_reason = "Review found blocker/major issues after implementer retries were exhausted."
            _set_step_status(run_state.plan, phase, TaskStatus.ESCALATED)
            continue

        if phase == RunPhase.DESIGN:
            next_phase = _find_next_phase(run_state.plan, phase)
            if run_state.approval_mode == ApprovalMode.MAJOR_GATES and not _gate_seen(
                run_state, GateType.DESIGN_LOCKED
            ):
                return await _pause_for_gate(
                    run_state,
                    GateType.DESIGN_LOCKED,
                    next_phase=next_phase,
                    state_store=state_store,
                    on_event=on_event,
                )
            if not _gate_seen(run_state, GateType.DESIGN_LOCKED):
                _auto_accept_gate(run_state, GateType.DESIGN_LOCKED)
            run_state.current_phase = next_phase
            continue

        if phase == RunPhase.REVIEW:
            if run_state.approval_mode == ApprovalMode.MAJOR_GATES and not _gate_seen(
                run_state, GateType.RELEASE_READY
            ):
                return await _pause_for_gate(
                    run_state,
                    GateType.RELEASE_READY,
                    next_phase=RunPhase.FINALIZE,
                    state_store=state_store,
                    on_event=on_event,
                )
            if not _gate_seen(run_state, GateType.RELEASE_READY):
                _auto_accept_gate(run_state, GateType.RELEASE_READY)
            run_state.current_phase = RunPhase.FINALIZE
            continue

        run_state.current_phase = _next_phase_after_success(run_state, phase)
        run_state.status = TaskStatus.IN_PROGRESS


async def run_autonomous(
    task_description: str,
    config: SwarmConfig,
    api_key: str,
    repo_root: Path,
    *,
    repo_metadata: RepoMetadata | None = None,
    task_id: str | None = None,
    approval_mode: ApprovalMode = ApprovalMode.MAJOR_GATES,
    execute: bool = False,
    on_event: Any | None = None,
    **driver_kwargs: object,
) -> SwarmRunState:
    task_id = task_id or str(uuid.uuid4())
    repo_metadata = repo_metadata
    state_store = get_state_store(config)
    role_map = _build_role_map(config, repo_metadata)
    flow = _determine_autonomous_flow(task_description)
    run_state = SwarmRunState(
        task_id=task_id,
        task_description=task_description,
        platform=config.platform,
        status=TaskStatus.IN_PROGRESS,
        quality_gate_strict=config.quality_gate_strict,
        current_phase=RunPhase.CLARIFY,
        approval_mode=approval_mode,
        execute=execute,
        plan=_build_execution_plan(task_description, flow),
        repo_metadata=repo_metadata,
    )

    await state_store.save_run_state(run_state)
    await _emit(
        on_event,
        {
            "type": "plan_created",
            "task_id": run_state.task_id,
            "flow": run_state.plan.flow,
            "plan": run_state.plan.model_dump(),
        },
    )

    return await _continue_autonomous(
        run_state,
        config=config,
        api_key=api_key,
        repo_root=repo_root,
        state_store=state_store,
        role_map=role_map,
        on_event=on_event,
        **driver_kwargs,
    )


async def resume_autonomous(
    task_id: str,
    config: SwarmConfig,
    api_key: str,
    repo_root: Path,
    *,
    decision: GateDecision = GateDecision.APPROVE,
    comments: str = "",
    on_event: Any | None = None,
    **driver_kwargs: object,
) -> SwarmRunState:
    state_store = get_state_store(config)
    run_state = await state_store.load_run_state(task_id)
    if not run_state:
        raise ValueError(f"No autonomous run state found for task_id: {task_id}")
    if not run_state.pending_gate:
        raise ValueError(f"Task {task_id} has no pending gate to resume.")

    pending_gate = run_state.pending_gate
    if decision == GateDecision.REJECT:
        pending_gate.status = GateStatus.REJECTED
        pending_gate.comments = comments
        run_state.gate_history.append(pending_gate)
        run_state.pending_gate = None
        run_state.status = TaskStatus.ESCALATED
        run_state.escalation_reason = comments or f"Gate rejected: {pending_gate.gate_type}"
        await state_store.save_run_state(run_state)
        await _emit(
            on_event,
            {
                "type": "phase_failed",
                "task_id": task_id,
                "phase": run_state.current_phase,
                "reason": run_state.escalation_reason,
            },
        )
        return run_state

    if pending_gate.gate_type == GateType.CLARIFICATION_REQUIRED:
        if not comments.strip():
            raise ValueError("Clarification responses must include comments.")
        run_state.plan.requirements.append(f"User clarification: {comments.strip()}")
        run_state.plan.clarification_questions = []
        run_state.current_phase = RunPhase.CLARIFY

    pending_gate.status = GateStatus.APPROVED
    pending_gate.comments = comments
    run_state.gate_history.append(pending_gate)
    run_state.pending_gate = None
    run_state.status = TaskStatus.IN_PROGRESS

    role_map = _build_role_map(config, run_state.repo_metadata)
    await state_store.save_run_state(run_state)
    return await _continue_autonomous(
        run_state,
        config=config,
        api_key=api_key,
        repo_root=repo_root,
        state_store=state_store,
        role_map=role_map,
        on_event=on_event,
        **driver_kwargs,
    )
