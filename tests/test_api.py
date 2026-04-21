from __future__ import annotations

import asyncio
from pathlib import Path

from apps.api.main import app, task_api_keys
from fastapi.testclient import TestClient

from agent_core.schemas import (
    ApprovalMode,
    AutonomousFlow,
    ExecutionPlan,
    GateRecord,
    GateType,
    PlanStep,
    Platform,
    RunPhase,
    StateStoreType,
    SwarmRunState,
    TaskStatus,
)


class _StoreStub:
    def __init__(self, run_state: SwarmRunState | None = None):
        self.run_state = run_state

    async def load_run_state(self, task_id: str):
        if self.run_state and self.run_state.task_id == task_id:
            return self.run_state
        return None

    async def load(self, task_id: str):
        return None


def _sample_run_state(task_id: str = "auto-1") -> SwarmRunState:
    return SwarmRunState(
        task_id=task_id,
        task_description="Add health endpoint",
        platform=Platform.GEMINI,
        status=TaskStatus.PENDING,
        approval_mode=ApprovalMode.MAJOR_GATES,
        execute=False,
        plan=ExecutionPlan(
            flow=AutonomousFlow.FEATURE,
            summary="Autonomous feature flow.",
            steps=[PlanStep(phase=RunPhase.CLARIFY, role="orchestrator", description="Clarify.")],
        ),
        pending_gate=GateRecord(gate_id="g1", gate_type=GateType.REQUIREMENTS_LOCKED),
    )


class TestAPI:
    def test_swarm_auto_accepts_request(self, monkeypatch) -> None:
        async def fake_run_autonomous(*args, **kwargs):
            return _sample_run_state(task_id=kwargs["task_id"]).model_copy(
                update={"status": TaskStatus.DONE, "current_phase": RunPhase.COMPLETED}
            )

        monkeypatch.setattr("apps.api.main.run_autonomous", fake_run_autonomous)

        with TestClient(app) as client:
            response = client.post(
                "/swarm/auto",
                json={
                    "task_description": "Add health endpoint\n- Return 200 OK",
                    "platform": "gemini",
                    "api_key": "test-key",
                    "approval_mode": "none",
                    "execute": False,
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"
        assert "task_id" in data

    def test_swarm_status_prefers_run_state(self, monkeypatch) -> None:
        run_state = _sample_run_state()
        monkeypatch.setattr("apps.api.main.get_default_state_store", lambda: _StoreStub(run_state))

        with TestClient(app) as client:
            response = client.get(f"/swarm/status/{run_state.task_id}")

        assert response.status_code == 200
        assert response.json()["pending_gate"]["gate_type"] == "requirements_locked"

    def test_swarm_approval_accepts_pending_gate(self, monkeypatch) -> None:
        run_state = _sample_run_state(task_id="auto-approval")
        task_api_keys[run_state.task_id] = "test-key"
        monkeypatch.setattr("apps.api.main.get_default_state_store", lambda: _StoreStub(run_state))

        async def fake_resume(*args, **kwargs):
            return run_state.model_copy(update={"status": TaskStatus.DONE, "pending_gate": None})

        monkeypatch.setattr("apps.api.main.resume_autonomous", fake_resume)

        with TestClient(app) as client:
            response = client.post(
                f"/swarm/approval/{run_state.task_id}",
                json={"gate_id": "g1", "decision": "approve", "comments": "Looks good"},
            )

        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        task_api_keys.pop(run_state.task_id, None)

    def test_swarm_approval_rejects_stale_gate(self, monkeypatch) -> None:
        run_state = _sample_run_state(task_id="auto-stale-gate")
        task_api_keys[run_state.task_id] = "test-key"
        monkeypatch.setattr("apps.api.main.get_default_state_store", lambda: _StoreStub(run_state))

        with TestClient(app) as client:
            response = client.post(
                f"/swarm/approval/{run_state.task_id}",
                json={"gate_id": "stale-gate", "decision": "approve", "comments": "Looks good"},
            )

        assert response.status_code == 409
        task_api_keys.pop(run_state.task_id, None)

    def test_swarm_approval_accepts_explicit_api_key(self, monkeypatch) -> None:
        run_state = _sample_run_state(task_id="auto-api-key")
        monkeypatch.setattr("apps.api.main.get_default_state_store", lambda: _StoreStub(run_state))

        async def fake_resume(*args, **kwargs):
            return run_state.model_copy(update={"status": TaskStatus.DONE, "pending_gate": None})

        monkeypatch.setattr("apps.api.main.resume_autonomous", fake_resume)

        with TestClient(app) as client:
            response = client.post(
                f"/swarm/approval/{run_state.task_id}",
                json={
                    "gate_id": "g1",
                    "decision": "approve",
                    "comments": "Resume with supplied key",
                    "api_key": "test-key",
                },
            )

        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        task_api_keys.pop(run_state.task_id, None)

    def test_build_api_config_uses_env_backing_store(self, monkeypatch, tmp_path: Path) -> None:
        from apps.api import main as api_main

        monkeypatch.setenv("AGENT_SWARM_REDIS_URL", "redis://localhost:6379/0")
        config = api_main._build_api_config(Platform.GEMINI, True)
        assert config.state_store_type == StateStoreType.REDIS
        assert config.redis_url == "redis://localhost:6379/0"

        monkeypatch.delenv("AGENT_SWARM_REDIS_URL")
        state_dir = tmp_path / "agent-swarm-state"
        monkeypatch.setenv("AGENT_SWARM_STATE_DIR", str(state_dir))
        config = api_main._build_api_config(Platform.GEMINI, False)
        assert config.state_store_type == StateStoreType.FILE
        assert config.state_dir == state_dir

    async def test_execute_swarm_inner_uses_repo_wide_candidates(
        self, monkeypatch, tmp_path: Path, minimal_spec
    ) -> None:
        from apps.api import main as api_main

        from agent_core.schemas import SwarmConfig

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("def test_x(): assert True\n")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "guide.md").write_text("# guide\n")

        captured = {}

        async def fake_run_sequential(*args, **kwargs):
            captured["chain"] = kwargs["agent_chain"]
            return []

        monkeypatch.setattr(api_main, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(api_main, "run_sequential", fake_run_sequential)
        monkeypatch.setattr(api_main, "analyze", lambda root: _StoreStub())  # type: ignore[arg-type]

        config = SwarmConfig(platform=Platform.CLAUDE_CODE, agents=[minimal_spec])
        api_main.event_queues["legacy-task"] = asyncio.Queue()

        await api_main.execute_swarm_inner("legacy-task", "Do a thing", config, "test-key")

        files = captured["chain"][0][1]
        names = {path.name for path in files}
        assert "main.py" in names
        assert "test_main.py" in names
        assert "guide.md" in names
