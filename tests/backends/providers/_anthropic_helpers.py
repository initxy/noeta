"""Shared fixtures for the Anthropic adapter tests."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from noeta.backends.providers.anthropic import AnthropicProvider
from noeta.core import models
from noeta.core.types import (
    Message,
    ModelRequest,
    ModelResponse,
    Text,
    ToolResult,
    ToolSpec,
    ToolUse,
    user,
)
from tests.backends.providers.sse import anthropic_sse, sse_response

BASE_URL = "https://api.anthropic.test"
MESSAGES_ENDPOINT = f"{BASE_URL}/v1/messages"

#: No shipped catalog row is text-only, so the non-vision paths run against a
#: row registered for the test.
_TEXT_ONLY_MODEL = "text-only-model"
_VISION_MODEL = "claude-opus-4-8"
#: Absent from the catalog: the "unknown model" paths.
_UNKNOWN_MODEL = "claude-opus-4-7"
_PNG = b"\x89PNG\r\n\x1a\nFAKEPNGDATA"


def register_text_only_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        models,
        "_EXTENSIONS",
        {
            **models._EXTENSIONS,
            _TEXT_ONLY_MODEL: models.ModelSpec(
                real_model_id=_TEXT_ONLY_MODEL,
                context_window=128_000,
                max_output_tokens=16_384,
                supports_vision=False,
            ),
        },
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _provider(**overrides: Any) -> AnthropicProvider:
    kwargs: dict[str, Any] = {
        "api_key": "sk-ant-test",
        "base_url": BASE_URL,
        "default_max_tokens": 4096,
    }
    kwargs.update(overrides)
    return AnthropicProvider(**kwargs)


def _req(**overrides: Any) -> ModelRequest:
    kwargs: dict[str, Any] = {"model": _UNKNOWN_MODEL, "messages": [user("hi")]}
    kwargs.update(overrides)
    return ModelRequest(**kwargs)


def _resp(
    *,
    content: list[dict[str, Any]] | None = None,
    stop_reason: str | None = "end_turn",
    usage: dict[str, Any] | None = None,
    response_type: str = "message",
    role: str = "assistant",
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": response_type,
        "role": role,
        "model": "claude-opus-4-7",
        "content": content if content is not None else [{"type": "text", "text": "ok"}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
    }


def _stream(message: dict[str, Any]) -> httpx.Response:
    return sse_response(anthropic_sse(message))


def _tool(name: str = "t", description: str = "d") -> ToolSpec:
    return ToolSpec(name=name, description=description, input_schema={"type": "object"})


async def _send(
    request: ModelRequest | None = None,
    message: dict[str, Any] | None = None,
    **provider_kw: Any,
) -> tuple[dict[str, Any], ModelResponse]:
    route = respx.post(MESSAGES_ENDPOINT).mock(return_value=_stream(message or _resp()))
    response = await _provider(**provider_kw).complete(request or _req())
    body = json.loads(route.calls.last.request.content)
    return body, response


async def _respond(message: dict[str, Any]) -> ModelResponse:
    respx.post(MESSAGES_ENDPOINT).mock(return_value=_stream(message))
    return await _provider().complete(_req())


def _injected(text: str, origin: str) -> Message:
    return Message(role="user", content=[Text(text=text)], origin=origin)  # type: ignore[arg-type]


def _call(call_id: str = "c1", name: str = "echo", **args: Any) -> Message:
    return Message(role="assistant", content=[ToolUse(id=call_id, name=name, input=args)])


def _result(call_id: str = "c1", text: str = "ok", **kw: Any) -> Message:
    return Message(
        role="user", content=[ToolResult(call_id=call_id, content=[Text(text=text)], **kw)]
    )


EPHEMERAL = {"type": "ephemeral"}
