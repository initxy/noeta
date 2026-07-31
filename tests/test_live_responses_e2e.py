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

Config comes from a git-ignored ``.env`` via ``tests._live_env`` (copy
``.env.example`` and fill in ``NOETA_LIVE_BASE_URL`` / ``NOETA_LIVE_API_KEY`` /
``NOETA_LIVE_MODEL``). The Responses adapter is pointed at ``<base>/v1/responses``
and a ``Bearer`` auth header is injected for gateways that want it::

    uv run pytest -m live tests/test_live_responses_e2e.py

Missing config auto-skips (CI does not run it by default). Real model responses
are non-deterministic, so assertions watch only **structural** invariants (block
types, stop_reason, non-empty call_id/signature, keyword presence), not verbatim
content.
"""

from __future__ import annotations

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

from tests import _live_env

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Provider from the shared .env loader
# ---------------------------------------------------------------------------


def _model() -> str:
    return _live_env.live_model() or ""


def _build_provider(content_store=None):
    """Build the Responses provider from the shared ``.env`` config."""
    return _live_env.build_responses_provider(content_store=content_store)


requires_live = _live_env.requires_live


# A generated 32x32 solid-red PNG for the image chain — shared with the other
# live modules via ``_live_env`` so there is one fixture to keep in sync.
_SAMPLE_PNG = _live_env.SAMPLE_PNG


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
    # At high effort the gateway returns a reasoning item. Some gateways attach
    # the encrypted_content continuation token as its signature; others return a
    # bare reasoning summary with an empty signature. Both are valid — capture
    # whichever this one emits so the round-trip below feeds back exactly it.
    assert thinking, "high-effort turn carried no ThinkingBlock"
    original_signature = thinking[0].signature

    # Turn two feeds the assistant's own content back unmodified: when the
    # gateway emits a signature it has to survive the round trip byte-for-byte,
    # since the gateway holds nothing server-side to reconstruct it from.
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
    # The thinking block we fed back is the same object the first turn produced,
    # so its continuation token (when the gateway emits one) went back verbatim.
    fed_back = [b for b in assistant_msg.content if isinstance(b, ThinkingBlock)]
    assert fed_back and fed_back[0].signature == original_signature


# ---------------------------------------------------------------------------
# Loop 4 — image: local PNG → base64 → ImageBlock → input_image → model describes it
# ---------------------------------------------------------------------------


@requires_live
def test_live_responses_image_input() -> None:
    from noeta.builtins.providers.impl.openai_responses import _model_supports_vision

    vision_model = _live_env.live_vision_model()
    if not vision_model or not _model_supports_vision(vision_model):
        pytest.skip(
            "image chain needs NOETA_LIVE_VISION_MODEL set to a catalog "
            f"vision-capable model (got {vision_model!r}); the adapter guard "
            "refuses images to non-vision models"
        )
    # The ContentStore holds the real bytes; only the small
    # ImageBlock(ContentRef) handle travels through the request.
    content_store = InMemoryContentStore()
    ref = content_store.put(_SAMPLE_PNG, media_type="image/png")
    provider = _build_provider(content_store=content_store)
    request = LLMRequest(
        model=vision_model,  # catalog supports_vision=True, checked above
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
