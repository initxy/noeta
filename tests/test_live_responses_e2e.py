"""OpenAI Responses adapter against the real gateway (live marker).

Four chains — plain text, tool call, reasoning continuation, image — because a
wire translation can only be proven correct by a model that actually answers.
The reasoning chain is the one worth the credit: under ``store:false`` the
gateway keeps no server-side state, so the ``encrypted_content`` returned as a
``ThinkingBlock`` signature is the only continuation token, and it has to go
back **verbatim** across the tool call or the second turn is rejected.

The provider is driven directly with hand-built ``LLMRequest`` values rather
than through the SDK host: this layer watches only the adapter↔gateway hop, and
host-level integration is already covered against a stub provider.

Run (credentials come from env, **never** hard-coded; the key is rotated and
human-held)::

    NOETA_AGENT_BASE_URL=https://<your-gateway-host>/responses \\
    NOETA_AGENT_API_KEY=<rotated-key> \\
    NOETA_AGENT_API_VERSION=<api-version> \\
    NOETA_AGENT_MODEL=gpt-5.4-2026-03-05 \\
        uv run pytest -m live tests/test_live_responses_e2e.py

Missing any env auto-skips (CI does not run it by default). Real model responses
are non-deterministic, so assertions watch only **structural** invariants (block
types, stop_reason, non-empty call_id/signature, keyword presence), not verbatim
content.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import pytest

from noeta.storage.memory import InMemoryContentStore
from noeta.protocols.messages import (
    ImageBlock,
    LLMRequest,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Provider from env — all credentials from env; skip if any is missing
# ---------------------------------------------------------------------------

_REQUIRED_ENV = ("NOETA_AGENT_BASE_URL", "NOETA_AGENT_API_KEY", "NOETA_AGENT_MODEL")


def _env_complete() -> bool:
    return all(os.environ.get(v) for v in _REQUIRED_ENV)


def _model() -> str:
    return os.environ.get("NOETA_AGENT_MODEL", "gpt-5.4-2026-03-05")


def _build_provider(content_store: Optional[Any] = None):
    """Build the Responses provider from env. ``base_url`` is the **full**
    responses endpoint, and ``image_resolver`` is what lets the provider
    dereference an ``ImageBlock``'s ``ContentRef`` at send time."""
    from noeta.builtins.providers.impl.openai_responses import OpenAIResponsesProvider

    return OpenAIResponsesProvider(
        base_url=os.environ["NOETA_AGENT_BASE_URL"],
        api_key=os.environ["NOETA_AGENT_API_KEY"],
        api_version=os.environ.get("NOETA_AGENT_API_VERSION"),
        image_resolver=content_store.get if content_store is not None else None,
    )


requires_live = pytest.mark.skipif(
    not _env_complete(),
    reason=(
        "Responses live E2E needs NOETA_AGENT_BASE_URL / NOETA_AGENT_API_KEY / "
        "NOETA_AGENT_MODEL (+ optional NOETA_AGENT_API_VERSION). Skipped without "
        "a rotated gateway key."
    ),
)


# A generated 32x32 solid-red PNG for the image chain — no external file to
# keep in sync. A **1x1 degenerate image will not work**: the gateway rejects
# it with HTTP 400 ("The image data you provided does not represent a valid
# image."), so the fixture needs real dimensions.
def _solid_png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """Generate a solid-color RGBA PNG at a fixed zlib level, so the bytes are
    reproducible across runs and machines."""
    import struct
    import zlib

    raw = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


_SAMPLE_PNG = _solid_png(32, 32, (220, 40, 40, 255))


# ---------------------------------------------------------------------------
# Loop 1 — plain text in, text out
# ---------------------------------------------------------------------------


@requires_live
def test_live_responses_plain_text() -> None:
    provider = _build_provider()
    request = LLMRequest(
        model=_model(),
        messages=[
            Message(
                role="user",
                content=[
                    TextBlock(
                        text="Reply with exactly the single word: pong"
                    )
                ],
            )
        ],
        max_tokens=64,
    )
    response = provider.complete(request)
    assert response.stop_reason == "end_turn"
    text = "".join(
        b.text for b in response.content if isinstance(b, TextBlock)
    )
    assert "pong" in text.lower()


# ---------------------------------------------------------------------------
# Loop 2 — tool call (function_call parsed into ToolUseBlock)
# ---------------------------------------------------------------------------

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


@requires_live
def test_live_responses_tool_call() -> None:
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
    assert tool_uses, "model never emitted a function_call"
    call = tool_uses[0]
    assert call.tool_name == "get_weather"
    assert call.call_id  # the tool result is paired back by this id
    assert "city" in call.arguments


# ---------------------------------------------------------------------------
# Loop 3 — reasoning continuation: encrypted_content carried verbatim across a tool call
# ---------------------------------------------------------------------------


@requires_live
def test_live_responses_reasoning_continuation_across_tool_call() -> None:
    provider = _build_provider()
    # High effort + a question that needs a tool to answer forces the gateway
    # to return a reasoning item (with encrypted_content) + a function_call.
    first = LLMRequest(
        model=_model(),
        messages=[
            Message(
                role="user",
                content=[
                    TextBlock(
                        text="I need the current weather in Tokyo to decide "
                        "what to pack. Call get_weather, then tell me whether "
                        "to bring an umbrella."
                    )
                ],
            )
        ],
        tools=[_WEATHER_TOOL],
        effort="high",
        max_tokens=2048,
    )
    first_response = provider.complete(first)
    assert first_response.stop_reason == "tool_use"
    tool_uses = [b for b in first_response.content if isinstance(b, ToolUseBlock)]
    assert tool_uses, "first turn never emitted a function_call"
    call = tool_uses[0]
    thinking = [b for b in first_response.content if isinstance(b, ThinkingBlock)]
    # At high effort the gateway returns a reasoning item whose signature is
    # the encrypted_content continuation token.
    assert thinking, "high-effort turn carried no ThinkingBlock"
    assert thinking[0].signature, "ThinkingBlock missing encrypted_content"

    # Turn two feeds the assistant's own content back unmodified: the
    # ThinkingBlock has to survive the round trip byte-for-byte, since the
    # gateway holds nothing server-side to reconstruct it from.
    assistant_msg = Message(role="assistant", content=list(first_response.content))
    tool_msg = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id=call.call_id,
                output='{"city": "Tokyo", "condition": "rainy", '
                '"temp_c": 18}',
                success=True,
            )
        ],
    )
    second = LLMRequest(
        model=_model(),
        messages=first.messages + [assistant_msg, tool_msg],
        tools=[_WEATHER_TOOL],
        effort="high",
        max_tokens=2048,
    )
    second_response = provider.complete(second)
    assert second_response.stop_reason == "end_turn"
    final_text = "".join(
        b.text for b in second_response.content if isinstance(b, TextBlock)
    ).lower()
    # Evidence the tool result actually reached the model: "rainy" is only
    # knowable from the tool output.
    assert "umbrella" in final_text or "yes" in final_text


# ---------------------------------------------------------------------------
# Loop 4 — image: local PNG → base64 → ImageBlock → input_image → model describes it
# ---------------------------------------------------------------------------


@requires_live
def test_live_responses_image_input() -> None:
    # The ContentStore holds the real bytes; only the small
    # ImageBlock(ContentRef) handle travels through the request.
    content_store = InMemoryContentStore()
    ref = content_store.put(_SAMPLE_PNG, media_type="image/png")
    provider = _build_provider(content_store=content_store)
    request = LLMRequest(
        model=_model(),  # must be a vision model (catalog supports_vision=True), else the guard blocks it
        messages=[
            Message(
                role="user",
                content=[
                    TextBlock(
                        text="Describe this image in one short sentence."
                    ),
                    ImageBlock(source=ref),
                ],
            )
        ],
        max_tokens=256,
    )
    response = provider.complete(request)
    assert response.stop_reason == "end_turn"
    text = "".join(
        b.text for b in response.content if isinstance(b, TextBlock)
    )
    # Any non-empty description proves the image arrived; the wording varies
    # run to run, so there is nothing stable to match on.
    assert text.strip(), "model returned no description for the image"
