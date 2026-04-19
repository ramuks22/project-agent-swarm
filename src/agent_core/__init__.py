"""
agent_core — Installable agent swarm configuration and orchestration package.

Public API:
    analyze()           — introspect a repo and get RepoMetadata + recommended roles
    build_context()     — build a SwarmContext for an agent invocation
    run_sequential()    — run a chain of agents sequentially
    run_parallel()      — run independent agents concurrently
    register_driver()   — register a custom platform driver

Configuration:
    SwarmConfig         — top-level config loaded from swarm.yaml
    AgentSpec           — definition of a single agent role
    SwarmContext        — explicit state transfer object (the comms protocol)
    StructuredResult    — the only valid return type from any driver
"""

from agent_core.orchestrator import (
    build_context,
    register_driver,
    run_parallel,
    run_sequential,
)
from agent_core.repo_analyzer import analyze, load_role_templates
from agent_core.schemas import (
    AgentOutput,
    AgentRole,
    AgentSpec,
    FileDiff,
    FileSnapshot,
    Platform,
    QualityGate,
    RepoMetadata,
    ReviewFinding,
    Severity,
    StructuredResult,
    SwarmConfig,
    SwarmContext,
    TaskStatus,
    ToolPermission,
)

__all__ = [
    "analyze", "load_role_templates", "build_context", "run_sequential",
    "run_parallel", "register_driver",
    "AgentRole", "AgentSpec", "AgentOutput", "FileDiff", "FileSnapshot",
    "Platform", "QualityGate", "RepoMetadata", "ReviewFinding", "Severity",
    "StructuredResult", "SwarmConfig", "SwarmContext", "TaskStatus", "ToolPermission",
]
