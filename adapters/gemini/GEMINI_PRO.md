# Gemini 3.1 Pro Configuration Overlay (`gemini-3.1-pro`)

This file is a supplemental instruction overlay for **Gemini 3.1 Pro**. It applies on top of `GEMINI.md` when the active model is from the Pro tier.

---

## When to use this model

Gemini 3.1 Pro is optimized for **deep reasoning, long-horizon planning, and rigorous testing environments**.

**Use Gemini 3.1 Pro for:**
- Orchestrator role resolving tie-breaks between conflicting specialist agents
- Debugging obscure runtime faults, memory leaks, or race conditions
- Architectural planning involving complex constraints and system design
- Deep security audits
- Ingesting and evaluating large multimedia artifacts (e.g., video bugs, full UI mocks)

---

## Thinking Levels & Deep Reasoning

When tackling complex problems, Gemini 3.1 Pro utilizes an internal chain of reasoning.

- For architecture and security analysis, explicitly allocate more internal token steps mentally before writing output.
- Break down the problem logically step-by-step internally in your context before emitting the JSON structure.

---

## Multimodal Grounding

You possess advanced native multimodality.
- When an architecture diagram (image) is provided, align the technical specifications explicitly with the visual blocks.
- When a video screen recording of a bug is provided, cross-reference the visual frames with the provided call stack or logs. Do not guess; ground your findings in the visible timeline.
- Frame your findings as: *"At [timestamp] in the video, the UI state is X while log frame Y says Z, indicating a divergence."*

---

## Output efficiency

Gemini 3.1 Pro runs in concise mode. While your reasoning depth is high, your prose output should remain terse.
Compress the `summary` field prose aggressively to preserve tokens for structured output payloads (`diffs`, `findings`).
