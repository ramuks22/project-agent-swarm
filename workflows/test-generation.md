# Workflow: Test generation

Use this playbook to add test coverage to existing untested or under-tested code. Works on any module, class, or function set.

---

## Phase 1 — Coverage audit (qa-engineer)

Invoke `qa-engineer` with:
- The target module or file list
- The existing test files (if any)

QA must produce:
- Coverage map: which behaviours have tests, which do not
- Prioritised list of gaps (highest risk first)
- Identification of any tautology tests already present

---

## Phase 2 — Read the spec or reverse-engineer intent (orchestrator + architect)

If there is no spec for the code under test:
- Invoke `architect` to document the inferred interface contract from the code
- This becomes the spec the qa-engineer tests against

If a spec exists: skip to Phase 3.

---

## Phase 3 — Write tests (qa-engineer)

Invoke `qa-engineer` with:
- The coverage gaps from Phase 1
- The spec from Phase 2 (or the original spec)
- The existing test framework and conventions

QA must produce:
- Tests covering all identified gaps
- Primary success path
- At least three failure/edge cases per function
- Boundary values
- Mocked external dependencies

---

## Phase 4 — Run and verify (qa-engineer)

- [ ] Full test suite runs
- [ ] New tests pass
- [ ] No previously passing tests broken
- [ ] Any flaky tests identified and flagged

---

## Phase 5 — Review test quality (reviewer)

Invoke `reviewer` with the new test files only.

Reviewer focus:
- Are assertions specific (not just `assert result is not None`)?
- Do test names read as plain-English assertions?
- Are there tautology tests that would pass regardless of the implementation?
- Are mocks set up correctly (not just silencing calls)?

---

## Phase 6 — Synthesise (orchestrator)

- [ ] Coverage gaps addressed
- [ ] All tests passing
- [ ] Test quality reviewed
- [ ] User notified of remaining gaps (if any could not be addressed without refactoring)
