"""conftest.py — Shared fixtures for the agent_core test suite."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.schemas import (
    AgentSpec,
    Platform,
    QualityGate,
    StructuredResult,
    SwarmConfig,
    SwarmContext,
    TaskStatus,
    ToolPermission,
)

# ---------------------------------------------------------------------------
# Minimal valid AgentSpec fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_spec() -> AgentSpec:
    return AgentSpec(
        name="test-agent",
        role="implementer",
        description="A test agent.",
        responsibilities=["Do the thing."],
        quality_gates=[QualityGate(description="Thing was done.")],
        tools_allowed=[ToolPermission(name="Read"), ToolPermission(name="Write")],
        out_of_scope=["Nothing else."],
    )


@pytest.fixture()
def architect_spec() -> AgentSpec:
    return AgentSpec(
        name="architect",
        role="architect",
        description="Design systems.",
        responsibilities=["Evaluate options.", "Produce a design."],
        quality_gates=[
            QualityGate(description="Two alternatives evaluated."),
            QualityGate(description="API contract defined."),
        ],
        tools_allowed=[ToolPermission(name="Read"), ToolPermission(name="WebSearch")],
        out_of_scope=["Writing implementation code."],
    )


# ---------------------------------------------------------------------------
# SwarmConfig fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_config(tmp_path: Path) -> SwarmConfig:
    return SwarmConfig(
        platform=Platform.CLAUDE_CODE,
        token_budget_per_agent=10_000,
        max_parallel_agents=1,
        quality_gate_strict=True,
        output_dir=tmp_path / "outputs",
    )


# ---------------------------------------------------------------------------
# SwarmContext fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_context() -> SwarmContext:
    return SwarmContext(
        task_id="test-task-001",
        task_description="Add input validation to the login endpoint.",
        platform=Platform.CLAUDE_CODE,
        relevant_files=[],
        previous_outputs=[],
        constraints={"token_budget": 8000, "quality_gate_strict": True},
    )


# ---------------------------------------------------------------------------
# StructuredResult fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def done_result() -> StructuredResult:
    return StructuredResult(
        task_id="test-task-001",
        role="implementer",
        status=TaskStatus.DONE,
        summary="Added validation. Three files modified.",
    )


@pytest.fixture()
def escalated_result() -> StructuredResult:
    return StructuredResult(
        task_id="test-task-001",
        role="implementer",
        status=TaskStatus.ESCALATED,
        summary="Could not proceed.",
        escalation_reason="Spec is contradictory on error response format.",
    )


# ---------------------------------------------------------------------------
# Fake driver factory
# ---------------------------------------------------------------------------


def make_fake_driver(spec: AgentSpec, result: StructuredResult) -> MagicMock:
    """Return a mock driver whose invoke() returns the given result."""
    driver = MagicMock()
    driver.role = str(spec.role)
    driver.invoke = AsyncMock(return_value=result)
    return driver


@pytest.fixture()
def fake_driver_factory(done_result: StructuredResult):
    def _factory(spec: AgentSpec, result: StructuredResult | None = None) -> MagicMock:
        return make_fake_driver(spec, result or done_result)

    return _factory


# ---------------------------------------------------------------------------
# Temp repo fixture (creates a minimal fake repo on disk)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    """Create a minimal fake Python repo for analyzer tests."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "class AuthService:\n    def validate_token(self, token: str) -> bool:\n        return True\n"
    )
    (tmp_path / "tests" / "test_auth.py").write_text(
        "def test_validate_token():\n    from src.main import AuthService\n    assert AuthService().validate_token('x')\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fake-repo"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: CI\n")
    (tmp_path / "playwright.config.ts").write_text("export default {};\n")
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "login.feature").write_text(
        "Feature: Login\n  Scenario: Valid credentials\n    Given I am on the login page\n"
    )
    return tmp_path
