# Autonomous Flow Guide

The autonomous mode is the coordinator-driven execution path for `agent-core`.
It is exposed through:

- `agent-core auto --task "..."`
- `POST /swarm/auto`
- `POST /swarm/approval/{task_id}` for gate decisions

## What It Does

The orchestrator remains a coordinator. It does not write code directly.
Instead it:

1. Clarifies the task and blocks on ambiguity
2. Locks requirements
3. Dispatches specialist agents
4. Enforces approval gates when configured
5. Reroutes verification failures to the debugger
6. Reroutes blocker/major review findings back to the implementer
7. Synthesizes the final result

## Flow Paths

- Feature: `clarify -> design -> implement -> verify -> review -> finalize`
- Bugfix: `clarify -> debug -> verify -> review -> finalize`
- Review-only: `clarify -> review -> finalize`
- Test-generation: `clarify -> verify -> review -> finalize`

## Approval Modes

- `major_gates`: pauses at requirements lock, design lock when applicable, and release-ready
- `none`: auto-continues through major gates, but still blocks on unresolved ambiguity

API approvals should submit the current `gate_id` from `/swarm/status/{task_id}`.
If the original API key is no longer cached by the server, the approval request
can include `api_key` to resume the run safely.

## Optional Execution Layer

Pass `--execute` or `execute=true` to enable the optional executor layer.
When enabled, it can:

- apply generated diffs
- run allowlisted verification commands
- capture stdout/stderr
- feed execution failures back into the autonomous loop

By default, autonomous mode runs with `execute=false`.
