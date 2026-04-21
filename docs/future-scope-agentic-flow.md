# Future Scope: Real Agentic Flow and Reliability Hardening

## Title

Real Agentic Flow and Reliability Hardening

## Objective

Track the remaining work to mature the autonomous orchestration path into a
production-ready agentic SDLC flow while preserving the legacy fixed workflow
chain mode.

## Why Now

The codebase now contains the opt-in autonomous coordinator flow and the key
reliability fixes that unblock it, but the longer-term scope still includes
hardening, deeper execution coverage, and rollout work that should be tracked
explicitly instead of being left implicit.

## Confirmed Gaps

### Repo scope defect

Finding 1 (apps/api/main.py:103-108) [added]
[P1] API scopes every run to a 10-file Python subset

execute_swarm_inner() ignores repo analysis and hard-codes src/**/*.py with a 10-file cap. That means agents never see tests, configs, docs, infra, or any non-Python code, so the advertised repo-wide and multi-language workflows cannot run end-to-end. A one-call SDLC flow will miss the artifacts it needs for design, testing, and review.

### Missing real orchestration loop

Finding 2 (src/agent_core/cli.py:359-368) [added]
[P1] Workflow runner cannot honor the documented orchestration loop

The docs describe orchestrator-led clarify, gate, debug, and synthesize phases, but the executable workflow map is a fixed one-pass list. feature-dev never schedules orchestrator or the conditional debugger loop, so the current implementation cannot re-plan, bounce failures back to implementer, or wait on acceptance gates. That is the main blocker to true single-call SDLC automation.

### Parallel conflict gate defect

Finding 3 (src/agent_core/orchestrator.py:400-420) [added]
[P2] Parallel conflict handling never escalates

run_parallel() promises to escalate contradictory findings, but _detect_conflicts() only logs and returns. In strict mode the caller still receives a normal result list, so downstream code can treat mutually inconsistent reviewer outputs as success. If you want an orchestrator to trust parallel workers, this needs to become a real gate.

### HTTP test collection defect

Finding 4 (tests/test_driver_http.py:79-80) [added]
[P2] HTTP test module crashes before the skip guard applies

When respx is not installed, the import guard sets _HAS_RESPX=False, but the test methods are still decorated with @respx.mock. That raises NameError during collection, so pytest -q fails before the skip marker can help. I verified this locally: the full suite errors in collection on this file.

### Gemini env var doc/runtime mismatch

Finding 5 (docs/QUICKSTART.md:21-25) [added]
[P3] Gemini setup docs use the wrong environment variable

The quickstart tells users to export GOOGLE_API_KEY, but the runtime checks GEMINI_API_KEY. As written, a user can follow the docs exactly and still fail to start Gemini-backed runs. The same mismatch appears in docker-compose, so this will create avoidable setup noise.

## Milestone 1: Coordinator Loop

- Keep `agent-core run` and `/swarm/run` as the legacy fixed-chain path.
- Keep `agent-core auto` and `/swarm/auto` as the opt-in autonomous path.
- Harden clarification, approval, reroute, and resume behavior.
- Expand autonomous API and SSE coverage.
- Validate repo-wide context selection across larger mixed-language repos.

## Milestone 2: Autonomous Execution

- Expand diff-application robustness and failure recovery.
- Improve allowlisted command detection by repo type and test framework.
- Add interruption-safe resume for in-flight executor work.
- Improve observability for executor stdout/stderr and command provenance.

## API and CLI Changes

- `agent-core auto`
- `POST /swarm/auto`
- `POST /swarm/approval/{task_id}`
- Expanded `GET /swarm/status/{task_id}`

## State Model Changes

- `ExecutionPlan`
- `PlanStep`
- `GateRecord`
- `SwarmRunState`
- `ApprovalMode`
- `RunPhase`

## Risks and Safeguards

- Autonomous mode remains opt-in.
- Major-gate approvals remain configurable.
- Clarification remains blocking for ambiguous work.
- Executor stays behind an explicit flag.
- Unsafe or non-allowlisted commands escalate instead of running.

## Acceptance Criteria

- Autonomous runs can plan, pause, resume, reroute, and complete deterministically.
- Legacy fixed workflows remain backward-compatible.
- Repo-wide context selection includes non-Python files when relevant.
- Strict parallel conflicts escalate instead of silently succeeding.
- `pytest -q` does not fail during collection when `respx` is absent.
- Gemini setup works with `GEMINI_API_KEY`, with `GOOGLE_API_KEY` only as a deprecated fallback.

## Open Questions

- When should autonomous mode become the default path instead of opt-in?
- How much executor capability should be enabled by default after rollout?
- Should clarification answers get a dedicated endpoint separate from approval comments?

## Status

- Overall: Proposed
- M1: Not Started
- M2: Not Started
- Owner: Unassigned
- Target Mode: Opt-in autonomous flow
