"""test_orchestrator.py — Tests for the swarm execution engine."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.orchestrator import (
    ParallelConflictError,
    _detect_conflicts,
    _write_result,
    build_context,
    resume_autonomous,
    run_autonomous,
    run_parallel,
    run_sequential,
)
from agent_core.schemas import (
    AgentOutput,
    AgentSpec,
    ApprovalMode,
    GateDecision,
    GateType,
    ReviewFinding,
    RunPhase,
    Severity,
    StructuredResult,
    SwarmConfig,
    SwarmContext,
    TaskStatus,
)


class TestBuildContext:
    def test_returns_swarm_context(
        self, minimal_spec: AgentSpec, minimal_config: SwarmConfig, tmp_path: Path
    ) -> None:
        ctx = build_context(
            task_description="Do a thing.",
            config=minimal_config,
            repo_metadata=None,
            file_paths=[],
        )
        assert isinstance(ctx, SwarmContext)
        assert ctx.task_description == "Do a thing."

    def test_task_id_auto_generated(self, minimal_config: SwarmConfig) -> None:
        ctx = build_context("task", minimal_config, None, [])
        assert ctx.task_id  # non-empty UUID

    def test_task_id_respected_when_provided(self, minimal_config: SwarmConfig) -> None:
        ctx = build_context("task", minimal_config, None, [], task_id="my-task-123")
        assert ctx.task_id == "my-task-123"

    def test_nonexistent_files_skipped(self, minimal_config: SwarmConfig, tmp_path: Path) -> None:
        ghost = tmp_path / "ghost.py"
        ctx = build_context("task", minimal_config, None, [ghost])
        assert ctx.relevant_files == []

    def test_token_budget_enforced(self, minimal_config: SwarmConfig, tmp_path: Path) -> None:
        # Write a file with enough content to consume the whole budget
        big = tmp_path / "big.py"
        big.write_text("x = 1\n" * 5000)
        small = tmp_path / "small.py"
        small.write_text("y = 2\n")

        # Budget is 10_000 tokens in minimal_config
        ctx = build_context("task", minimal_config, None, [big, small])
        total_tokens = sum(f.token_count for f in ctx.relevant_files)
        assert total_tokens <= minimal_config.token_budget_per_agent

    def test_previous_outputs_passed_through(self, minimal_config: SwarmConfig) -> None:
        prior = AgentOutput(
            role="architect",
            status=TaskStatus.DONE,
            summary="Design complete.",
        )
        ctx = build_context("task", minimal_config, None, [], previous_outputs=[prior])
        assert len(ctx.previous_outputs) == 1
        assert ctx.previous_outputs[0].role == "architect"

    def test_constraints_include_quality_gate_strict(self, minimal_config: SwarmConfig) -> None:
        ctx = build_context("task", minimal_config, None, [])
        assert "quality_gate_strict" in ctx.constraints
        assert ctx.constraints["quality_gate_strict"] is True


class TestWriteResult:
    def test_writes_json_file(self, done_result: StructuredResult, tmp_path: Path) -> None:
        _write_result(done_result, tmp_path, "task-001")
        out = tmp_path / "task-001" / "implementer.json"
        assert out.exists()

    def test_written_json_is_valid(self, done_result: StructuredResult, tmp_path: Path) -> None:
        import json

        _write_result(done_result, tmp_path, "task-001")
        out = tmp_path / "task-001" / "implementer.json"
        data = json.loads(out.read_text())
        assert data["role"] == "implementer"
        assert data["status"] == "done"

    def test_creates_parent_dirs(self, done_result: StructuredResult, tmp_path: Path) -> None:
        _write_result(done_result, tmp_path / "deep" / "nested", "task-001")
        assert (tmp_path / "deep" / "nested" / "task-001" / "implementer.json").exists()


class TestRunSequential:
    async def test_single_agent_invoked(
        self,
        minimal_spec: AgentSpec,
        minimal_config: SwarmConfig,
        done_result: StructuredResult,
        tmp_path: Path,
    ) -> None:
        minimal_config = minimal_config.model_copy(update={"output_dir": tmp_path / "outputs"})
        fake_driver = MagicMock()
        fake_driver.invoke = AsyncMock(return_value=done_result)

        with patch("agent_core.orchestrator._get_driver", return_value=fake_driver):
            results = await run_sequential(
                task_description="Do a thing.",
                agent_chain=[(minimal_spec, [])],
                config=minimal_config,
                api_key="test-key",
            )

        assert len(results) == 1
        assert results[0].status == TaskStatus.DONE
        fake_driver.invoke.assert_called_once()

    async def test_previous_outputs_accumulated(
        self,
        minimal_spec: AgentSpec,
        architect_spec: AgentSpec,
        minimal_config: SwarmConfig,
        done_result: StructuredResult,
        tmp_path: Path,
    ) -> None:
        minimal_config = minimal_config.model_copy(update={"output_dir": tmp_path / "outputs"})
        captured_contexts: list[SwarmContext] = []

        async def capture_invoke(ctx: SwarmContext) -> StructuredResult:
            captured_contexts.append(ctx.model_copy(deep=True))
            return StructuredResult(
                task_id=ctx.task_id,
                role="architect",
                status=TaskStatus.DONE,
                summary="Done.",
            )

        fake_driver = MagicMock()
        fake_driver.invoke = AsyncMock(side_effect=capture_invoke)

        with patch("agent_core.orchestrator._get_driver", return_value=fake_driver):
            await run_sequential(
                task_description="Design then implement.",
                agent_chain=[(architect_spec, []), (minimal_spec, [])],
                config=minimal_config,
                api_key="test-key",
            )

        # Second invocation should see the first agent's output
        assert len(captured_contexts) == 2
        assert len(captured_contexts[1].previous_outputs) == 1
        assert captured_contexts[1].previous_outputs[0].role == "architect"

    async def test_escalation_halts_chain_in_strict_mode(
        self,
        minimal_spec: AgentSpec,
        architect_spec: AgentSpec,
        minimal_config: SwarmConfig,
        escalated_result: StructuredResult,
        tmp_path: Path,
    ) -> None:
        minimal_config = minimal_config.model_copy(
            update={"output_dir": tmp_path / "outputs", "quality_gate_strict": True}
        )
        fake_driver = MagicMock()
        fake_driver.invoke = AsyncMock(return_value=escalated_result)

        with patch("agent_core.orchestrator._get_driver", return_value=fake_driver):
            results = await run_sequential(
                task_description="Do a thing.",
                agent_chain=[(architect_spec, []), (minimal_spec, [])],
                config=minimal_config,
                api_key="test-key",
            )

        # Only the first agent ran (it escalated, chain stopped)
        assert len(results) == 1
        assert results[0].status == TaskStatus.ESCALATED

    async def test_escalation_continues_in_non_strict_mode(
        self,
        minimal_spec: AgentSpec,
        architect_spec: AgentSpec,
        minimal_config: SwarmConfig,
        escalated_result: StructuredResult,
        done_result: StructuredResult,
        tmp_path: Path,
    ) -> None:
        minimal_config = minimal_config.model_copy(
            update={"output_dir": tmp_path / "outputs", "quality_gate_strict": False}
        )
        results_sequence = [escalated_result, done_result]
        fake_driver = MagicMock()
        fake_driver.invoke = AsyncMock(side_effect=results_sequence)

        with patch("agent_core.orchestrator._get_driver", return_value=fake_driver):
            results = await run_sequential(
                task_description="Do a thing.",
                agent_chain=[(architect_spec, []), (minimal_spec, [])],
                config=minimal_config,
                api_key="test-key",
            )

        # Both agents ran despite the first escalating
        assert len(results) == 2


class TestDetectConflicts:
    def test_no_conflict_when_consistent(self, minimal_config: SwarmConfig) -> None:
        results = [
            StructuredResult(
                task_id="t1",
                role="reviewer-a",
                status=TaskStatus.DONE,
                summary="x",
                findings=[
                    ReviewFinding(
                        file="auth.py", severity=Severity.BLOCKER, description="SQL injection."
                    )
                ],
            ),
            StructuredResult(
                task_id="t1",
                role="reviewer-b",
                status=TaskStatus.DONE,
                summary="x",
                findings=[
                    ReviewFinding(file="auth.py", severity=Severity.BLOCKER, description="Same.")
                ],
            ),
        ]
        # Should not raise — same severity is not a conflict
        _detect_conflicts(results, minimal_config)

    def test_conflict_logged_when_severity_differs(
        self, minimal_config: SwarmConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        non_strict = minimal_config.model_copy(update={"quality_gate_strict": False})

        results = [
            StructuredResult(
                task_id="t1",
                role="reviewer-a",
                status=TaskStatus.DONE,
                summary="x",
                findings=[
                    ReviewFinding(
                        file="auth.py", severity=Severity.BLOCKER, description="Critical."
                    )
                ],
            ),
            StructuredResult(
                task_id="t1",
                role="reviewer-b",
                status=TaskStatus.DONE,
                summary="x",
                findings=[
                    ReviewFinding(file="auth.py", severity=Severity.NIT, description="Minor.")
                ],
            ),
        ]
        with caplog.at_level(logging.WARNING):
            _detect_conflicts(results, non_strict)
        assert any("Conflicting" in m for m in caplog.messages)

    def test_conflict_raises_in_strict_mode(self, minimal_config: SwarmConfig) -> None:
        results = [
            StructuredResult(
                task_id="t1",
                role="reviewer-a",
                status=TaskStatus.DONE,
                summary="x",
                findings=[
                    ReviewFinding(file="auth.py", severity=Severity.BLOCKER, description="Critical.")
                ],
            ),
            StructuredResult(
                task_id="t1",
                role="reviewer-b",
                status=TaskStatus.DONE,
                summary="x",
                findings=[ReviewFinding(file="auth.py", severity=Severity.NIT, description="Minor.")],
            ),
        ]
        with pytest.raises(ParallelConflictError):
            _detect_conflicts(results, minimal_config)


def _make_result(
    role: str,
    *,
    status: TaskStatus = TaskStatus.DONE,
    summary: str = "ok",
    findings: list[ReviewFinding] | None = None,
) -> StructuredResult:
    payload = {
        "task_id": "auto-task",
        "role": role,
        "status": status,
        "summary": summary,
    }
    if findings:
        payload["findings"] = findings
    if status == TaskStatus.ESCALATED:
        payload["escalation_reason"] = "phase failed"
    return StructuredResult.model_validate(payload)


def _driver_factory(results_by_role: dict[str, list[StructuredResult]]):
    def _factory(platform, spec, api_key, **kwargs):
        queue = results_by_role[str(spec.role)]
        driver = MagicMock()
        driver.invoke = AsyncMock(side_effect=lambda ctx: queue.pop(0))
        return driver

    return _factory


class TestAutonomousFlow:
    async def test_clarification_required_for_ambiguous_task(
        self, autonomous_config: SwarmConfig, fake_repo: Path
    ) -> None:
        state = await run_autonomous(
            task_description="Fix it",
            config=autonomous_config,
            api_key="test-key",
            repo_root=fake_repo,
            approval_mode=ApprovalMode.NONE,
        )

        assert state.status == TaskStatus.PENDING
        assert state.pending_gate is not None
        assert state.pending_gate.gate_type == GateType.CLARIFICATION_REQUIRED

    async def test_major_gate_pause_and_resume(
        self, autonomous_config: SwarmConfig, fake_repo: Path
    ) -> None:
        results = {
            "architect": [_make_result("architect", summary="Design complete.")],
            "implementer": [_make_result("implementer", summary="Implementation complete.")],
            "qa-engineer": [_make_result("qa-engineer", summary="Verification complete.")],
            "reviewer": [_make_result("reviewer", summary="Review complete.")],
        }

        with patch("agent_core.orchestrator._get_driver", side_effect=_driver_factory(results)):
            state = await run_autonomous(
                task_description="Add health endpoint\n- Return 200 OK",
                config=autonomous_config,
                api_key="test-key",
                repo_root=fake_repo,
                approval_mode=ApprovalMode.MAJOR_GATES,
            )

            assert state.pending_gate is not None
            assert state.pending_gate.gate_type == GateType.REQUIREMENTS_LOCKED

            state = await resume_autonomous(
                task_id=state.task_id,
                config=autonomous_config,
                api_key="test-key",
                repo_root=fake_repo,
                decision=GateDecision.APPROVE,
            )
            assert state.pending_gate is not None
            assert state.pending_gate.gate_type == GateType.DESIGN_LOCKED

            state = await resume_autonomous(
                task_id=state.task_id,
                config=autonomous_config,
                api_key="test-key",
                repo_root=fake_repo,
                decision=GateDecision.APPROVE,
            )
            assert state.pending_gate is not None
            assert state.pending_gate.gate_type == GateType.RELEASE_READY

            state = await resume_autonomous(
                task_id=state.task_id,
                config=autonomous_config,
                api_key="test-key",
                repo_root=fake_repo,
                decision=GateDecision.APPROVE,
            )

        assert state.status == TaskStatus.DONE
        assert state.current_phase == RunPhase.COMPLETED
        assert len(state.gate_history) == 3

    async def test_bugfix_flow_completes_without_major_gate_pauses(
        self, autonomous_config: SwarmConfig, fake_repo: Path
    ) -> None:
        results = {
            "debugger": [_make_result("debugger", summary="Bug fixed.")],
            "qa-engineer": [_make_result("qa-engineer", summary="Regression verified.")],
            "reviewer": [_make_result("reviewer", summary="Review complete.")],
        }

        with patch("agent_core.orchestrator._get_driver", side_effect=_driver_factory(results)):
            state = await run_autonomous(
                task_description="Fix login bug in auth flow",
                config=autonomous_config,
                api_key="test-key",
                repo_root=fake_repo,
                approval_mode=ApprovalMode.NONE,
            )

        assert state.plan.flow.value == "bugfix"
        assert state.status == TaskStatus.DONE
        assert state.current_phase == RunPhase.COMPLETED

    async def test_verify_failure_routes_to_debugger(
        self, autonomous_config: SwarmConfig, fake_repo: Path
    ) -> None:
        results = {
            "architect": [_make_result("architect", summary="Design complete.")],
            "implementer": [_make_result("implementer", summary="Implementation complete.")],
            "qa-engineer": [
                _make_result(
                    "qa-engineer",
                    summary="Verification failed.",
                    findings=[
                        ReviewFinding(
                            file="src/main.py",
                            severity=Severity.BLOCKER,
                            description="Behavior still fails.",
                        )
                    ],
                ),
                _make_result("qa-engineer", summary="Verification complete."),
            ],
            "debugger": [_make_result("debugger", summary="Root cause fixed.")],
            "reviewer": [_make_result("reviewer", summary="Review complete.")],
        }

        with patch("agent_core.orchestrator._get_driver", side_effect=_driver_factory(results)):
            state = await run_autonomous(
                task_description="Add health endpoint\n- Return 200 OK",
                config=autonomous_config,
                api_key="test-key",
                repo_root=fake_repo,
                approval_mode=ApprovalMode.NONE,
            )

        assert state.status == TaskStatus.DONE
        assert state.retry_counts["debugger"] == 1
        assert [result.role for result in state.phase_results] == [
            "architect",
            "implementer",
            "qa-engineer",
            "debugger",
            "qa-engineer",
            "reviewer",
        ]

    async def test_review_findings_route_back_to_implementer(
        self, autonomous_config: SwarmConfig, fake_repo: Path
    ) -> None:
        results = {
            "architect": [_make_result("architect", summary="Design complete.")],
            "implementer": [
                _make_result("implementer", summary="Implementation pass 1."),
                _make_result("implementer", summary="Implementation pass 2."),
            ],
            "qa-engineer": [
                _make_result("qa-engineer", summary="Verification pass 1."),
                _make_result("qa-engineer", summary="Verification pass 2."),
            ],
            "reviewer": [
                _make_result(
                    "reviewer",
                    summary="Review found issues.",
                    findings=[
                        ReviewFinding(
                            file="src/main.py",
                            severity=Severity.MAJOR,
                            description="Missing edge case handling.",
                        )
                    ],
                ),
                _make_result("reviewer", summary="Review complete."),
            ],
        }

        with patch("agent_core.orchestrator._get_driver", side_effect=_driver_factory(results)):
            state = await run_autonomous(
                task_description="Add health endpoint\n- Return 200 OK",
                config=autonomous_config,
                api_key="test-key",
                repo_root=fake_repo,
                approval_mode=ApprovalMode.NONE,
            )

        assert state.status == TaskStatus.DONE
        assert state.retry_counts["implementer"] == 1
        assert [result.role for result in state.phase_results] == [
            "architect",
            "implementer",
            "qa-engineer",
            "reviewer",
            "implementer",
            "qa-engineer",
            "reviewer",
        ]

    async def test_retry_exhaustion_escalates(
        self, autonomous_config: SwarmConfig, fake_repo: Path, debugger_spec: AgentSpec
    ) -> None:
        config = autonomous_config.model_copy(
            update={
                "agents": [
                    spec.model_copy(update={"escalation": debugger_spec.escalation.model_copy(update={"max_retries": 0})})
                    if str(spec.role) == "debugger"
                    else spec
                    for spec in autonomous_config.agents
                ]
            }
        )
        results = {
            "architect": [_make_result("architect", summary="Design complete.")],
            "implementer": [_make_result("implementer", summary="Implementation complete.")],
            "qa-engineer": [
                _make_result(
                    "qa-engineer",
                    summary="Verification failed.",
                    findings=[
                        ReviewFinding(
                            file="src/main.py",
                            severity=Severity.BLOCKER,
                            description="Still broken.",
                        )
                    ],
                )
            ],
        }

        with patch("agent_core.orchestrator._get_driver", side_effect=_driver_factory(results)):
            state = await run_autonomous(
                task_description="Add health endpoint\n- Return 200 OK",
                config=config,
                api_key="test-key",
                repo_root=fake_repo,
                approval_mode=ApprovalMode.NONE,
            )

        assert state.status == TaskStatus.ESCALATED


class TestRunParallel:
    async def test_parallel_conflict_raises(
        self, minimal_spec: AgentSpec, minimal_config: SwarmConfig
    ) -> None:
        reviewer_a = minimal_spec.model_copy(update={"role": "reviewer-a", "name": "reviewer-a"})
        reviewer_b = minimal_spec.model_copy(update={"role": "reviewer-b", "name": "reviewer-b"})
        results = {
            "reviewer-a": [
                _make_result(
                    "reviewer-a",
                    findings=[ReviewFinding(file="auth.py", severity=Severity.BLOCKER, description="Critical.")],
                )
            ],
            "reviewer-b": [
                _make_result(
                    "reviewer-b",
                    findings=[ReviewFinding(file="auth.py", severity=Severity.NIT, description="Minor.")],
                )
            ],
        }

        with patch("agent_core.orchestrator._get_driver", side_effect=_driver_factory(results)):
            with pytest.raises(ParallelConflictError):
                await run_parallel(
                    task_description="Review auth module",
                    agent_tasks=[(reviewer_a, []), (reviewer_b, [])],
                    config=minimal_config,
                    api_key="test-key",
                )
