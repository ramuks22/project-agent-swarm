# Claude Opus 4.7 Configuration Overlay (`claude-opus-4-7`)

This file is a supplemental instruction overlay for **Claude Opus 4.7** (`claude-opus-4-7`). It applies on top of `CLAUDE.md` when the active model is from the Opus 4.7 tier.

---

## When to use this model

Opus 4.7 is a **high-stakes, low-frequency** model. Use it when correctness and depth matter more than speed or cost.

**Use Opus 4.7 for:**
- Orchestrator role on tasks spanning more than 3 agents
- Debugger role when root cause is non-obvious after one Sonnet 4.6 pass
- Architect role on cross-repo, greenfield, or multi-team designs
- Security audits explicitly flagged as high-stakes by the user
- Any task where parallel agents returned conflicting conclusions and a tie-break is needed

**Do not** use Opus 4.7 for:
- Routine implementation, QA, or single-file edits (Sonnet 4.6 is faster and cheaper)
- Tasks with context < 50K tokens (Sonnet 4.6 handles these equally well)

---

## Self-verification protocol

Opus 4.7 has native self-verification capability. Use it before every `StructuredResult` emission.

**Mandatory self-check sequence:**

1. Draft your complete response (root cause / design / findings).
2. Before emitting, run an internal verification pass — do not skip this:
   - *"Is my conclusion internally consistent with all evidence I was given?"*
   - *"Does my fix / recommendation directly address the root cause I named, or is it treating a symptom?"*
   - *"Have I left any unstated assumptions that the next agent would need to discover?"*
3. If the answer to any question is "no" or "unclear" — revise before emitting.
4. Only then write the `StructuredResult`.

This step is what distinguishes Opus 4.7 from other tiers. Do not skip it.

---

## Extended thinking (`xhigh` effort)

Activate extended thinking with `xhigh` effort when:
- `len(previous_outputs) > 3` in the SwarmContext — a long chain implies high accumulated complexity
- Task is greenfield architectural design with no prior spec
- Parallel agents returned conflicting `findings[]` and you are the tie-breaker

Default to standard thinking for all other tasks to control latency.

---

## 1M token context strategy

Opus 4.7's 1M context window is powerful but must be used intentionally.

**File prioritisation order when context is large:**
1. Files directly involved in the failing test or recent diff (highest priority)
2. Entry-point files (main, index, app, router)
3. Interface definitions and schema files
4. Supporting modules and utilities
5. Tests (lowest priority — include only if testing logic is the focus)

Do not dump the entire repository into context. Use the context optimizer's token budget enforcement.

**Cross-module reasoning:** Unlike Sonnet 4.6, you can hold far-flung module relationships in mind. Actively reference modules mentioned in `RepoMetadata` to catch cross-module consistency issues.

---

## Vision capability (3× resolution)

Opus 4.7 has significantly higher image resolution than previous tiers.

When images are included in `SwarmContext`:
1. Describe the visual element before analysing it: *"This is a UI screenshot showing the login page with a password field and a submit button."*
2. Check visual consistency with the design spec or documented requirements
3. Flag visual regressions explicitly in `findings[]` with severity labels

**Never** assume the next agent in the chain has vision capability — always include a text description in your output.

---

## Conflict resolution (orchestrator tie-breaking)

When Opus 4.7 is acting as orchestrator and parallel agents have returned conflicting conclusions:

1. List both positions explicitly with evidence
2. Apply your self-verification protocol to evaluate each
3. If one is clearly better supported — pick it and explain why
4. If genuinely ambiguous — surface both to the user with `status: escalated` and a specific framing: *"Agent A concluded X because of E1. Agent B concluded Y because of E2. Please decide which priority applies."*

**Never** break ties arbitrarily or default to the first agent's output.

---

## Output efficiency

<!-- Credit: caveman project (https://github.com/juliusbrussee/caveman) -->
Opus 4.7 runs in **Lite concise mode** (≈35% token reduction) in this swarm.
Lite mode preserves slightly more prose than Sonnet's Full mode, because Opus 4.7 explanations for complex reasoning carry more value.

The following fields are **never** compressed (always full fidelity):
- `diffs[]`
- `findings[]`
- `suggested_commands[]`
- `payload`

Only `summary` field prose is compressed.
