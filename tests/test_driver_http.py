"""
test_driver_http.py — HTTP integration tests for ClaudeDriver, CodexDriver, GeminiDriver.

Uses `respx` to intercept httpx calls at the transport level.
No real network requests are made. Tests verify:
  - correct request shape (URL, headers, body structure)
  - successful JSON response parsed into StructuredResult
  - rate-limit 429 triggers RateLimitError
  - 4xx errors trigger DriverError
  - malformed response body triggers MalformedResponseError → escalation
  - prompt caching header present when enabled
  - reasoning model uses max_completion_tokens (Codex o-series)
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

try:
    import respx
    _HAS_RESPX = True
except ImportError:
    _HAS_RESPX = False

from agent_core.drivers.base import DriverError, MalformedResponseError, RateLimitError
from agent_core.schemas import (
    AgentSpec,
    QualityGate,
    StructuredResult,
    SwarmContext,
    TaskStatus,
    ToolPermission,
    Platform,
)

pytestmark = pytest.mark.skipif(not _HAS_RESPX, reason="respx not installed")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

DONE_PAYLOAD = {
    "task_id": "t1",
    "role": "implementer",
    "status": "done",
    "summary": "Implementation complete.",
}


def _make_spec(role: str = "implementer") -> AgentSpec:
    return AgentSpec(
        name=role,
        role=role,
        description="Test agent.",
        responsibilities=["Do the thing."],
        quality_gates=[QualityGate(description="Thing done.")],
        tools_allowed=[ToolPermission(name="Read")],
        out_of_scope=[],
    )


def _make_ctx(task_id: str = "t1") -> SwarmContext:
    return SwarmContext(
        task_id=task_id,
        task_description="Add input validation.",
        platform=Platform.CLAUDE_CODE,
        constraints={"token_budget": 8000},
    )


# ---------------------------------------------------------------------------
# ClaudeDriver HTTP tests
# ---------------------------------------------------------------------------


class TestClaudeDriverHTTP:
    @respx.mock
    async def test_successful_request_returns_structured_result(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(
            200,
            json={"content": [{"type": "text", "text": json.dumps(DONE_PAYLOAD)}]},
        ))

        driver = ClaudeDriver(_make_spec(), api_key="test-key", enable_caching=False)
        result = await driver.invoke(_make_ctx())

        assert result.status == TaskStatus.DONE
        assert result.summary == "Implementation complete."

    @respx.mock
    async def test_request_body_structure(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": json.dumps(DONE_PAYLOAD)}]},
            )

        respx.post(ANTHROPIC_API_URL).mock(side_effect=capture)

        driver = ClaudeDriver(_make_spec(), api_key="test-key", model="claude-sonnet-4-5",
                              enable_caching=False)
        await driver.invoke(_make_ctx())

        assert len(captured) == 1
        body = json.loads(captured[0].content)
        assert body["model"] == "claude-sonnet-4-5"
        assert "messages" in body
        assert body["messages"][0]["role"] == "user"
        assert "system" in body

    @respx.mock
    async def test_prompt_caching_header_present_when_enabled(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": json.dumps(DONE_PAYLOAD)}]},
            )

        respx.post(ANTHROPIC_API_URL).mock(side_effect=capture)

        driver = ClaudeDriver(_make_spec(), api_key="test-key", enable_caching=True)
        await driver.invoke(_make_ctx())

        assert "anthropic-beta" in captured[0].headers
        assert "prompt-caching" in captured[0].headers["anthropic-beta"]

    @respx.mock
    async def test_caching_sends_cache_control_in_system(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": json.dumps(DONE_PAYLOAD)}]},
            )

        respx.post(ANTHROPIC_API_URL).mock(side_effect=capture)

        driver = ClaudeDriver(_make_spec(), api_key="test-key", enable_caching=True)
        await driver.invoke(_make_ctx())

        body = json.loads(captured[0].content)
        assert isinstance(body["system"], list)
        assert body["system"][0]["cache_control"] == {"type": "ephemeral"}

    @respx.mock
    async def test_rate_limit_429_raises_rate_limit_error(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(429, text="rate limited"))

        spec = _make_spec()
        from agent_core.schemas import EscalationPolicy
        spec = spec.model_copy(update={"escalation": EscalationPolicy(max_retries=0)})
        driver = ClaudeDriver(spec, api_key="test-key", enable_caching=False)
        result = await driver.invoke(_make_ctx())

        # After 0 retries, escalates
        assert result.status == TaskStatus.ESCALATED

    @respx.mock
    async def test_4xx_error_escalates(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(401, text="unauthorized"))

        spec = _make_spec()
        from agent_core.schemas import EscalationPolicy
        spec = spec.model_copy(update={"escalation": EscalationPolicy(max_retries=0)})
        driver = ClaudeDriver(spec, api_key="bad-key", enable_caching=False)
        result = await driver.invoke(_make_ctx())

        assert result.status == TaskStatus.ESCALATED

    @respx.mock
    async def test_empty_content_blocks_escalates(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(
            200,
            json={"content": []},
        ))

        spec = _make_spec()
        from agent_core.schemas import EscalationPolicy
        spec = spec.model_copy(update={"escalation": EscalationPolicy(max_retries=0)})
        driver = ClaudeDriver(spec, api_key="test-key", enable_caching=False)
        result = await driver.invoke(_make_ctx())

        assert result.status == TaskStatus.ESCALATED

    @respx.mock
    async def test_invalid_json_response_escalates(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "not json at all"}]},
        ))

        spec = _make_spec()
        from agent_core.schemas import EscalationPolicy
        spec = spec.model_copy(update={"escalation": EscalationPolicy(max_retries=0)})
        driver = ClaudeDriver(spec, api_key="test-key", enable_caching=False)
        result = await driver.invoke(_make_ctx())

        assert result.status == TaskStatus.ESCALATED
        assert result.escalation_reason is not None

    @respx.mock
    async def test_api_key_sent_in_header(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": json.dumps(DONE_PAYLOAD)}]},
            )

        respx.post(ANTHROPIC_API_URL).mock(side_effect=capture)

        driver = ClaudeDriver(_make_spec(), api_key="sk-ant-test123", enable_caching=False)
        await driver.invoke(_make_ctx())

        assert captured[0].headers["x-api-key"] == "sk-ant-test123"

    @respx.mock
    async def test_task_description_in_user_message(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": json.dumps(DONE_PAYLOAD)}]},
            )

        respx.post(ANTHROPIC_API_URL).mock(side_effect=capture)

        ctx = SwarmContext(
            task_id="t1",
            task_description="Add rate limiting to /login endpoint",
            platform=Platform.CLAUDE_CODE,
            constraints={"token_budget": 8000},
        )
        driver = ClaudeDriver(_make_spec(), api_key="test-key", enable_caching=False)
        await driver.invoke(ctx)

        body = json.loads(captured[0].content)
        user_content = body["messages"][0]["content"]
        assert "Add rate limiting to /login endpoint" in user_content

    @respx.mock
    async def test_retry_on_429_then_succeed(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        call_count = 0

        def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": json.dumps(DONE_PAYLOAD)}]},
            )

        respx.post(ANTHROPIC_API_URL).mock(side_effect=side_effect)

        # max_retries=2 allows one 429 retry
        spec = _make_spec()
        from agent_core.schemas import EscalationPolicy
        spec = spec.model_copy(update={"escalation": EscalationPolicy(max_retries=2)})
        driver = ClaudeDriver(spec, api_key="test-key", enable_caching=False)
        result = await driver.invoke(_make_ctx())

        assert result.status == TaskStatus.DONE
        assert call_count == 2


# ---------------------------------------------------------------------------
# CodexDriver HTTP tests
# ---------------------------------------------------------------------------


class TestCodexDriverHTTP:
    @respx.mock
    async def test_successful_request(self) -> None:
        from agent_core.drivers.codex import CodexDriver, OPENAI_API_URL

        respx.post(OPENAI_API_URL).mock(return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(DONE_PAYLOAD)}}]},
        ))

        driver = CodexDriver(_make_spec(), api_key="test-key")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.CODEX})
        result = await driver.invoke(ctx)

        assert result.status == TaskStatus.DONE

    @respx.mock
    async def test_standard_model_uses_max_tokens(self) -> None:
        from agent_core.drivers.codex import CodexDriver, OPENAI_API_URL

        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(DONE_PAYLOAD)}}]},
            )

        respx.post(OPENAI_API_URL).mock(side_effect=capture)

        driver = CodexDriver(_make_spec(), api_key="test-key", model="gpt-4o")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.CODEX})
        await driver.invoke(ctx)

        body = json.loads(captured[0].content)
        assert "max_tokens" in body
        assert "max_completion_tokens" not in body

    @respx.mock
    async def test_reasoning_model_uses_max_completion_tokens(self) -> None:
        from agent_core.drivers.codex import CodexDriver, OPENAI_API_URL

        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(DONE_PAYLOAD)}}]},
            )

        respx.post(OPENAI_API_URL).mock(side_effect=capture)

        driver = CodexDriver(_make_spec(), api_key="test-key", model="o4-mini")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.CODEX})
        await driver.invoke(ctx)

        body = json.loads(captured[0].content)
        assert "max_completion_tokens" in body
        assert "max_tokens" not in body

    @respx.mock
    async def test_reasoning_model_uses_developer_role(self) -> None:
        from agent_core.drivers.codex import CodexDriver, OPENAI_API_URL

        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(DONE_PAYLOAD)}}]},
            )

        respx.post(OPENAI_API_URL).mock(side_effect=capture)

        driver = CodexDriver(_make_spec(), api_key="test-key", model="o3-mini")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.CODEX})
        await driver.invoke(ctx)

        body = json.loads(captured[0].content)
        system_msg = next(m for m in body["messages"] if m["role"] in ("system", "developer"))
        assert system_msg["role"] == "developer"

    @respx.mock
    async def test_structured_outputs_json_format(self) -> None:
        from agent_core.drivers.codex import CodexDriver, OPENAI_API_URL

        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(DONE_PAYLOAD)}}]},
            )

        respx.post(OPENAI_API_URL).mock(side_effect=capture)

        driver = CodexDriver(_make_spec(), api_key="test-key", model="gpt-4o",
                             structured_outputs=True)
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.CODEX})
        await driver.invoke(ctx)

        body = json.loads(captured[0].content)
        assert body.get("response_format") == {"type": "json_object"}

    @respx.mock
    async def test_429_escalates(self) -> None:
        from agent_core.drivers.codex import CodexDriver, OPENAI_API_URL

        respx.post(OPENAI_API_URL).mock(return_value=httpx.Response(429, text="rate limited"))

        spec = _make_spec()
        from agent_core.schemas import EscalationPolicy
        spec = spec.model_copy(update={"escalation": EscalationPolicy(max_retries=0)})
        driver = CodexDriver(spec, api_key="test-key")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.CODEX})
        result = await driver.invoke(ctx)

        assert result.status == TaskStatus.ESCALATED

    @respx.mock
    async def test_bearer_auth_header(self) -> None:
        from agent_core.drivers.codex import CodexDriver, OPENAI_API_URL

        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(DONE_PAYLOAD)}}]},
            )

        respx.post(OPENAI_API_URL).mock(side_effect=capture)

        driver = CodexDriver(_make_spec(), api_key="sk-openai-xyz")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.CODEX})
        await driver.invoke(ctx)

        assert captured[0].headers["Authorization"] == "Bearer sk-openai-xyz"


# ---------------------------------------------------------------------------
# GeminiDriver HTTP tests
# ---------------------------------------------------------------------------


class TestGeminiDriverHTTP:
    @respx.mock
    async def test_successful_request(self) -> None:
        from agent_core.drivers.gemini import GeminiDriver, AI_STUDIO_URL, DEFAULT_MODEL

        url = AI_STUDIO_URL.format(model=DEFAULT_MODEL)
        respx.post(url).mock(return_value=httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": json.dumps(DONE_PAYLOAD)}]}}]},
        ))

        driver = GeminiDriver(_make_spec(), api_key="test-key")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.GEMINI})
        result = await driver.invoke(ctx)

        assert result.status == TaskStatus.DONE

    @respx.mock
    async def test_api_key_in_header(self) -> None:
        from agent_core.drivers.gemini import GeminiDriver, AI_STUDIO_URL, DEFAULT_MODEL

        captured: list[httpx.Request] = []
        url = AI_STUDIO_URL.format(model=DEFAULT_MODEL)

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": json.dumps(DONE_PAYLOAD)}]}}]},
            )

        respx.post(url).mock(side_effect=capture)

        driver = GeminiDriver(_make_spec(), api_key="gemini-key-abc")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.GEMINI})
        await driver.invoke(ctx)

        assert captured[0].headers["x-goog-api-key"] == "gemini-key-abc"

    @respx.mock
    async def test_json_mime_type_in_generation_config(self) -> None:
        from agent_core.drivers.gemini import GeminiDriver, AI_STUDIO_URL, DEFAULT_MODEL

        captured: list[httpx.Request] = []
        url = AI_STUDIO_URL.format(model=DEFAULT_MODEL)

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": json.dumps(DONE_PAYLOAD)}]}}]},
            )

        respx.post(url).mock(side_effect=capture)

        driver = GeminiDriver(_make_spec(), api_key="test-key")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.GEMINI})
        await driver.invoke(ctx)

        body = json.loads(captured[0].content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"

    @respx.mock
    async def test_system_instruction_present(self) -> None:
        from agent_core.drivers.gemini import GeminiDriver, AI_STUDIO_URL, DEFAULT_MODEL

        captured: list[httpx.Request] = []
        url = AI_STUDIO_URL.format(model=DEFAULT_MODEL)

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": json.dumps(DONE_PAYLOAD)}]}}]},
            )

        respx.post(url).mock(side_effect=capture)

        driver = GeminiDriver(_make_spec(), api_key="test-key")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.GEMINI})
        await driver.invoke(ctx)

        body = json.loads(captured[0].content)
        assert "system_instruction" in body
        assert body["system_instruction"]["parts"][0]["text"]

    @respx.mock
    async def test_no_candidates_escalates(self) -> None:
        from agent_core.drivers.gemini import GeminiDriver, AI_STUDIO_URL, DEFAULT_MODEL

        url = AI_STUDIO_URL.format(model=DEFAULT_MODEL)
        respx.post(url).mock(return_value=httpx.Response(200, json={"candidates": []}))

        spec = _make_spec()
        from agent_core.schemas import EscalationPolicy
        spec = spec.model_copy(update={"escalation": EscalationPolicy(max_retries=0)})
        driver = GeminiDriver(spec, api_key="test-key")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.GEMINI})
        result = await driver.invoke(ctx)

        assert result.status == TaskStatus.ESCALATED

    @respx.mock
    async def test_429_escalates(self) -> None:
        from agent_core.drivers.gemini import GeminiDriver, AI_STUDIO_URL, DEFAULT_MODEL

        url = AI_STUDIO_URL.format(model=DEFAULT_MODEL)
        respx.post(url).mock(return_value=httpx.Response(429, text="quota exceeded"))

        spec = _make_spec()
        from agent_core.schemas import EscalationPolicy
        spec = spec.model_copy(update={"escalation": EscalationPolicy(max_retries=0)})
        driver = GeminiDriver(spec, api_key="test-key")
        ctx = _make_ctx()
        ctx = ctx.model_copy(update={"platform": Platform.GEMINI})
        result = await driver.invoke(ctx)

        assert result.status == TaskStatus.ESCALATED


# ---------------------------------------------------------------------------
# Cross-driver: JSON parsing edge cases
# ---------------------------------------------------------------------------


class TestJSONParsing:
    """Verify _parse_json_result edge cases across drivers."""

    @respx.mock
    async def test_markdown_fenced_json_parsed(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        fenced = f"```json\n{json.dumps(DONE_PAYLOAD)}\n```"
        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(
            200,
            json={"content": [{"type": "text", "text": fenced}]},
        ))

        driver = ClaudeDriver(_make_spec(), api_key="test-key", enable_caching=False)
        result = await driver.invoke(_make_ctx())

        assert result.status == TaskStatus.DONE

    @respx.mock
    async def test_missing_role_injected(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        payload_without_role = {k: v for k, v in DONE_PAYLOAD.items() if k != "role"}
        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(
            200,
            json={"content": [{"type": "text", "text": json.dumps(payload_without_role)}]},
        ))

        driver = ClaudeDriver(_make_spec(), api_key="test-key", enable_caching=False)
        result = await driver.invoke(_make_ctx())

        assert result.role == "implementer"

    @respx.mock
    async def test_missing_task_id_injected_from_context(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        payload_without_id = {k: v for k, v in DONE_PAYLOAD.items() if k != "task_id"}
        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(
            200,
            json={"content": [{"type": "text", "text": json.dumps(payload_without_id)}]},
        ))

        driver = ClaudeDriver(_make_spec(), api_key="test-key", enable_caching=False)
        ctx = _make_ctx(task_id="injected-id-xyz")
        result = await driver.invoke(ctx)

        assert result.task_id == "injected-id-xyz"

    @respx.mock
    async def test_partial_json_escalates(self) -> None:
        from agent_core.drivers.claude import ClaudeDriver, ANTHROPIC_API_URL

        respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(
            200,
            json={"content": [{"type": "text", "text": '{"task_id": "t1", "role": "imp'}]},
        ))

        spec = _make_spec()
        from agent_core.schemas import EscalationPolicy
        spec = spec.model_copy(update={"escalation": EscalationPolicy(max_retries=0)})
        driver = ClaudeDriver(spec, api_key="test-key", enable_caching=False)
        result = await driver.invoke(_make_ctx())

        assert result.status == TaskStatus.ESCALATED
