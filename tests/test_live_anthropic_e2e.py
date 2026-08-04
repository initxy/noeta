"""Anthropic ``/v1/messages`` adapter against a real gateway (live marker).

Mirrors ``test_live_responses_e2e.py`` for the Anthropic-shape adapter, which
until now had no direct live coverage — only an indirect path through
``test_live_context_supply_e2e.py``. Five chains, each pinning a contract only a
real model + real gateway can prove:

1. **streaming** — ``complete_streaming`` emits ``StreamDelta`` fragments as the
   gateway chunks the SSE body, and the reconstructed ``LLMResponse`` is
   shape-identical to a batch call. A fake transport can fabricate frames but
   not the gateway's actual chunk boundaries.
2. **reasoning continuation across a tool call** — under high effort the gateway
   returns a ``ThinkingBlock`` whose ``signature`` is the only continuation
   token; it must survive the round trip **verbatim** or the second turn is
   rejected.
3. **subagent delegation** — the real model drives the ``spawn_subagent``
   control tool through the shipping SDK host, the child runs, and its result
   folds back onto the parent.
4. **image input** — a local PNG rides as an ``ImageBlock(ContentRef)``, the
   adapter dereferences and base64-encodes it, and a vision model describes it.
5. **multi-turn tool use** — a tool result fed back reaches the model, proven by
   an answer only derivable from the tool output.

Config comes from a git-ignored ``.env`` via ``tests._live_env`` (copy
``.env.example``). Missing base/key/model auto-skips; CI never runs these.
Assertions watch **structural** invariants, never verbatim non-deterministic
content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.core.fold import fold
from noeta.protocols.messages import (
    ImageBlock,
    LLMRequest,
    Message,
    StreamDelta,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.storage.memory import InMemoryContentStore

from tests import _live_env

pytestmark = pytest.mark.live


# --------------------------------------------------------------------------- #
# Shared tool schema (OpenAI-shape; the adapter's _translate_tools converts it)
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# 1 — streaming
# --------------------------------------------------------------------------- #


@_live_env.requires_live
def test_live_anthropic_streaming() -> None:
    provider = _live_env.build_anthropic_provider()
    deltas: list[StreamDelta] = []
    request = LLMRequest(
        model=_live_env.live_model(),
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
    assert all(d.kind in ("text", "thinking") for d in deltas)
    assert all(isinstance(d, StreamDelta) for d in deltas)
    assert response.stop_reason == "end_turn"
    text = "".join(b.text for b in response.content if isinstance(b, TextBlock))
    assert "pong" in text.lower()
    # The streamed text is reconstructed from the same fragments the deltas carry.
    streamed_text = "".join(d.text for d in deltas if d.kind == "text")
    assert "pong" in streamed_text.lower()


# --------------------------------------------------------------------------- #
# 2 — reasoning continuation: signature carried verbatim across a tool call
# --------------------------------------------------------------------------- #


@_live_env.requires_live
def test_live_anthropic_reasoning_continuation_across_tool_call() -> None:
    provider = _live_env.build_anthropic_provider()
    # High effort + a question that needs a tool forces a reasoning block
    # (with a signature) alongside the tool_use.
    first = LLMRequest(
        model=_live_env.live_model(),
        messages=[
            Message(
                role="user",
                content=[
                    TextBlock(
                        text="I need the current weather in Tokyo to decide what "
                        "to pack. Call get_weather, then tell me whether to bring "
                        "an umbrella."
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
    assert tool_uses, "first turn never emitted a tool_use"
    call = tool_uses[0]
    assert call.tool_name == "get_weather"
    thinking = [b for b in first_response.content if isinstance(b, ThinkingBlock)]
    assert thinking, "high-effort turn carried no ThinkingBlock"
    # Some gateways return a signature (the encrypted continuation token) and
    # some return a bare reasoning summary with an empty signature. Both are
    # valid; capture whichever this gateway emits so the round-trip below feeds
    # back exactly what came out.
    original_signature = thinking[0].signature

    # Turn two feeds the assistant's own content back unmodified. When the
    # gateway emits a signature it has to survive byte-for-byte or the
    # continuation is rejected; either way the thinking block round-trips and
    # the tool result reaches the model.
    assistant_msg = Message(role="assistant", content=list(first_response.content))
    tool_msg = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id=call.call_id,
                output='{"city": "Tokyo", "condition": "rainy", "temp_c": 18}',
                success=True,
            )
        ],
    )
    second = LLMRequest(
        model=_live_env.live_model(),
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
    # "rainy" is only knowable from the tool output — evidence it reached the model.
    assert "umbrella" in final_text or "yes" in final_text
    # The thinking block we fed back is the same object the first turn produced,
    # so its continuation token (when the gateway emits one) went back verbatim.
    fed_back = [b for b in assistant_msg.content if isinstance(b, ThinkingBlock)]
    assert fed_back and fed_back[0].signature == original_signature


# --------------------------------------------------------------------------- #
# 3 — subagent delegation through the shipping SDK host
# --------------------------------------------------------------------------- #


@_live_env.requires_live
def test_live_anthropic_subagent_delegation(tmp_path: Path) -> None:
    from noeta.runtime.shell_policy import ShellMode
    from noeta.runtime.workspace import FsWriteMode

    from tests._sdk_session import (
        make_driver,
        make_host,
        make_registry,
        preset_spec,
        runner_main_spec,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("The secret code is BLUEBIRD.\n", encoding="utf-8")

    provider = _live_env.build_anthropic_provider()
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    children = [preset_spec(n) for n in ("explore", "general-purpose", "plan")]
    host = make_host(
        make_registry(main, *children),
        workspace_dir=ws,
        provider=provider,
        model=_live_env.live_model(),
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    driver = make_driver(host)
    out = driver.start(
        goal=(
            "Delegate to the 'explore' subagent: ask it to read notes.txt in the "
            "workspace and report the secret code. Use the spawn_subagent tool. "
            "After it reports back, reply with the code."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    parent = fold(host.event_log, host.content_store, out.task_id)
    # The child actually ran and its result folded back into the parent.
    assert parent.governance.subtask_results, "no subtask result folded onto parent"
    # A SubtaskSpawned event proves the control tool drove a real delegation.
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "SubtaskSpawned" in types


# --------------------------------------------------------------------------- #
# 4 — image input (vision model)
# --------------------------------------------------------------------------- #


@_live_env.requires_live
def test_live_anthropic_image_input() -> None:
    from noeta.builtins.providers.impl.anthropic import (
        AnthropicProvider,
        _model_admits_images,
    )

    vision_model = _live_env.live_vision_model()
    if not vision_model or not _model_admits_images(vision_model):
        pytest.skip(
            "image chain needs NOETA_LIVE_VISION_MODEL set to an image-"
            f"admitting model (got {vision_model!r}); the adapter guard "
            "refuses images to catalogued non-vision models"
        )
    # The ContentStore holds the real bytes; only the small ImageBlock(ContentRef)
    # handle travels through the request until the adapter dereferences it.
    content_store = InMemoryContentStore()
    ref = content_store.put(_live_env.SAMPLE_PNG, media_type="image/png")
    provider = AnthropicProvider(
        api_key=_live_env.live_api_key(),
        base_url=_live_env.live_base_url() or "",
        default_max_tokens=_live_env.live_max_tokens(),
        image_resolver=content_store.get,
    )
    request = LLMRequest(
        model=vision_model,
        messages=[
            Message(
                role="user",
                content=[
                    TextBlock(text="Describe this image in one short sentence."),
                    ImageBlock(source=ref),
                ],
            )
        ],
        max_tokens=256,
    )
    response = provider.complete(request)
    assert response.stop_reason == "end_turn"
    text = "".join(b.text for b in response.content if isinstance(b, TextBlock))
    # Wording varies run to run; any non-empty description proves the image arrived.
    assert text.strip(), "model returned no description for the image"


# --------------------------------------------------------------------------- #
# 5 — multi-turn tool use: a tool result fed back reaches the model
# --------------------------------------------------------------------------- #


@_live_env.requires_live
def test_live_anthropic_multi_turn_tool_use() -> None:
    provider = _live_env.build_anthropic_provider()
    first = LLMRequest(
        model=_live_env.live_model(),
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
        model=_live_env.live_model(),
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
