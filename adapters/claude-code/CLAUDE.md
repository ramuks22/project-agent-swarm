# Project instructions

This project uses a structured agent swarm. When working on any non-trivial task, coordinate through the specialist agents defined in `agents/` rather than doing everything in a single context.

## Model selection

Choose the right Claude model for the task at hand:

| Use this model | When |
|---|---|
| `claude-sonnet-4-5` (Sonnet 4.6) | Implementation, QA, code review, daily coding, tasks with < 200K tokens of context |
| `claude-opus-4-7` (Opus 4.7) | Orchestrator on tasks spanning > 3 agents, Debugger when root cause is non-obvious after one pass, Architect on cross-repo or greenfield design, high-stakes security audits |

## Agent roster

| Agent | Invoke for | Optimized model |
|---|---|---|
| orchestrator | Coordinating the full swarm, task decomposition | Opus 4.7 |
| architect | System design, API contracts, data models, tech choices | Any |
| implementer | Writing, editing, and refactoring code | Sonnet 4.6 |
| qa-engineer | Writing and running tests, coverage analysis | Sonnet 4.6 |
| reviewer | Code review, security audit, pre-merge checks | Sonnet 4.6 |
| debugger | Diagnosing and fixing bugs and regressions | Opus 4.7 |

## MCP tools (when available)

Claude Code can use these Model Context Protocol tools when configured:

- `mcp__filesystem` — read/write files without raw Bash
- `mcp__github` — search issues, PRs, and code across repos
- `mcp__memory` — persist task context and decisions across sessions (Opus 4.7 preferred)

## Orchestration rules

- Always check the todo list at the start of a session (`TodoRead`)
- Break multi-step work into atomic sub-tasks and track them in the todo list
- Route each sub-task to the correct agent via the `Task` tool
- Review agent output against its quality gates before accepting it
- Surface blockers to the user — never guess through consequential decisions
- Do not mix concerns: an agent that is implementing should not simultaneously review

## Response format

<!-- Token efficiency: inspired by the caveman project (https://github.com/juliusbrussee/caveman) -->
Terse. Technical substance exact.
Drop: filler, pleasantries, articles, hedging, restatements.
Compress only prose. Code blocks, diffs, JSON, commands: unchanged.

## Code quality baseline

These apply across all agents and all languages:

- No hardcoded secrets, credentials, or environment-specific URLs
- No debug logging left in production paths
- No TODO comments left in completed work
- All new code has at least one test covering the primary behaviour
- Error cases are handled explicitly — no silent failures

## Working in this codebase

Before making changes:
1. Read the relevant files first — understand what exists
2. Follow the existing naming conventions, file structure, and patterns
3. Make the smallest change that achieves the goal

## Workflow playbooks

Structured playbooks for common tasks are in `.agent-swarm/workflows/`:

- `feature-dev.md` — end-to-end feature development
- `bug-fix.md` — systematic debugging workflow
- `code-review.md` — pre-merge review process
- `test-generation.md` — adding test coverage to existing code
