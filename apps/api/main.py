from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_core.context_optimizer import get_eligible_candidates
from agent_core.orchestrator import resume_autonomous, run_autonomous, run_sequential
from agent_core.persistence import get_default_state_store
from agent_core.registry import get_default_registry
from agent_core.repo_analyzer import analyze
from agent_core.schemas import (
    ApprovalMode,
    GateDecision,
    Platform,
    StateStoreType,
    SwarmConfig,
    TaskStatus,
)
from agent_core.security.prompt_guard import protect_prompt, scan_for_injection

# L-01: Configurable SSE queue TTL — resolved at startup so a bad value fails fast.
# Default: 3600 s (1 h) — covers long-running autonomous sessions plus follow-up approvals.
try:
    _QUEUE_TTL_SECONDS: int = int(os.environ.get("AGENT_SWARM_QUEUE_TTL_SECONDS", "3600"))
    if _QUEUE_TTL_SECONDS <= 0:
        raise ValueError("TTL must be a positive integer")
except (ValueError, TypeError) as _ttl_err:
    raise ValueError(
        f"AGENT_SWARM_QUEUE_TTL_SECONDS is invalid: {_ttl_err}. "
        "Set it to a positive integer (seconds) or remove it to use the default 3600."
    ) from _ttl_err

REPO_ROOT = Path(".").resolve()

app = FastAPI(title="Agent Swarm API", version="0.2.0")

event_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
task_api_keys: dict[str, str] = {}


class SwarmRequest(BaseModel):
    task_description: str = Field(..., description="The coding task or objective")
    roles: list[str] = Field(
        default=["architect"],
        description="List of agent roles to sequentially execute",
    )
    platform: Platform = Platform.GEMINI
    api_key: str = Field(..., description="Platform API Key (Gemini, Claude, or OpenAI)")
    strict_mode: bool = True


class AutonomousSwarmRequest(BaseModel):
    task_description: str = Field(..., description="The coding task or objective")
    platform: Platform = Platform.GEMINI
    api_key: str = Field(..., description="Platform API Key (Gemini, Claude, or OpenAI)")
    strict_mode: bool = True
    approval_mode: ApprovalMode = ApprovalMode.MAJOR_GATES
    execute: bool = False


class GateDecisionRequest(BaseModel):
    gate_id: str
    decision: GateDecision
    comments: str = ""
    api_key: str | None = None


def _expire_task_state(task_id: str) -> None:
    event_queues.pop(task_id, None)
    task_api_keys.pop(task_id, None)


def _register_queue(task_id: str) -> None:
    event_queues[task_id] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    loop.call_later(_QUEUE_TTL_SECONDS, lambda: _expire_task_state(task_id))


async def _push_event(task_id: str, payload: dict[str, Any]) -> None:
    if task_id in event_queues:
        await event_queues[task_id].put(payload)


def _guard_prompt(task_description: str) -> str:
    if scan_for_injection(task_description):
        raise HTTPException(
            status_code=400,
            detail="Security policy violation: Prompt injection detected.",
        )
    return protect_prompt(task_description)


def _resolve_platform_api_key(platform: Platform) -> str:
    env_map = {
        Platform.CLAUDE_CODE: "ANTHROPIC_API_KEY",
        Platform.CODEX: "OPENAI_API_KEY",
        Platform.OPENAI: "OPENAI_API_KEY",
        Platform.GEMINI: "GEMINI_API_KEY",
        Platform.GENERIC: "ANTHROPIC_API_KEY",
    }
    api_key = os.environ.get(env_map.get(platform, ""), "")
    if api_key:
        return api_key
    if platform == Platform.GEMINI:
        return os.environ.get("GOOGLE_API_KEY", "")
    return ""


def _build_api_config(platform: Platform, strict_mode: bool, *, agents: list[Any] | None = None) -> SwarmConfig:
    config_kwargs: dict[str, Any] = {
        "platform": platform,
        "agents": agents or [],
        "quality_gate_strict": strict_mode,
    }

    redis_url = os.environ.get("AGENT_SWARM_REDIS_URL")
    if redis_url:
        config_kwargs.update(
            {
                "state_store_type": StateStoreType.REDIS,
                "redis_url": redis_url,
            }
        )
        return SwarmConfig(**config_kwargs)

    state_dir = os.environ.get("AGENT_SWARM_STATE_DIR")
    if state_dir:
        config_kwargs.update(
            {
                "state_store_type": StateStoreType.FILE,
                "state_dir": Path(state_dir),
            }
        )

    return SwarmConfig(**config_kwargs)


@app.get("/health")
def health() -> dict[str, Any]:
    registry = get_default_registry()
    all_agents = registry.all()
    return {
        "status": "healthy",
        "registered_agents": len(all_agents),
        "registered_roles": [a.role for a in all_agents],
    }


@app.post("/swarm/run")
async def trigger_swarm(
    request: SwarmRequest, background_tasks: BackgroundTasks
) -> dict[str, str]:
    safe_task_description = _guard_prompt(request.task_description)

    registry = get_default_registry()
    agents = []
    for role in request.roles:
        try:
            agents.append(registry.get(role))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    task_id = str(uuid.uuid4())
    _register_queue(task_id)

    config = _build_api_config(request.platform, request.strict_mode, agents=agents)

    background_tasks.add_task(
        execute_swarm_inner,
        task_id,
        safe_task_description,
        config,
        request.api_key,
    )

    return {"task_id": task_id, "status": "accepted", "events_url": f"/swarm/events/{task_id}"}


@app.post("/swarm/auto")
async def trigger_autonomous_swarm(
    request: AutonomousSwarmRequest, background_tasks: BackgroundTasks
) -> dict[str, str]:
    safe_task_description = _guard_prompt(request.task_description)

    task_id = str(uuid.uuid4())
    _register_queue(task_id)
    task_api_keys[task_id] = request.api_key

    config = _build_api_config(request.platform, request.strict_mode)

    background_tasks.add_task(
        execute_autonomous_swarm_inner,
        task_id,
        safe_task_description,
        config,
        request.api_key,
        request.approval_mode,
        request.execute,
    )

    return {"task_id": task_id, "status": "accepted", "events_url": f"/swarm/events/{task_id}"}


@app.post("/swarm/approval/{task_id}")
async def submit_approval(
    task_id: str, request: GateDecisionRequest, background_tasks: BackgroundTasks
) -> dict[str, str]:
    store = get_default_state_store()
    run_state = await store.load_run_state(task_id)
    if not run_state:
        raise HTTPException(status_code=404, detail="Autonomous run not found")
    if not run_state.pending_gate:
        raise HTTPException(status_code=409, detail="No pending gate for this task")
    if request.gate_id != run_state.pending_gate.gate_id:
        raise HTTPException(status_code=409, detail="Gate mismatch; refresh status before approving")

    api_key = request.api_key or task_api_keys.get(task_id) or _resolve_platform_api_key(run_state.platform)
    if not api_key:
        raise HTTPException(
            status_code=409,
            detail="Approval session expired; resubmit the approval with api_key or set the platform env var",
        )

    task_api_keys[task_id] = api_key
    config = _build_api_config(run_state.platform, run_state.quality_gate_strict)
    background_tasks.add_task(
        execute_autonomous_resume_inner,
        task_id,
        config,
        api_key,
        request.decision,
        request.comments,
    )
    return {"task_id": task_id, "status": "accepted"}


async def execute_swarm_inner(
    task_id: str, desc: str, config: SwarmConfig, api_key: str
) -> None:
    async def on_event(data: Any) -> None:
        await _push_event(task_id, data)

    meta = analyze(REPO_ROOT)
    if not config.agents:
        config = config.model_copy(update={"agents": meta.agent_specs})

    try:
        all_files = get_eligible_candidates(REPO_ROOT)
        chain = [(agent, all_files) for agent in config.agents]
        await run_sequential(
            task_description=desc,
            agent_chain=chain,
            config=config,
            api_key=api_key,
            repo_metadata=meta,
            task_id=task_id,
            on_event=on_event,
        )
    finally:
        await _push_event(task_id, {"type": "swarm_complete", "task_id": task_id})
        task_api_keys.pop(task_id, None)


async def execute_autonomous_swarm_inner(
    task_id: str,
    desc: str,
    config: SwarmConfig,
    api_key: str,
    approval_mode: ApprovalMode,
    execute: bool,
) -> None:
    async def on_event(data: Any) -> None:
        await _push_event(task_id, data)

    meta = analyze(REPO_ROOT)
    if not config.agents:
        config = config.model_copy(update={"agents": meta.agent_specs})

    try:
        state = await run_autonomous(
            task_description=desc,
            config=config,
            api_key=api_key,
            repo_root=REPO_ROOT,
            repo_metadata=meta,
            task_id=task_id,
            approval_mode=approval_mode,
            execute=execute,
            on_event=on_event,
        )
    except Exception as exc:
        await _push_event(
            task_id,
            {"type": "phase_failed", "task_id": task_id, "reason": str(exc)},
        )
        await _push_event(task_id, {"type": "swarm_complete", "task_id": task_id})
        task_api_keys.pop(task_id, None)
        return

    if state.status in {TaskStatus.DONE, TaskStatus.ESCALATED}:
        await _push_event(task_id, {"type": "swarm_complete", "task_id": task_id})
        task_api_keys.pop(task_id, None)


async def execute_autonomous_resume_inner(
    task_id: str,
    config: SwarmConfig,
    api_key: str,
    decision: GateDecision,
    comments: str,
) -> None:
    async def on_event(data: Any) -> None:
        await _push_event(task_id, data)

    store = get_default_state_store()
    if not await store.load_run_state(task_id):
        await _push_event(task_id, {"type": "swarm_complete", "task_id": task_id})
        return

    try:
        state = await resume_autonomous(
            task_id=task_id,
            config=config,
            api_key=api_key,
            repo_root=REPO_ROOT,
            decision=decision,
            comments=comments,
            on_event=on_event,
        )
    except Exception as exc:
        await _push_event(
            task_id,
            {"type": "phase_failed", "task_id": task_id, "reason": str(exc)},
        )
        await _push_event(task_id, {"type": "swarm_complete", "task_id": task_id})
        task_api_keys.pop(task_id, None)
        return

    if state.status in {TaskStatus.DONE, TaskStatus.ESCALATED}:
        await _push_event(task_id, {"type": "swarm_complete", "task_id": task_id})
        task_api_keys.pop(task_id, None)


@app.get("/swarm/events/{task_id}")
async def swarm_events(task_id: str) -> StreamingResponse:
    if task_id not in event_queues:
        raise HTTPException(status_code=404, detail="Task not found or already completed")

    async def event_generator() -> AsyncIterator[str]:
        queue = event_queues[task_id]
        try:
            while True:
                data = await queue.get()
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("type") == "swarm_complete":
                    break
        finally:
            event_queues.pop(task_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/swarm/status/{task_id}")
async def get_status(task_id: str) -> dict[str, Any]:
    store = get_default_state_store()
    run_state = await store.load_run_state(task_id)
    if run_state:
        return run_state.model_dump()

    context = await store.load(task_id)
    if not context:
        raise HTTPException(status_code=404, detail="Status unknown or no checkpoint saved")
    return context.model_dump()
