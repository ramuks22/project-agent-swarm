"""
drivers/claude.py — Driver for Anthropic Claude (Claude Code, API).

Handles:
- claude-sonnet-4-5 (Sonnet 4.6) and claude-opus-4-7 (Opus 4.7) model variants
- Prompt caching headers for large context windows (beta)
- Structured JSON output enforcement via system prompt
- Native concise-output mode that cuts prose token usage by ~65%
  (technique inspired by the caveman project: https://github.com/juliusbrussee/caveman)
"""

from __future__ import annotations

import logging
import os

import httpx

from agent_core.drivers.base import (
    BaseAgentDriver,
    DriverError,
    MalformedResponseError,
    RateLimitError,
)
from agent_core.schemas import AgentSpec, StructuredResult, SwarmContext

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 8096


class ClaudeDriver(BaseAgentDriver):
    """
    Driver for Anthropic Claude models.

    Config keys (pass as kwargs to run_sequential / run_parallel):
        model           — model string (default: claude-sonnet-4-5)
        max_tokens      — output token limit (default: 8096)
        enable_caching  — prompt caching beta header (default: True)
    """

    def __init__(self, spec: AgentSpec, api_key: str | None = None, **kwargs: object) -> None:
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not resolved_key:
            raise DriverError(
                "ANTHROPIC_API_KEY not set. Provide api_key= or set the environment variable."
            )
        super().__init__(spec, resolved_key, **kwargs)
        self._model: str = str(kwargs.get("model", DEFAULT_MODEL))
        self._max_tokens: int = int(kwargs.get("max_tokens", MAX_TOKENS))
        self._enable_caching: bool = bool(kwargs.get("enable_caching", True))
        # concise_mode=True (default) prepends a terse-output directive that cuts
        # prose token usage by ~65% with no accuracy loss — inspired by caveman.
        # Set concise_mode=False to disable for debugging or verbose explanation tasks.
        self._concise_mode: bool = bool(kwargs.get("concise_mode", True))

    # ------------------------------------------------------------------
    # BaseAgentDriver implementation
    # ------------------------------------------------------------------

    # ---------------------------------------------------------------------------
    # Concise-output directive (caveman-inspired, zero dependency)
    # Credit: https://github.com/juliusbrussee/caveman
    # Technique: instructing Claude to drop prose fluff cuts output tokens by
    # ~65% while preserving 100% technical accuracy on structured fields.
    # ---------------------------------------------------------------------------
    _CONCISE_DIRECTIVE = (
        "## Output efficiency rule (mandatory)\n"
        "Terse. Technical substance exact. Drop: filler, pleasantries, hedging, restatements.\n"
        "Fragments OK. Short synonyms preferred.\n"
        "DO NOT compress: code blocks, diffs, JSON payloads, file contents, "
        "finding descriptions, suggested_commands fields.\n"
        "Compress only: summary field prose and free-text explanation prose.\n"
        "Pattern for prose: [thing] [action] [reason]. [next step].\n"
    )

    def _build_messages(self, context: SwarmContext) -> tuple[list[dict], str]:  # type: ignore[override]
        """
        Returns (messages_list, system_str).
        _call_api unpacks this — Claude's API takes system separately from messages.
        """
        base_system = self._build_system_prompt(context)
        if self._concise_mode:
            system = self._CONCISE_DIRECTIVE + "\n---\n\n" + base_system
        else:
            system = base_system

        file_block = "\n\n".join(
            f"### {f.path}\n```{f.language}\n{f.content}\n```" for f in context.relevant_files
        )
        prior_block = "\n\n".join(
            f"**{o.role}** ({o.status}): {o.summary}" for o in context.previous_outputs
        )

        user_content = (
            f"Task: {context.task_description}\n\n"
            + (f"## Prior agent outputs\n{prior_block}\n\n" if prior_block else "")
            + (f"## Relevant files\n{file_block}\n\n" if file_block else "")
            + "Return your response as a single valid JSON object matching the StructuredResult schema."
        )

        return [{"role": "user", "content": user_content}], system

    async def _call_api(self, messages: object, context: SwarmContext) -> str:
        messages_list, system_str = messages  # type: ignore[misc]

        headers: dict[str, str] = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if self._enable_caching:
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"

        if self._enable_caching:
            system_payload: object = [
                {"type": "text", "text": system_str, "cache_control": {"type": "ephemeral"}}
            ]
        else:
            system_payload = system_str

        body: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system_payload,
            "messages": messages_list,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(ANTHROPIC_API_URL, headers=headers, json=body)

        if resp.status_code == 429:
            raise RateLimitError(f"Claude rate limit: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise DriverError(f"Claude API error {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        content_blocks = data.get("content", [])
        text_blocks = [b["text"] for b in content_blocks if b.get("type") == "text"]
        if not text_blocks:
            raise MalformedResponseError("Claude returned no text content blocks")
        return "\n".join(text_blocks)

    def _parse_response(self, raw: str, context: SwarmContext) -> StructuredResult:
        return self._parse_json_result(raw, context)
