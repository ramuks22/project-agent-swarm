# Workflow: Code review

Use this playbook for structured pre-merge review. Produces severity-labelled findings with clear resolution paths.

---

## Phase 1 — Scope (orchestrator)

Before invoking the reviewer:

- [ ] What files are in scope? (changed files only, or broader context?)
- [ ] Is there a spec or design doc this should be reviewed against?
- [ ] Are there specific concerns? (security, performance, correctness)
- [ ] What is the merge deadline pressure?

---

## Phase 2 — Review (reviewer)

Invoke `reviewer` with:
- The list of changed files (read them all)
- The spec or design doc if one exists
- Any specific concerns flagged in Phase 1

Reviewer must produce:
- Overall recommendation: approve / approve with minor changes / request changes
- Severity-labelled findings (blocker, major, minor, nit)
- Concrete fix suggestion for every blocker and major
- Explicit security surface evaluation
- At least one positive observation

---

## Phase 3 — Triage findings (orchestrator)

For each `blocker`:
- Invoke `implementer` to apply the reviewer's suggested fix
- Then re-invoke `reviewer` on the changed file only (not full re-review)

For each `major`:
- Present to user for a fix-now vs. track-as-tech-debt decision
- If fix-now: same path as blocker
- If track: create a tracked todo item

Minors and nits: present to user as a batch with no required action.

---

## Phase 4 — Re-review changed files (reviewer) [conditional]

If any files were changed in Phase 3, invoke reviewer on those files only.
Scope is narrow: confirm the blocker/major is resolved and no new issues introduced.

---

## Phase 5 — Synthesise (orchestrator)

- [ ] No open blockers
- [ ] All majors either fixed or explicitly deferred
- [ ] User has a clear summary: what was approved, what was deferred
- [ ] Any deferred items are tracked
