"""
schemas.py — Pydantic v2 models for all agent-core data structures.

Design contract:
- Every cross-agent data transfer is a typed model. No raw dicts or strings.
- All models are strict: unknown fields are rejected, not ignored.
- StructuredResult is the ONLY valid return type from any driver. Open-ended
  string generation is prohibited at the driver boundary.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, Field, model_validator


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return str(self.value)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AgentRole(StrEnum):
    """
    Built-in role identifiers. These are not exhaustive — the repo_analyzer
    may discover and register additional roles from the host repository's
    documentation, Makefiles, CI config, etc.

    Treat these as canonical examples, not a closed set.
    """

    ORCHESTRATOR = "orchestrator"
    ARCHITECT = "architect"
    IMPLEMENTER = "implementer"
    QA_ENGINEER = "qa-engineer"
    REVIEWER = "reviewer"
    DEBUGGER = "debugger"


class Platform(StrEnum):
    """
    Supported AI coding platforms. New platforms require only a new driver
    that subclasses BaseAgentDriver — nothing else in the package changes.
    """

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"  # backward compatibility alias for openai
    OPENAI = "openai"
    GEMINI = "gemini"  # Google Gemini / Antigravity family
    GENERIC = "generic"


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    ESCALATED = "escalated"


class StateStoreType(StrEnum):
    FILE = "file"
    REDIS = "redis"
    MEMORY = "memory"


class AutonomousFlow(StrEnum):
    FEATURE = "feature"
    BUGFIX = "bugfix"
    REVIEW_ONLY = "review-only"
    TEST_GENERATION = "test-generation"


class RunPhase(StrEnum):
    CLARIFY = "clarify"
    DESIGN = "design"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    DEBUG = "debug"
    REVIEW = "review"
    FINALIZE = "finalize"
    COMPLETED = "completed"


class ApprovalMode(StrEnum):
    MAJOR_GATES = "major_gates"
    NONE = "none"


class GateType(StrEnum):
    CLARIFICATION_REQUIRED = "clarification_required"
    REQUIREMENTS_LOCKED = "requirements_locked"
    DESIGN_LOCKED = "design_locked"
    RELEASE_READY = "release_ready"


class GateStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto_approved"


class GateDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


# ---------------------------------------------------------------------------
# Agent definition (the thing you configure)
# ---------------------------------------------------------------------------


class ToolPermission(BaseModel, extra="forbid"):
    name: str
    description: str = ""


class QualityGate(BaseModel, extra="forbid"):
    description: str
    # If provided, the orchestrator will eval this expression against the
    # StructuredResult to mechanically verify the gate.
    eval_expr: Optional[str] = None


class EscalationPolicy(BaseModel, extra="forbid"):
    max_retries: int = Field(default=2, ge=0, le=5)
    on_failure: TaskStatus = TaskStatus.ESCALATED
    message_template: str = (
        "Agent '{role}' failed after {attempts} attempt(s). "
        "Reason: {reason}. Escalating to orchestrator."
    )


class AgentSpec(BaseModel, extra="forbid"):
    """
    Platform-agnostic definition of an agent role.

    Role membership is open: the repo_analyzer will generate AgentSpec
    instances dynamically based on what it discovers in the host repo.
    The fields below are the required contract — nothing in this schema
    should be hardcoded to a specific role name.
    """

    name: str = Field(description="Unique identifier for this agent role")
    role: Union[AgentRole, str] = Field(
        description=(
            "Canonical role key. May be a built-in AgentRole value or any "
            "custom string discovered by the repo_analyzer."
        )
    )
    description: str = Field(
        description="When to invoke this agent. Used by the orchestrator to route tasks."
    )
    responsibilities: list[str] = Field(min_length=1)
    quality_gates: list[QualityGate] = Field(min_length=1)
    tools_allowed: list[ToolPermission]
    out_of_scope: list[str] = Field(default_factory=list)
    escalation: EscalationPolicy = Field(default_factory=EscalationPolicy)
    # Output schema: the JSON Schema the driver must conform its response to.
    # If None, the driver uses the default StructuredResult schema.
    output_json_schema: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Swarm state — the explicit state protocol between agents
# ---------------------------------------------------------------------------


class FileSnapshot(BaseModel, extra="forbid"):
    """A single file's content captured at task time."""

    path: Path
    content: str
    language: str = ""
    token_count: int = 0


class AgentOutput(BaseModel, extra="forbid"):
    """Completed output from one agent, carried forward to the next."""

    role: str
    status: TaskStatus
    summary: str
    artifacts: list[str] = Field(
        default_factory=list,
        description="File paths created or modified by this agent",
    )
    findings: list[dict[str, Any]] = Field(default_factory=list)
    structured_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Any additional structured data the next agent should consume",
    )


class SwarmContext(BaseModel, extra="forbid"):
    """
    The explicit state transfer object passed into every driver call.

    This is the answer to the communication protocol question:
    - All state is carried HERE. Drivers are stateless.
    - No driver may persist or mutate state between calls.
    - The orchestrator is responsible for building and updating this object.
    - Context is sliced (relevant_files) to control token budget.
    """

    task_id: str
    task_description: str
    platform: Platform
    relevant_files: list[FileSnapshot] = Field(
        default_factory=list,
        description=(
            "Only the files relevant to this agent's task — not the whole repo. "
            "The orchestrator slices this based on agent role and task scope."
        ),
    )
    previous_outputs: list[AgentOutput] = Field(
        default_factory=list,
        description="Outputs from upstream agents in this task's chain",
    )
    constraints: dict[str, Any] = Field(
        default_factory=dict,
        description="Runtime constraints: token_budget, max_files, allowed_tools, etc.",
    )
    repo_metadata: Optional[RepoMetadata] = None


# ---------------------------------------------------------------------------
# Structured result — the ONLY valid return type from any driver
# ---------------------------------------------------------------------------


class FileDiff(BaseModel, extra="forbid"):
    path: Path
    operation: str  # "create" | "modify" | "delete"
    unified_diff: str  # Standard unified diff format
    explanation: str


class ReviewFinding(BaseModel, extra="forbid"):
    file: str
    line: Optional[int] = None
    severity: Severity
    description: str
    suggestion: Optional[str] = None


class StructuredResult(BaseModel, extra="forbid"):
    """
    The mandatory return type from every driver.

    Drivers MUST NOT return raw strings. All output must be expressed
    in this schema. This is what makes host repo pipelines reliable.
    """

    task_id: str
    role: str
    status: TaskStatus
    summary: str = Field(max_length=500)

    # Code changes
    diffs: list[FileDiff] = Field(default_factory=list)

    # Review/QA findings
    findings: list[ReviewFinding] = Field(default_factory=list)

    # Commands the host repo may choose to run (never auto-executed)
    suggested_commands: list[str] = Field(default_factory=list)

    # Arbitrary structured payload, validated against AgentSpec.output_json_schema
    payload: dict[str, Any] = Field(default_factory=dict)

    # Escalation
    escalation_reason: Optional[str] = None
    next_agent: Optional[str] = None

    @model_validator(mode="after")
    def escalation_requires_reason(self) -> StructuredResult:
        if self.status == TaskStatus.ESCALATED and not self.escalation_reason:
            raise ValueError("Escalated results must include an escalation_reason")
        return self


class ClarificationQuestion(BaseModel, extra="forbid"):
    id: str
    prompt: str
    rationale: str = ""


class PlanStep(BaseModel, extra="forbid"):
    phase: RunPhase
    role: str
    description: str
    status: TaskStatus = TaskStatus.PENDING


class ExecutionPlan(BaseModel, extra="forbid"):
    flow: AutonomousFlow
    summary: str
    requirements: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    clarification_questions: list[ClarificationQuestion] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)


class GateRecord(BaseModel, extra="forbid"):
    gate_id: str
    gate_type: GateType
    status: GateStatus = GateStatus.PENDING
    comments: str = ""


class ExecutorCommandResult(BaseModel, extra="forbid"):
    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""


class ExecutorOutcome(BaseModel, extra="forbid"):
    status: TaskStatus
    applied_paths: list[str] = Field(default_factory=list)
    command_results: list[ExecutorCommandResult] = Field(default_factory=list)
    failure_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Repo metadata — populated by the repo_analyzer
# ---------------------------------------------------------------------------


class RepoMetadata(BaseModel, extra="forbid"):
    """
    What the repo_analyzer discovers about the host repository.
    This drives dynamic agent role assignment — nothing is hardcoded.
    """

    root: Path
    primary_languages: list[str]
    frameworks: list[str]
    test_frameworks: list[str]
    ci_systems: list[str]
    has_docker: bool
    has_migrations: bool
    has_openapi_spec: bool
    module_map: dict[str, list[str]] = Field(
        description="module_name -> [file_paths] — used to scope agent context"
    )
    # Agent roles discovered or inferred from this repo's conventions.
    recommended_roles: list[str] = Field(default_factory=list)

    # Full definitions for every role in recommended_roles.
    # Populated by repo_analyzer.analyze() from custom logic + built-in templates.
    agent_specs: list[AgentSpec] = Field(default_factory=list)


class SwarmRunState(BaseModel, extra="forbid"):
    task_id: str
    task_description: str
    platform: Platform
    status: TaskStatus = TaskStatus.PENDING
    quality_gate_strict: bool = True
    current_phase: RunPhase = RunPhase.CLARIFY
    approval_mode: ApprovalMode = ApprovalMode.MAJOR_GATES
    execute: bool = False
    plan: ExecutionPlan
    pending_gate: Optional[GateRecord] = None
    gate_history: list[GateRecord] = Field(default_factory=list)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    changed_artifacts: list[str] = Field(default_factory=list)
    previous_outputs: list[AgentOutput] = Field(default_factory=list)
    phase_results: list[StructuredResult] = Field(default_factory=list)
    executor_outcomes: list[ExecutorOutcome] = Field(default_factory=list)
    repo_metadata: Optional[RepoMetadata] = None
    completion_summary: str = ""
    escalation_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Swarm config — the top-level config a host repo provides
# ---------------------------------------------------------------------------


class SwarmConfig(BaseModel, extra="forbid"):
    """
    The configuration a host repo provides at runtime.
    Loaded from swarm.yaml (or swarm.json) in the host repo root.
    """

    platform: Platform
    agents: list[AgentSpec] = Field(
        default_factory=list,
        description=(
            "Explicit agent definitions. If empty, the repo_analyzer will "
            "generate these dynamically from repo introspection."
        ),
    )
    token_budget_per_agent: int = Field(
        default=60_000,
        description="Max tokens of context passed to each agent invocation",
    )
    max_parallel_agents: int = Field(
        default=1,
        ge=1,
        le=8,
        description=(
            "Maximum agents running in parallel. Set to 1 for sequential "
            "(safer, easier to debug). Increase only after sequential mode "
            "is stable."
        ),
    )
    quality_gate_strict: bool = Field(
        default=True,
        description=(
            "If True, a failing quality gate halts the swarm and escalates. "
            "If False, findings are logged but execution continues."
        ),
    )
    output_dir: Path = Field(
        default=Path(".swarm/outputs"),
        description="Where StructuredResult JSON files are written for host repo pipelines",
    )
    state_store_type: StateStoreType = Field(
        default=StateStoreType.FILE,
        description="Backend for task checkpointing (file, redis, or memory)",
    )
    state_dir: Path = Field(
        default=Path(".swarm/state"),
        description="Directory for file-based task snapshots",
    )
    redis_url: Optional[str] = Field(
        default=None,
        description="Redis connection URL (required if state_store_type is redis)",
    )

    @model_validator(mode="after")
    def parallel_requires_explicit_agents(self) -> SwarmConfig:
        if self.max_parallel_agents > 1 and not self.agents:
            raise ValueError(
                "Parallel execution requires explicit agent definitions. "
                "Run repo_analyzer first to generate agents, then set max_parallel_agents."
            )
        return self


# Update forward references
SwarmContext.model_rebuild()
SwarmRunState.model_rebuild()
