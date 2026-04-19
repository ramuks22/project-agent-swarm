# Claude Sonnet 4.6 Configuration Overlay (`claude-sonnet-4-5`)

This file is a supplemental instruction overlay for **Claude Sonnet 4.6** (`claude-sonnet-4-5`). It applies on top of `CLAUDE.md` when the active model is from the Sonnet 4.6 tier.

---

## When to use this model

Sonnet 4.6 is the **daily driver** — the right choice for:

- All implementation, QA, and code review tasks
- Review passes and pre-merge checks
- Any task where context is < 200K tokens
- High-frequency agentic loops (TodoRead → Task → TodoWrite)
- Computer Use tasks that are bounded and reversible (e.g., run tests, apply a patch, format code)

**Do not** use Sonnet 4.6 for:
- Cross-repo greenfield architectural design (use Opus 4.7)
- Root-cause debugging where the cause is non-obvious after one pass (use Opus 4.7)

---

## Extended thinking

Sonnet 4.6 supports extended thinking. Use it selectively — it increases latency and cost.

**Enable** extended thinking when:
- Task modifies ≥ 3 files simultaneously
- Task requires evaluating competing technical approaches
- Security section of a review is being written

**Disable** (default) extended thinking when:
- Single-file edits or additions
- QA test generation from an existing spec
- Simple formatting or refactoring tasks

---

## Agentic loop guidance

Sonnet 4.6 is optimised for sustained agentic sessions. For multi-step tasks:

1. `TodoRead` at session start — understand current state
2. Pick the next incomplete sub-task
3. Complete it using the appropriate specialist agent (`Task`)
4. `TodoWrite` to mark it complete and note any blockers
5. Repeat — do not skip the todo update step or state will drift

---

## Computer Use

Sonnet 4.6 scores highly on computer-use benchmarks. Approved uses in this swarm:

- Running the test suite (`pytest`, `npm test`, etc.)
- Applying a staged patch and confirming it applies cleanly
- Formatting code (`ruff format`, `black`, `prettier`)
- Reading terminal output to verify a build or deploy

**Never** use Computer Use for:
- Destructive operations (delete, purge, drop database)
- Network operations that mutate external state (push, deploy, API calls)
- Any task where a dry-run is not available

---

## Output efficiency

<!-- Credit: caveman project (https://github.com/juliusbrussee/caveman) -->
Sonnet 4.6 concise-output mode is **active by default** in this swarm's driver.
It reduces prose token usage by ~65% with no accuracy loss.

The following fields are **never** compressed (always full fidelity):
- `diffs[]` — file change content
- `findings[]` — review findings (descriptions must be exact)
- `suggested_commands[]` — shell commands
- `payload` — arbitrary structured data

Only `summary` field prose is compressed.
