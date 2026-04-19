# Changelog

## [0.1.0] — Initial release

### Package

- `agent_core` is now an installable Python package (`pip install agent-core` or `pip install -e .agent-swarm/`)
- `pyproject.toml` at repo root with `hatchling` build backend
- `agent-core` CLI entry point with `init`, `analyze`, `validate`, `run` subcommands

### Architecture decisions

**Communication protocol: `SwarmContext`**
All state between agents is carried explicitly in a `SwarmContext` Pydantic model.
Drivers are stateless — one `SwarmContext` in, one `StructuredResult` out.
No shared memory, no implicit side channels, no ambient state between calls.

**Structured outputs: `StructuredResult`**
The only valid return type from any driver. Diffs, findings, suggested commands,
and escalation reasons are all typed fields. Open-ended string generation is
prohibited at the driver boundary. Host repo pipelines consume typed objects.

**Adapter pattern: `BaseAgentDriver`**
Three abstract methods (`_build_messages`, `_call_api`, `_parse_response`) and
one public `invoke()` that handles retry, logging, and result validation.
Adding a new platform requires one new file — nothing else changes.

**Dynamic role discovery: `repo_analyzer`**
No agent roles are hardcoded. The `repo_analyzer` inspects languages, frameworks,
test frameworks, CI systems, and documentation to generate `AgentSpec` instances
appropriate to the host repository. The `agents/` markdown files are templates
used for system prompt construction, not a fixed role registry.

**Token budget: `context_optimizer`**
Files are scored by relevance (recency, path/content term match, role-specific
signals, error trace symbols) before being sliced to the token budget.
Falls back to character-ratio estimation when tiktoken cannot reach its CDN
(air-gapped, CI, sandbox environments).

**Quality gates and escalation**
`EscalationPolicy.max_retries` controls retry count.
`quality_gate_strict=True` halts the swarm on any gate failure.
Gates with `eval_expr` are mechanically evaluated against `StructuredResult`.
Parallel runs detect conflicting severity assessments and log warnings.

### Drivers

- `ClaudeDriver` — Anthropic Claude (claude-sonnet-4-5 default), prompt caching beta header, system/messages split
- `CodexDriver` — OpenAI GPT-4o / o-series; reasoning models use `max_completion_tokens` and `developer` role
- `GeminiDriver` — Google Gemini AI Studio + Vertex AI; `responseMimeType: application/json` for controlled generation

### Markdown layer (for file-based tools)

- `adapters/claude-code/` — `CLAUDE.md` + `.claude/agents/*.md` for Claude Code sub-agent protocol
- `adapters/codex/` — `AGENTS.md` for Codex CLI
- `adapters/generic/` — `system-prompt.md` for copy-paste into any tool
- `agents/` — role definition templates (orchestrator, architect, implementer, qa-engineer, reviewer, debugger)
- `workflows/` — phase-gated playbooks (feature-dev, bug-fix, code-review, test-generation)

### Tests

- `test_schemas.py` — Pydantic model constraints
- `test_repo_analyzer.py` — dynamic role discovery
- `test_context_optimizer.py` — relevance scoring and token slicing
- `test_orchestrator.py` — sequential/parallel execution, state accumulation, escalation halting
- `test_drivers.py` — base driver mechanics (retry, quality gates, JSON parsing)
- `test_driver_http.py` — HTTP integration tests for all three drivers via `respx`

### Known limitations

- `tiktoken` falls back to character-ratio estimation when its CDN is unreachable.
  This is intentional for CI/air-gapped environments. Estimation accuracy: ±15%.
- Parallel execution requires explicit `agents` in `swarm.yaml` (validated by Pydantic).
- Gemini Vertex AI requires `google-auth` for OAuth2 — not bundled as a dependency.
  Set `GOOGLE_ACCESS_TOKEN` environment variable or integrate `google.auth` in production.
- `agent-core run` uses a fixed workflow→role mapping. For custom workflows, use the Python API directly.
