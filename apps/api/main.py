from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pathlib import Path
from typing import List, Optional, Any
import os
import uuid
import asyncio
import json

from agent_core.orchestrator import run_sequential, run_parallel
from agent_core.schemas import SwarmConfig, AgentSpec, RepoMetadata, Platform
from agent_core.persistence import get_state_store, get_default_state_store
from agent_core.registry import get_default_registry
from agent_core.security.prompt_guard import scan_for_injection, protect_prompt

# L-01: Configurable SSE queue TTL — resolved at startup so a bad value fails fast.
# Default: 3600 s (1 h) — covers ~6 agents × 10 min/agent + 40 min safety headroom.
# Override with:  AGENT_SWARM_QUEUE_TTL_SECONDS=7200 uvicorn ...
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

app = FastAPI(title="Agent Swarm API", version="0.1.0")

event_queues: dict[str, asyncio.Queue] = {}

class SwarmRequest(BaseModel):
    task_description: str = Field(..., description="The coding task or objective")
    roles: List[str] = Field(default=["architect"], description="List of agent roles to sequentially execute")
    platform: Platform = Platform.GEMINI
    api_key: str = Field(..., description="Platform API Key (Gemini, Claude, or OpenAI)")
    strict_mode: bool = True

@app.get("/health")
def health():
    registry = get_default_registry()
    all_agents = registry.all()
    return {
        "status": "healthy",
        "registered_agents": len(all_agents),
        "registered_roles": [a.role for a in all_agents],
    }

@app.post("/swarm/run")
async def trigger_swarm(request: SwarmRequest, background_tasks: BackgroundTasks):
    # 1. SECURITY CHECKS
    if scan_for_injection(request.task_description):
        raise HTTPException(status_code=400, detail="Security policy violation: Prompt injection detected.")
    
    safe_task_description = protect_prompt(request.task_description)

    # 2. RESOLVE AGENTS
    registry = get_default_registry()
    agents = []
    for role in request.roles:
        try:
            agents.append(registry.get(role))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    task_id = str(uuid.uuid4())
    event_queues[task_id] = asyncio.Queue()
    
    # M-01 Fix: Add TTL to prevent memory leaks if client never connects
    loop = asyncio.get_running_loop()
    loop.call_later(_QUEUE_TTL_SECONDS, lambda: event_queues.pop(task_id, None))
    
    config = SwarmConfig(
        platform=request.platform,
        agents=agents,
        quality_gate_strict=request.strict_mode
    )

    background_tasks.add_task(
        execute_swarm_inner, 
        task_id, 
        safe_task_description, 
        agents, 
        config, 
        request.api_key
    )
    
    return {
        "task_id": task_id,
        "status": "accepted",
        "events_url": f"/swarm/events/{task_id}"
    }

async def execute_swarm_inner(task_id: str, desc: str, agents: List[AgentSpec], config: SwarmConfig, api_key: str):
    async def on_event(data: Any):
        if task_id in event_queues:
            await event_queues[task_id].put(data)

    try:
        # Instead of [Path(".")] we gather some python files. 
        # In a real setup, repo_analyzer identifies exactly which files to attach
        all_files = list(REPO_ROOT.glob("src/**/*.py"))[:10]  # Hardcap for POC safety

        chain = [(a, all_files) for a in agents]
        
        await run_sequential(
            task_description=desc,
            agent_chain=chain,
            config=config,
            api_key=api_key,
            task_id=task_id,
            on_event=on_event
        )
    finally:
        if task_id in event_queues:
            await event_queues[task_id].put({"type": "swarm_complete", "task_id": task_id})

@app.get("/swarm/events/{task_id}")
async def swarm_events(task_id: str):
    if task_id not in event_queues:
        raise HTTPException(status_code=404, detail="Task not found or already completed")

    async def event_generator():
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
async def get_status(task_id: str):
    # M-03 Fix: get_default_state_store() honours AGENT_SWARM_STATE_DIR env var
    # and derives the default from SwarmConfig.state_dir — never hardcodes a path.
    store = get_default_state_store()
    context = await store.load(task_id)
    if not context:
        raise HTTPException(status_code=404, detail="Status unknown or no checkpoint saved")
    return context.model_dump()
