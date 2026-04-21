"""test_schemas.py — Validates all Pydantic model constraints."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_core.schemas import (
    AgentSpec,
    FileDiff,
    Platform,
    QualityGate,
    ReviewFinding,
    Severity,
    StructuredResult,
    SwarmConfig,
    SwarmContext,
    TaskStatus,
    ToolPermission,
)


class TestAgentSpec:
    def test_valid_spec_passes(self, minimal_spec: AgentSpec) -> None:
        assert minimal_spec.name == "test-agent"

    def test_custom_role_string_allowed(self) -> None:
        spec = AgentSpec(
            name="security-reviewer",
            role="security-reviewer",  # not in AgentRole enum
            description="Custom role.",
            responsibilities=["Review auth."],
            quality_gates=[QualityGate(description="Auth reviewed.")],
            tools_allowed=[ToolPermission(name="Read")],
            out_of_scope=[],
        )
        assert spec.role == "security-reviewer"

    def test_missing_responsibilities_fails(self) -> None:
        with pytest.raises(ValidationError):
            AgentSpec(
                name="bad",
                role="implementer",
                description="x",
                responsibilities=[],  # violates min_length=1
                quality_gates=[QualityGate(description="x")],
                tools_allowed=[],
                out_of_scope=[],
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentSpec(
                name="x",
                role="implementer",
                description="x",
                responsibilities=["x"],
                quality_gates=[QualityGate(description="x")],
                tools_allowed=[],
                out_of_scope=[],
                unexpected_field="should fail",  # type: ignore[call-arg]
            )

    def test_escalation_defaults(self, minimal_spec: AgentSpec) -> None:
        assert minimal_spec.escalation.max_retries == 2
        assert minimal_spec.escalation.on_failure == TaskStatus.ESCALATED


class TestStructuredResult:
    def test_done_result_valid(self, done_result: StructuredResult) -> None:
        assert done_result.status == TaskStatus.DONE

    def test_escalated_without_reason_fails(self) -> None:
        with pytest.raises(ValidationError, match="escalation_reason"):
            StructuredResult(
                task_id="t1",
                role="implementer",
                status=TaskStatus.ESCALATED,
                summary="Failed.",
                # escalation_reason omitted — should fail validation
            )

    def test_escalated_with_reason_passes(self, escalated_result: StructuredResult) -> None:
        assert escalated_result.escalation_reason is not None

    def test_summary_max_length(self) -> None:
        with pytest.raises(ValidationError):
            StructuredResult(
                task_id="t1",
                role="implementer",
                status=TaskStatus.DONE,
                summary="x" * 501,  # exceeds max_length=500
            )

    def test_file_diff_embedded(self) -> None:
        from pathlib import Path

        result = StructuredResult(
            task_id="t1",
            role="implementer",
            status=TaskStatus.DONE,
            summary="Changed one file.",
            diffs=[
                FileDiff(
                    path=Path("src/auth.py"),
                    operation="modify",
                    unified_diff="--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1 +1 @@\n-pass\n+return True",
                    explanation="Added return value.",
                )
            ],
        )
        assert result.diffs[0].operation == "modify"

    def test_review_findings_severity_labels(self) -> None:
        result = StructuredResult(
            task_id="t1",
            role="reviewer",
            status=TaskStatus.DONE,
            summary="Two findings.",
            findings=[
                ReviewFinding(
                    file="src/auth.py", severity=Severity.BLOCKER, description="SQL injection."
                ),
                ReviewFinding(
                    file="src/auth.py", line=42, severity=Severity.NIT, description="Rename var."
                ),
            ],
        )
        assert result.findings[0].severity == Severity.BLOCKER
        assert result.findings[1].line == 42


class TestSwarmConfig:
    def test_valid_config(self, minimal_config: SwarmConfig) -> None:
        assert minimal_config.platform == Platform.CLAUDE_CODE

    def test_parallel_without_agents_fails(self) -> None:
        with pytest.raises(ValidationError, match="Parallel execution"):
            SwarmConfig(
                platform=Platform.CLAUDE_CODE,
                agents=[],  # empty
                max_parallel_agents=4,  # requires explicit agents
            )

    def test_max_parallel_agents_ceiling(self) -> None:
        with pytest.raises(ValidationError):
            SwarmConfig(
                platform=Platform.CODEX,
                max_parallel_agents=9,  # exceeds le=8
            )

    def test_output_dir_default(self) -> None:
        from pathlib import Path

        cfg = SwarmConfig(platform=Platform.GENERIC)
        assert cfg.output_dir == Path(".swarm/outputs")
        assert cfg.state_dir == Path(".swarm/state")


class TestSwarmContext:
    def test_minimal_context_valid(self, minimal_context: SwarmContext) -> None:
        assert minimal_context.task_id == "test-task-001"
        assert minimal_context.previous_outputs == []

    def test_extra_fields_rejected(self, minimal_context: SwarmContext) -> None:
        with pytest.raises(ValidationError):
            SwarmContext(
                task_id="x",
                task_description="x",
                platform=Platform.CLAUDE_CODE,
                sneaky_field="should fail",  # type: ignore[call-arg]
            )
