"""
drivers/codex.py — Driver for OpenAI Codex / GPT-4o family.

Handles:
- chat completions endpoint (Codex CLI uses the same underlying API)
- Structured outputs via response_format: json_schema
- o-series reasoning models (o1, o3, o4-mini) vs standard models
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

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o"
MAX_TOKENS = 8096

# Reasoning models use max_completion_tokens instead of max_tokens
# and do not support system role (use developer role instead)
REASONING_MODELS = {"o1", "o1-mini", "o1-preview", "o3", "o3-mini", "o4-mini"}


class CodexDriver(BaseAgentDriver):
    """
    Driver for OpenAI Codex / GPT-4o.

    Config keys:
        model               — model string (default: gpt-4o)
        max_tokens          — output token limit (default: 8096)
        structured_outputs  — use JSON schema enforcement (default: True)
    """

    def __init__(self, spec: AgentSpec, api_key: str | None = None, **kwargs: object) -> None:
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not resolved_key:
            raise DriverError(
                "OPENAI_API_KEY not set. "
                "Provide api_key= or set the environment variable."
            )
        super().__init__(spec, resolved_key, **kwargs)
        self._model: str = str(kwargs.get("model", DEFAULT_MODEL))
        self._max_tokens: int = int(kwargs.get("max_tokens", MAX_TOKENS))  # type: ignore[arg-type]
        self._structured_outputs: bool = bool(kwargs.get("structured_outputs", True))
        self._is_reasoning_model: bool = any(
            self._model.startswith(m) for m in REASONING_MODELS
        )

    # ------------------------------------------------------------------
    # BaseAgentDriver implementation
    # ------------------------------------------------------------------

    def _build_messages(self, context: SwarmContext) -> list[dict]:
        system = self._build_system_prompt(context)

        file_block = "\n\n".join(
            f"### {f.path}\n```{f.language}\n{f.content}\n```"
            for f in context.relevant_files
        )
        prior_block = "\n\n".join(
            f"**{o.role}** ({o.status}): {o.summary}"
            for o in context.previous_outputs
        )

        user_content = (
            f"Task: {context.task_description}\n\n"
            + (f"## Prior agent outputs\n{prior_block}\n\n" if prior_block else "")
            + (f"## Relevant files\n{file_block}\n\n" if file_block else "")
            + "Return your response as a single valid JSON object matching the StructuredResult schema."
        )

        # Reasoning models use "developer" role for system-level instructions
        system_role = "developer" if self._is_reasoning_model else "system"
        return [
            {"role": system_role, "content": system},
            {"role": "user", "content": user_content},
        ]

    async def _call_api(self, messages: list[dict], context: SwarmContext) -> str:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        body: dict = {
            "model": self._model,
            "messages": messages,
        }

        # Reasoning models use max_completion_tokens
        if self._is_reasoning_model:
            body["max_completion_tokens"] = self._max_tokens
        else:
            body["max_tokens"] = self._max_tokens

        # Enforce structured JSON output when supported
        if self._structured_outputs and not self._is_reasoning_model:
            body["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(OPENAI_API_URL, headers=headers, json=body)

        if resp.status_code == 429:
            raise RateLimitError(f"OpenAI rate limit: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise DriverError(f"OpenAI API error {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise MalformedResponseError(
                f"Unexpected Codex response shape: {exc}"
            ) from exc

    def _parse_response(self, raw: str, context: SwarmContext) -> StructuredResult:
        return self._parse_json_result(raw, context)
