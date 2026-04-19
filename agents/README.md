# Agent Ownership Registry

This document is the **source of truth** for which agent files are model-specific and which are universal. Read this before editing any file in the `agents/` directory.

---

## Two-tier model

### ✅ Universal agents
These files contain model-agnostic instructions. Any AI coding model (Claude, Gemini, GPT-4o, etc.) can use them without modification.

| File | Role |
|---|---|
| `architect.md` | System design and technical decision-making |
| `implementer.md` | Writing, editing, and refactoring code |
| `qa-engineer.md` | Test design and coverage analysis |

**Rule:** These files must not contain any model-specific tool names, API identifiers, or platform-exclusive capabilities. If you need to add model-specific guidance for a universal agent, create a separate overlay file instead (see below).

---

### 🔵 Claude-Optimized agents

These files contain instructions tuned for Anthropic Claude models. They reference **Claude Code native tools** (`Task`, `TodoRead`, `TodoWrite`) and Claude-specific capabilities (`extended_thinking`, Opus 4.7 self-verification). Do **not** overwrite these files based on model-agnostic registry updates.

Each Claude-Optimized file begins with a `<!-- CLAUDE-OPTIMIZED ... -->` header block that must be preserved on all edits.

| File | Optimized for | Why Claude-specific |
|---|---|---|
| `orchestrator.md` | Opus 4.7 (`claude-opus-4-7`) | Uses `Task`, `TodoRead`, `TodoWrite` — Claude Code native tools with no universal equivalent |
| `reviewer.md` | Sonnet 4.6 (`claude-sonnet-4-5`) | Extended thinking activation on security sections is a Claude-specific API parameter |
| `debugger.md` | Opus 4.7 (`claude-opus-4-7`) | Step 8 self-validate leverages Opus 4.7's native self-verification capability |

---

## CLAUDE-OPTIMIZED tag specification

Every Claude-Optimized file must start at line 1 with a block in exactly this format:

```markdown
<!-- CLAUDE-OPTIMIZED
  Model: <api model string>
  Tools: <comma-separated Claude Code tools used>
  Non-Claude users: <what to do instead>
  Do not modify this CLAUDE-OPTIMIZED block.
-->
```

The registry parser uses `grep -l "CLAUDE-OPTIMIZED"` to enumerate protected files before applying updates.

---

## Protection policy

When adding support for a new model (Gemini, GPT-4o, etc.):

1. **Do not edit** any file containing `<!-- CLAUDE-OPTIMIZED -->`.
2. Create a separate overlay file (e.g., `agents/orchestrator.gemini.md`) if model-specific instructions are needed.
3. Reference the canonical universal agent file as the base; the overlay only adds model-specific sections.

---

## Contribution guide

### Adding a new universal agent

1. Create `agents/<role>.md` following the structure in any existing universal file.
2. Do not include any tool names that are exclusive to one platform.
3. Add the file to the Universal table above.

### Adding a new Claude-optimized agent

1. Create `agents/<role>.md` starting with the `<!-- CLAUDE-OPTIMIZED ... -->` header.
2. Document exactly which Claude-specific tools or capabilities are used, and why they justify the Claude-specific designation.
3. Add the file to the Claude-Optimized table above.
4. If the same role needs to work on other platforms, create `agents/<role>.<platform>.md` as an overlay.

### Token cost practices

<!-- Credit: caveman project (https://github.com/juliusbrussee/caveman) -->
All Claude agent files benefit from the concise-output directive baked into `src/agent_core/drivers/claude.py`. This reduces prose output token usage by ~65% with no accuracy loss.

When writing agent quality gate descriptions or output format sections:
- Keep descriptions precise and short — the model will be in terse mode
- Do not write verbose examples in the quality gate list items
- Structured output fields (`findings[]`, `diffs[]`) are never compressed

---

## Verification

To confirm the ownership registry is consistent with the file system:

```bash
# Should return exactly 3 files
grep -rl "CLAUDE-OPTIMIZED" agents/

# Should return no results (universal files must not have the tag)
grep -l "CLAUDE-OPTIMIZED" agents/architect.md agents/implementer.md agents/qa-engineer.md
```
