"""Anthropic adapter: a text block with no visible text never reaches history.

A streamed text block can start and stop without a single ``text_delta``;
assembled as ``TextBlock("")`` it poisons the next request ("text content
blocks must be non-empty"). The shared parser drops it on both transports,
and the outbound translation drops one already recorded in older history.
Thinking and tool_use blocks are untouched.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from noeta.builtins.providers.impl.anthropic import AnthropicProvider
from noeta.protocols.messages import (
    LLMRequest,
    Message,
    StreamDelta,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

_REQ = LLMRequest(
    model="claude-opus-5",
    messages=[Message(role="user", content=[TextBlock(text="hi")])],
    max_tokens=50,
)


def _provider(frames: list[tuple[str, dict[str, Any]]]) -> AnthropicProvider:
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


def _stop_delta(reason: str) -> tuple[str, dict[str, Any]]:
    return ("message_delta", {"delta": {"stop_reason": reason}, "usage": {"output_tokens": 9}})


def _empty_text(index: int, text: str = "") -> list[tuple[str, dict[str, Any]]]:
    frames: list[tuple[str, dict[str, Any]]] = [
        ("content_block_start", {"index": index, "content_block": {"type": "text", "text": ""}})
    ]
    if text:
        frames.append(
            ("content_block_delta", {"index": index, "delta": {"type": "text_delta", "text": text}})
        )
    frames.append(("content_block_stop", {"index": index}))
    return frames


def _thinking(index: int) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("content_block_start", {"index": index, "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"index": index, "delta": {"type": "thinking_delta", "thinking": "hmm"}}),
        ("content_block_delta", {"index": index, "delta": {"type": "signature_delta", "signature": "sig"}}),
        ("content_block_stop", {"index": index}),
    ]


def _tool_use(index: int) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "content_block_start",
            {
                "index": index,
                "content_block": {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
            },
        ),
        (
            "content_block_delta",
            {"index": index, "delta": {"type": "input_json_delta", "partial_json": '{"file_path":"/a"}'}},
        ),
        ("content_block_stop", {"index": index}),
    ]


def _discard(_delta: StreamDelta) -> None:
    return None


def test_streamed_empty_text_block_before_a_tool_call_is_dropped() -> None:
    provider = _provider(
        [_START, *_thinking(0), *_empty_text(1), *_tool_use(2), _stop_delta("tool_use"), _STOP]
    )
    response = provider.complete_streaming(_REQ, _discard)
    assert response.stop_reason == "tool_use"
    assert response.content == [
        ThinkingBlock(text="hmm", signature="sig"),
        ToolUseBlock(call_id="t1", tool_name="Read", arguments={"file_path": "/a"}),
    ]


def test_streamed_whitespace_only_text_block_is_dropped_real_text_kept() -> None:
    provider = _provider(
        [_START, *_empty_text(0, "\n\n"), *_empty_text(1, "Done."), _stop_delta("end_turn"), _STOP]
    )
    response = provider.complete_streaming(_REQ, _discard)
    assert response.content == [TextBlock(text="Done.")]


def test_streamed_message_of_only_an_empty_text_block_is_empty_not_blank() -> None:
    # Well-formed and empty: the ReAct policy refuses to record an empty
    # assistant turn (``llm_empty_response``) instead of writing an unsendable one.
    provider = _provider([_START, *_empty_text(0), _stop_delta("end_turn"), _STOP])
    response = provider.complete_streaming(_REQ, _discard)
    assert response.stop_reason == "end_turn"
    assert response.content == []


def test_batch_response_with_an_empty_text_block_drops_it() -> None:
    payload = {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": ""},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }
    response = AnthropicProvider(api_key="k")._parse_response(payload)  # noqa: SLF001
    assert response.content == [
        ToolUseBlock(call_id="t1", tool_name="Read", arguments={"file_path": "/a"})
    ]


def test_recorded_empty_text_block_is_not_resent() -> None:
    request = LLMRequest(
        model="claude-opus-5",
        messages=[
            Message(role="user", content=[TextBlock(text="hi")]),
            Message(
                role="assistant",
                content=[
                    TextBlock(text=""),
                    TextBlock(text="ok"),
                    ToolUseBlock(call_id="t1", tool_name="Read", arguments={}),
                ],
            ),
        ],
        max_tokens=50,
    )
    body = AnthropicProvider(api_key="k")._build_request_body(request)  # noqa: SLF001
    assistant = body["messages"][1]["content"]
    assert [block["type"] for block in assistant] == ["text", "tool_use"]
    assert assistant[0]["text"] == "ok"
