# Gemini 3 Flash Configuration Overlay (`gemini-3-flash`)

This file is a supplemental instruction overlay for **Gemini 3 Flash**. It applies on top of `GEMINI.md` when the active model is from the Flash tier.

---

## When to use this model

Gemini 3 Flash is the **high-speed daily driver** — the right choice for:

- Routine coding, refactoring, and implementation tasks
- Unit test generation and quality assurance
- Simple, straightforward bug fixes
- Sub-tasks within a highly structured workflow
- Tasks where rapid execution and cost-efficiency are critical

**Do not** use Gemini 3 Flash for:
- Resolution of conflicting conclusions from multiple agents (use Pro)
- Complex architectural design spanning multiple disparate modules (use Pro)
- Advanced security vulnerability discovery (use Pro)

---

## Agentic loop guidance

Gemini 3 Flash excels at rapid iterations. Maintain tight loops:

1. Execute the sub-task focusing precisely on the prompt boundaries.
2. Rely on the orchestrator to manage state between sub-tasks.
3. Keep descriptions terse.

---

## Output efficiency

The driver native concise-output mode is active. Gemini 3 Flash will respond tersely in the `summary` fields.

The following fields are **never** compressed (always full fidelity):
- `diffs[]` — file change content
- `findings[]` — review findings
- `suggested_commands[]` — shell commands
- `payload` — arbitrary structured data

Only string prose in the `summary` should follow the caveman-inspired terse structure.
