# Gemini Project Instructions

This project uses a structured agent swarm. When working on any non-trivial task, coordinate through the specialist agents defined in `agents/` rather than doing everything in a single context.

## Model selection

Choose the right Gemini model for the task at hand:

| Use this model | When |
|---|---|
| `gemini-3-flash` (Gemini 3 Flash) | Implementation, QA, code review, daily coding, fast task automation |
| `gemini-3.1-pro` (Gemini 3.1 Pro) | Orchestrator tie-breaking, long-horizon bug triage, cross-module architecture, security audits, heavy multimodal files |

## Context Optimization (Implicit Caching)

Gemini APIs (AI Studio and Vertex) often employ implicit caching for static prefixes. To maximize cache hits and lower costs:
- If provided with large context files (like full specs), read them early and do not ask to re-fetch them.
- Batch context-heavy tasks per session.

## Agent roster

| Agent | Invoke for | Optimized model |
|---|---|---|
| orchestrator | Coordinating the full swarm, task decomposition | Gemini 3.1 Pro |
| architect | System design, API contracts, data models, tech choices | Any |
| implementer | Writing, editing, and refactoring code | Gemini 3 Flash |
| qa-engineer | Writing and running tests, coverage analysis | Gemini 3 Flash |
| reviewer | Code review, security audit, pre-merge checks | Gemini 3.1 Pro (Security) / Flash (Routine) |
| debugger | Diagnosing and fixing bugs and regressions | Gemini 3.1 Pro |

## Orchestration rules

- Always prioritize the `StructuredResult` contract over conversational output.
- Break multi-step work into atomic sub-tasks and track them.
- If a task is ambiguous, use the `escalation_reason` field to ask the user.
- Review agent output against its quality gates before accepting it.

## Response format

<!-- Token efficiency: native terse output mode -->
Terse. Technical substance exact.
Drop: filler, pleasantries, articles, hedging, restatements.
Compress only prose. Code blocks, diffs, JSON, commands: unchanged.

## Code quality baseline

These apply across all agents and all languages:

- No hardcoded secrets, credentials, or environment-specific URLs
- No debug logging left in production paths
- All new code has at least one test covering the primary behaviour
- Error cases are handled explicitly — no silent failures
- Leverage native Gemini multimodal reasoning if UI inputs or diagrams are given

## Workflow playbooks

Structured playbooks for common tasks are in `.agent-swarm/workflows/`:

- `feature-dev.md` — end-to-end feature development
- `bug-fix.md` — systematic debugging workflow
- `code-review.md` — pre-merge review process
- `test-generation.md` — adding test coverage to existing code
