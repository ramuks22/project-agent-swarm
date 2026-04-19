# Workflow: Bug fix

Use this playbook when something is broken and the root cause is unknown. The debugger leads; other agents support.

---

## Phase 1 — Triage (orchestrator)

Collect before dispatching:

- [ ] What is the observed behaviour?
- [ ] What is the expected behaviour?
- [ ] Is this reproducible? If so, what are the exact steps?
- [ ] When did it start? Was there a recent change?
- [ ] What is the severity? (production down / degraded / cosmetic)

If severity is production-down, skip design review — fix first, clean up after.

---

## Phase 2 — Diagnose and fix (debugger)

Invoke the `debugger` agent with all triage information.

Debugger must follow this sequence exactly:
1. Reproduce the failure
2. Isolate to a minimal scope
3. Form a hypothesis
4. Verify the hypothesis
5. Fix the root cause (not the symptom)
6. Confirm the fix resolves the failure
7. Add a regression test

Debugger must produce:
- Exact root cause statement
- Minimal code fix
- Regression test that would have caught this
- List of related patterns in the codebase to audit

**Gate:** The regression test must pass. Previously passing tests must not break.

---

## Phase 3 — Audit related patterns (debugger or reviewer) [conditional]

If the debugger identified related patterns at risk, invoke `reviewer` on those files.

Reviewer scope: look only for the same class of bug. Do not conduct a general review.

---

## Phase 4 — Review the fix (reviewer)

Invoke `reviewer` with only the files changed by the debugger.

Reviewer focus:
- Does the fix address the root cause or mask a symptom?
- Does the regression test actually test the failure mode (not a tautology)?
- Any new security surface introduced?

---

## Phase 5 — Synthesise (orchestrator)

- [ ] Defect is fixed and verified
- [ ] Regression test added
- [ ] Related patterns reviewed
- [ ] User notified of root cause and fix summary
- [ ] Any discovered related risks reported as follow-up items
