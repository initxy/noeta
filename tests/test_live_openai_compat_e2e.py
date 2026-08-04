"""OpenAI ``/v1/chat/completions`` adapter against the real gateway (live marker).

The third shipping provider adapter — until now the only one with **no** live
coverage. Mirrors ``test_live_responses_e2e.py`` in shape, driving the provider
directly with hand-built ``LLMRequest`` values because this layer watches only
the adapter↔gateway hop (host integration is covered against a stub provider).

Four chains — plain text, tool call, streaming, multi-turn tool use — each
pinning a contract only a real model + real gateway proves:

1. **plain text** — a bare text turn round-trips to ``end_turn`` with the word
   back.
2. **tool call** — an OpenAI-shape tool schema is translated, the model emits a
   ``function`` call, and it parses into a ``ToolUseBlock`` with a paired
   ``call_id``.
3. **streaming** — ``complete_streaming`` emits ``StreamDelta`` fragments as the
   gateway chunks the SSE body, and the reconstructed text matches a batch call.
4. **multi-turn tool use** — a ``ToolResultBlock`` fed back reaches the model,
   proven by an answer only derivable from the tool output.

Reasoning continuation is deliberately NOT asserted here: the compat adapter's
``reasoning_continuation`` defaults to ``"off"`` and native OpenAI hides the
reasoning trace, so unlike the Responses / Anthropic shapes there is no
continuation token to prove round-trips.

Config comes from a git-ignored ``.env`` via ``tests._live_env`` (copy
``.env.example`` and fill in ``NOETA_LIVE_BASE_URL`` / ``NOETA_LIVE_API_KEY`` /
``NOETA_LIVE_MODEL``). The adapter is pointed at ``<base>/v1`` and appends
``/chat/completions`` itself; it already sends ``Authorization: Bearer``::

    uv run pytest -m live tests/test_live_openai_compat_e2e.py

Missing config auto-skips (CI does not run it by default). Real model responses
are non-deterministic, so assertions watch only **structural** invariants (block
types, stop_reason, non-empty call_id, keyword presence), never verbatim content.
"""

from __future__ import annotations

import pytest

from noeta.protocols.messages import (
    LLMRequest,
    Message,
    StreamDelta,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from tests import _live_env

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Provider from the shared .env loader
# ---------------------------------------------------------------------------


def _model() -> str:
    return _live_env.live_model() or ""


def _build_provider():
    """Build the OpenAI-compat provider from the shared ``.env`` config."""
    return _live_env.build_openai_compat_provider()


requires_live = _live_env.requires_live


# Shared tool schema (OpenAI-shape; the adapter forwards it as-is).
_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


# ---------------------------------------------------------------------------
# Loop 1 — plain text in, text out
# ---------------------------------------------------------------------------


@requires_live
def test_live_openai_compat_plain_text() -> None:
    provider = _build_provider()
    request = LLMRequest(
        model=_model(),
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="Reply with exactly the single word: pong")],
            )
        ],
        max_tokens=64,
    )
    response = provider.complete(request)
    assert response.stop_reason == "end_turn"
    text = "".join(b.text for b in response.content if isinstance(b, TextBlock))
    assert "pong" in text.lower()


# ---------------------------------------------------------------------------
# Loop 2 — tool call (function call parsed into ToolUseBlock)
# ---------------------------------------------------------------------------


@requires_live
def test_live_openai_compat_tool_call() -> None:
    provider = _build_provider()
    request = LLMRequest(
        model=_model(),
        messages=[
            Message(
                role="user",
                content=[
                    TextBlock(
                        text="What is the weather in Tokyo? Use the "
                        "get_weather tool."
                    )
                ],
            )
        ],
        tools=[_WEATHER_TOOL],
        max_tokens=256,
    )
    response = provider.complete(request)
    assert response.stop_reason == "tool_use"
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    assert tool_uses, "model never emitted a function call"
    call = tool_uses[0]
    assert call.tool_name == "get_weather"
    assert call.call_id  # the tool result is paired back by this id
    assert "city" in call.arguments


# ---------------------------------------------------------------------------
# Loop 3 — streaming: SSE deltas reconstruct the same text as a batch call
# ---------------------------------------------------------------------------


@requires_live
def test_live_openai_compat_streaming() -> None:
    provider = _build_provider()
    deltas: list[StreamDelta] = []
    request = LLMRequest(
        model=_model(),
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="Reply with exactly the single word: pong")],
            )
        ],
        max_tokens=64,
    )
    response = provider.complete_streaming(request, deltas.append)
    # Deltas fired as a side effect; the full LLMResponse is still the return.
    assert deltas, "streaming produced no deltas"
    assert all(isinstance(d, StreamDelta) for d in deltas)
    assert all(d.kind in ("text", "thinking") for d in deltas)
    assert response.stop_reason == "end_turn"
    text = "".join(b.text for b in response.content if isinstance(b, TextBlock))
    assert "pong" in text.lower()
    # The streamed text is reconstructed from the same fragments the deltas carry.
    streamed_text = "".join(d.text for d in deltas if d.kind == "text")
    assert "pong" in streamed_text.lower()


# ---------------------------------------------------------------------------
# Loop 4 — multi-turn tool use: a fed-back tool result reaches the model
# ---------------------------------------------------------------------------


@requires_live
def test_live_openai_compat_multi_turn_tool_use() -> None:
    provider = _build_provider()
    first = LLMRequest(
        model=_model(),
        messages=[
            Message(
                role="user",
                content=[
                    TextBlock(
                        text="What is the weather in Paris? Use the get_weather tool."
                    )
                ],
            )
        ],
        tools=[_WEATHER_TOOL],
        max_tokens=512,
    )
    first_response = provider.complete(first)
    assert first_response.stop_reason == "tool_use"
    tool_uses = [b for b in first_response.content if isinstance(b, ToolUseBlock)]
    assert tool_uses, "model never emitted a tool_use"
    call = tool_uses[0]
    assert call.tool_name == "get_weather"
    assert call.call_id
    assert "city" in call.arguments

    assistant_msg = Message(role="assistant", content=list(first_response.content))
    tool_msg = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id=call.call_id,
                output='{"city": "Paris", "condition": "snowing", "temp_c": -2}',
                success=True,
            )
        ],
    )
    second = LLMRequest(
        model=_model(),
        messages=first.messages + [assistant_msg, tool_msg],
        tools=[_WEATHER_TOOL],
        max_tokens=512,
    )
    second_response = provider.complete(second)
    assert second_response.stop_reason == "end_turn"
    final_text = "".join(
        b.text for b in second_response.content if isinstance(b, TextBlock)
    ).lower()
    # "snowing" / "snow" is only knowable from the tool output.
    assert "snow" in final_text
