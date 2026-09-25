"""Provider adapters on the edges of a response: truncation, cut streams,
refusals, error bodies, index-less parallel calls, tool-result images.

Each case drives the real adapter over an ``httpx.MockTransport`` — the wire
bytes are the fixture, the neutral ``LLMResponse`` / error is the assertion.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx
import pytest

from noeta.builtins.providers.impl import catalog
from noeta.builtins.providers.impl.anthropic import AnthropicProvider
from noeta.builtins.providers.impl.openai_compat import OpenAICompatProvider
from noeta.builtins.providers.impl.openai_responses import OpenAIResponsesProvider
from noeta.protocols.errors import (
    FatalError,
    MalformedToolArgumentsError,
    TransientError,
)
from noeta.protocols.messages import (
    ImageBlock,
    LLMRequest,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.values import ContentRef

_REQ = LLMRequest(
    model="claude-opus-5",
    messages=[Message(role="user", content=[TextBlock(text="hi")])],
    max_tokens=50,
)


def _discard(_delta: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _anthropic(frames: list[tuple[str, dict[str, Any]]]) -> AnthropicProvider:
    body = "".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in frames
    ).encode()
    provider = AnthropicProvider(api_key="k", default_max_tokens=50)
    provider._client = httpx.Client(  # noqa: SLF001
        base_url="https://x",
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            )
        ),
    )
    return provider


_START = (
    "message_start",
    {
        "type": "message_start",
        "message": {
            "type": "message",
            "role": "assistant",
            "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": 5, "output_tokens": 1},
        },
    },
)
_STOP = ("message_stop", {"type": "message_stop"})


def _tool_use(index: int, call_id: str, partial_json: Optional[str]) -> list:
    frames: list = [
        (
            "content_block_start",
            {
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "Read",
                    "input": {},
                },
            },
        )
    ]
    if partial_json is not None:
        frames.append(
            (
                "content_block_delta",
                {
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": partial_json},
                },
            )
        )
    frames.append(("content_block_stop", {"index": index}))
    return frames


def _stop_delta(reason: str) -> tuple[str, dict[str, Any]]:
    return ("message_delta", {"delta": {"stop_reason": reason}, "usage": {"output_tokens": 50}})


def test_anthropic_max_tokens_drops_the_call_cut_mid_json() -> None:
    provider = _anthropic(
        [
            _START,
            *_tool_use(0, "t1", '{"file_path":"/a"}'),
            *_tool_use(1, "t2", '{"file_pa'),
            _stop_delta("max_tokens"),
            _STOP,
        ]
    )
    response = provider.complete(_REQ)
    assert response.stop_reason == "max_tokens"
    assert response.content == [
        ToolUseBlock(call_id="t1", tool_name="Read", arguments={"file_path": "/a"})
    ]


def test_anthropic_undecodable_input_without_max_tokens_still_raises() -> None:
    provider = _anthropic(
        [_START, *_tool_use(0, "t1", '{"file_pa'), _stop_delta("tool_use"), _STOP]
    )
    with pytest.raises(ValueError, match="tool_use input was not valid JSON"):
        provider.complete(_REQ)


def test_anthropic_stream_closed_before_message_delta_is_transient() -> None:
    provider = _anthropic(
        [
            _START,
            (
                "content_block_start",
                {"index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            (
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": "half an ans"}},
            ),
        ]
    )
    with pytest.raises(TransientError, match="no stop_reason"):
        provider.complete(_REQ)


def test_anthropic_refusal_interrupting_a_tool_call_ends_the_turn() -> None:
    provider = _anthropic(
        [
            _START,
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": "I can't"}}),
            ("content_block_stop", {"index": 0}),
            *_tool_use(1, "t1", '{"file_path":"/a"}'),
            _stop_delta("refusal"),
            _STOP,
        ]
    )
    response = provider.complete(_REQ)
    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="I can't")]


# ---------------------------------------------------------------------------
# OpenAI Chat Completions
# ---------------------------------------------------------------------------


def _chat(chunks: list[dict[str, Any]], *, done: bool = True) -> OpenAICompatProvider:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    if done:
        body += "data: [DONE]\n\n"
    provider = OpenAICompatProvider("https://x/v1", api_key="k")
    provider._client = httpx.Client(  # noqa: SLF001
        base_url="https://x/v1",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=body.encode())),
    )
    return provider


def _call_chunk(
    index: Optional[int], call_id: Optional[str], args: str, name: str = "Read"
) -> dict[str, Any]:
    call: dict[str, Any] = {"function": {"name": name, "arguments": args}}
    if index is not None:
        call["index"] = index
    if call_id is not None:
        call["id"] = call_id
        call["type"] = "function"
    return {"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}


def _finish(reason: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def _text_chunk(text: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def test_chat_length_drops_the_call_cut_mid_json() -> None:
    provider = _chat(
        [
            _call_chunk(0, "c1", '{"file_path":"/a"}'),
            _call_chunk(1, "c2", '{"file_pa'),
            _finish("length"),
        ]
    )
    response = provider.complete_streaming(_REQ, _discard)
    assert response.stop_reason == "max_tokens"
    assert response.content == [
        ToolUseBlock(call_id="c1", tool_name="Read", arguments={"file_path": "/a"})
    ]


def test_chat_undecodable_arguments_without_length_stay_transient() -> None:
    provider = _chat([_call_chunk(0, "c1", '{"file_pa'), _finish("tool_calls")])
    with pytest.raises(MalformedToolArgumentsError):
        provider.complete_streaming(_REQ, _discard)


def test_chat_stream_cut_after_partial_text_is_transient() -> None:
    provider = _chat([_text_chunk("half an ans")], done=False)
    with pytest.raises(TransientError, match="without \\[DONE\\] or a finish_reason"):
        provider.complete_streaming(_REQ, _discard)


def test_chat_stream_with_finish_reason_but_no_done_still_completes() -> None:
    provider = _chat([_text_chunk("whole answer"), _finish("stop")], done=False)
    response = provider.complete_streaming(_REQ, _discard)
    assert response.stop_reason == "end_turn"
    assert response.content == [TextBlock(text="whole answer")]


def test_chat_parallel_calls_without_index_split_on_a_new_id() -> None:
    provider = _chat(
        [
            _call_chunk(None, "c1", '{"file_path":'),
            _call_chunk(None, None, '"/a"}'),
            _call_chunk(None, "c2", '{"file_path":"/b"}'),
            _finish("tool_calls"),
        ]
    )
    response = provider.complete_streaming(_REQ, _discard)
    assert response.stop_reason == "tool_use"
    assert response.content == [
        ToolUseBlock(call_id="c1", tool_name="Read", arguments={"file_path": "/a"}),
        ToolUseBlock(call_id="c2", tool_name="Read", arguments={"file_path": "/b"}),
    ]


def test_chat_tool_result_images_leave_a_note() -> None:
    image = ImageBlock(source=ContentRef(hash="h" * 64, size=3, media_type="image/png"))
    request = LLMRequest(
        model="gpt-4o",
        messages=[
            Message(role="user", content=[TextBlock(text="look")]),
            Message(
                role="assistant",
                content=[ToolUseBlock(call_id="c1", tool_name="Read", arguments={})],
            ),
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        call_id="c1", success=True, output="Read image a.png", images=[image]
                    )
                ],
            ),
        ],
    )
    body = OpenAICompatProvider("https://x/v1", api_key="k")._build_request_body(request)  # noqa: SLF001
    tool_message = body["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["content"].startswith("Read image a.png\n[image omitted:")


# ---------------------------------------------------------------------------
# OpenAI Responses
# ---------------------------------------------------------------------------


def _responses(payload: dict[str, Any]) -> OpenAIResponsesProvider:
    provider = OpenAIResponsesProvider("https://x/responses", api_key="k")
    provider._client = httpx.Client(  # noqa: SLF001
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
    )
    return provider


def _function_call(call_id: str, arguments: str) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": "Read",
        "arguments": arguments,
    }


def test_responses_output_cap_drops_the_call_cut_mid_json() -> None:
    provider = _responses(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                _function_call("c1", '{"file_path":"/a"}'),
                _function_call("c2", '{"file_pa'),
            ],
        }
    )
    response = provider.complete(_REQ)
    assert response.stop_reason == "max_tokens"
    assert response.content == [
        ToolUseBlock(call_id="c1", tool_name="Read", arguments={"file_path": "/a"})
    ]


def test_responses_undecodable_arguments_when_completed_stay_transient() -> None:
    provider = _responses(
        {"status": "completed", "output": [_function_call("c1", '{"file_pa')]}
    )
    with pytest.raises(MalformedToolArgumentsError):
        provider.complete(_REQ)


# ---------------------------------------------------------------------------
# HTTP error text keeps the provider's explanation
# ---------------------------------------------------------------------------


def _failing_client(status: int, **response_kwargs: Any) -> httpx.Client:
    return httpx.Client(
        base_url="https://x",
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(status, **response_kwargs)
        ),
    )


def _anthropic_failing(status: int, **kw: Any) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="k", default_max_tokens=50)
    provider._client = _failing_client(status, **kw)  # noqa: SLF001
    return provider


def _chat_failing(status: int, **kw: Any) -> OpenAICompatProvider:
    provider = OpenAICompatProvider("https://x/v1", api_key="k")
    provider._client = _failing_client(status, **kw)  # noqa: SLF001
    return provider


def _responses_failing(status: int, **kw: Any) -> OpenAIResponsesProvider:
    provider = OpenAIResponsesProvider("https://x/responses", api_key="k")
    provider._client = _failing_client(status, **kw)  # noqa: SLF001
    return provider


_ADAPTERS = [_anthropic_failing, _chat_failing, _responses_failing]


@pytest.mark.parametrize("make", _ADAPTERS)
def test_fatal_http_error_carries_the_json_error_message(make: Any) -> None:
    provider = make(
        404,
        json={"error": {"type": "not_found_error", "message": "model: nope-1 not found"}},
    )
    with pytest.raises(FatalError) as info:
        provider.complete(_REQ)
    assert str(info.value).endswith(": model: nope-1 not found")
    assert "404" in str(info.value)


@pytest.mark.parametrize("make", _ADAPTERS)
def test_transient_http_error_carries_a_capped_raw_body(make: Any) -> None:
    provider = make(503, text="upstream gateway melted " + "x" * 2000)
    with pytest.raises(TransientError) as info:
        provider.complete(_REQ)
    text = str(info.value)
    assert "upstream gateway melted" in text
    assert "x" * 450 in text  # the body reaches the error text ...
    assert "x" * 500 not in text  # ... capped at 500 characters


@pytest.mark.parametrize("make", _ADAPTERS)
def test_http_error_with_empty_body_keeps_the_status_text(make: Any) -> None:
    provider = make(401, content=b"")
    with pytest.raises(FatalError) as info:
        provider.complete(_REQ)
    assert "401" in str(info.value)
    assert not str(info.value).endswith(": ")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_gpt_4o_family_reads_images() -> None:
    for model_id in ("gpt-4o", "gpt-4o-mini"):
        spec = catalog.find_spec(model_id)
        assert spec is not None and spec.supports_vision is True


# ---------------------------------------------------------------------------
# OpenAI Chat Completions: user-turn images
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG-bytes"
_PNG_REF = ContentRef(hash="p" * 64, size=len(_PNG), media_type="image/png")


def _image_request(model: str, *, origin: Optional[str] = None) -> LLMRequest:
    return LLMRequest(
        model=model,
        messages=[
            Message(
                role="user",
                origin=origin,
                content=[TextBlock(text="what is this?"), ImageBlock(source=_PNG_REF)],
            )
        ],
    )


def test_chat_user_image_rides_as_an_image_url_data_uri() -> None:
    import base64

    provider = OpenAICompatProvider(
        "https://x/v1", api_key="k", image_resolver=lambda ref: _PNG
    )
    body = provider._build_request_body(_image_request("gpt-4o"))  # noqa: SLF001
    assert body["messages"][-1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(_PNG).decode()
                },
            },
        ],
    }


def test_chat_user_image_for_an_uncatalogued_model_passes() -> None:
    provider = OpenAICompatProvider(
        "https://x/v1", api_key="k", image_resolver=lambda ref: _PNG
    )
    body = provider._build_request_body(_image_request("some-gateway-model"))  # noqa: SLF001
    assert body["messages"][-1]["content"][1]["type"] == "image_url"


def test_chat_user_image_for_a_catalogued_text_only_model_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        catalog,
        "_EXTENSIONS",
        {
            **catalog._EXTENSIONS,  # noqa: SLF001
            "text-only-model": catalog.ModelSpec(
                real_model_id="text-only-model",
                context_window=128_000,
                max_output_tokens=16_384,
                supports_vision=False,
            ),
        },
    )
    calls: list[Any] = []
    provider = OpenAICompatProvider(
        "https://x/v1", api_key="k", image_resolver=lambda ref: calls.append(ref) or _PNG
    )
    provider._client = httpx.Client(  # noqa: SLF001
        base_url="https://x/v1",
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)),
    )
    with pytest.raises(FatalError, match="supports_vision=False"):
        provider.complete(_image_request("text-only-model"))
    assert calls == []  # nothing deref'd, nothing sent


def test_chat_image_in_host_injected_turn_is_still_refused() -> None:
    provider = OpenAICompatProvider(
        "https://x/v1", api_key="k", image_resolver=lambda ref: _PNG
    )
    with pytest.raises(ValueError, match="does not support image"):
        provider._build_request_body(_image_request("gpt-4o", origin="system"))  # noqa: SLF001
