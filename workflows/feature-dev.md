# Workflow: Feature development

Use this playbook when building a new feature end-to-end. Follow the sequence. Do not skip steps.

---

## Phase 1 — Clarify (orchestrator)

Before any agent is invoked:

- [ ] Can you state the feature in one sentence?
- [ ] Do you know the acceptance criteria?
- [ ] Is there an existing pattern in the codebase this should follow?
- [ ] Are there dependencies (other PRs, schema migrations, config changes)?

If any answer is "no", ask the user before proceeding. One question at a time.

---

## Phase 2 — Design (architect)

Invoke the `architect` agent with:
- The one-sentence feature description
- The acceptance criteria
- Pointers to relevant existing code

Architect must produce:
- Recommended approach with rationale
- Data model changes (if any)
- API contract (if any)
- Module/file structure for the implementation
- Identified security surface

**Gate:** Do not proceed until the architect output is accepted by the user or orchestrator.

---

## Phase 3 — Implement (implementer)

Invoke the `implementer` agent with:
- The architect's full output as the spec
- Pointers to files that will be affected

Implementer must produce:
- All code changes, wired end-to-end
- No TODOs, no stubs, no hardcoded values
- Linter-clean output

**Gate:** Do not proceed until linter passes and the implementer confirms end-to-end wiring.

---

## Phase 4 — Test (qa-engineer)

Invoke the `qa-engineer` agent with:
- The architect's spec (not the implementation — test the spec)
- The implementer's list of changed files

QA must produce:
- Unit tests covering all new logic
- At least three failure/edge case tests
- Boundary value tests
- Full suite run with passing results

**Gate:** All tests must pass. Any defects route to Phase 4.5.

---

## Phase 4.5 — Debug (debugger) [conditional]

If QA reports defects, invoke the `debugger` agent with:
- The failing test name and output
- The implementer's change summary

Debugger must produce:
- Root cause identification
- Minimal fix
- Confirmation that the previously failing test now passes
- Confirmation that no previously passing tests now fail

Then return to Phase 4.

---

## Phase 5 — Review (reviewer)

Invoke the `reviewer` agent with:
- The full list of changed files from the implementer and debugger

Reviewer must produce:
- Severity-labelled findings
- Concrete fix suggestions for all blockers and majors
- Explicit security surface evaluation

**Gate:** No blockers may remain. Majors require user or orchestrator sign-off.

---

## Phase 6 — Synthesise (orchestrator)

- [ ] All phases complete
- [ ] No open blockers
- [ ] Todo list fully checked off
- [ ] User notified of any deferred minor/nit items

Return a summary: what was built, files changed, tests added, any deferred items.
