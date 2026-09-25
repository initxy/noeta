"""Tool-call arguments reach the history in canonical (key-sorted) order.

The recorded bytes are canonical (``sort_keys``), so a task rebuilt from its
events carries every tool call's arguments in sorted key order. If the live
history kept the model's order instead, the first request after a resume would
render different wire bytes at the first multi-key call and miss the prompt
cache from there on. ``RuntimeLLMClient`` sorts on the live path so both
histories — and therefore both requests — are byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path

from noeta.builtins.providers.impl.anthropic import AnthropicProvider
from noeta.client import Client, Options
from noeta.protocols.canonical import to_canonical
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider

_UNSORTED = {"b": 1, "a": {"d": [{"z": 1, "y": 2}], "c": 2}}


def _tool_call() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            TextBlock(text="Calling."),
            ToolUseBlock(call_id="c1", tool_name="probe", arguments=_UNSORTED),
        ],
        usage=Usage(uncached=1, output=1),
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _keys(value: object) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(k)
            out.extend(_keys(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_keys(v))
    return out


def test_client_returns_tool_arguments_key_sorted_recursively() -> None:
    client = RuntimeLLMClient(
        provider=FakeLLMProvider(responses=[_tool_call()]),
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
    )
    resp = client.complete(
        LLMRequest(
            model="m", messages=[Message(role="user", content=[TextBlock(text="go")])]
        ),
        StepContext(task_id="t", lease_id="l", trace_id="tr"),
    )
    (block,) = [b for b in resp.content if isinstance(b, ToolUseBlock)]
    assert block.arguments == _UNSORTED
    assert _keys(block.arguments) == ["a", "c", "d", "y", "z", "b"]


def test_client_keeps_an_already_sorted_response_unchanged() -> None:
    sorted_resp = LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id="c1", tool_name="p", arguments={"a": 1, "b": 2})],
        usage=Usage(uncached=1, output=1),
    )
    client = RuntimeLLMClient(
        provider=FakeLLMProvider(responses=[sorted_resp]),
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
    )
    resp = client.complete(
        LLMRequest(
            model="m", messages=[Message(role="user", content=[TextBlock(text="go")])]
        ),
        StepContext(task_id="t", lease_id="l", trace_id="tr"),
    )
    assert resp.content == sorted_resp.content


def _assistant_with_call(req: LLMRequest) -> Message:
    for msg in req.messages:
        if msg.role == "assistant" and any(
            isinstance(b, ToolUseBlock) for b in msg.content
        ):
            return msg
    raise AssertionError("no assistant tool call in the request")


def test_live_and_replayed_requests_carry_identical_tool_call_bytes(
    tmp_path: Path,
) -> None:
    """Step 2 of turn 1 reads the LIVE history; turn 2 is composed from the
    folded events. The assistant tool-call message must be byte-identical in
    both — on the neutral canonical form (insertion order kept) and on the
    Anthropic wire body."""
    provider = FakeLLMProvider(responses=[_tool_call(), _end(), _end("again")])
    ws = tmp_path / "ws"
    ws.mkdir()
    client = Client(
        Options(system_prompt="test agent", name="main"),
        provider=provider,
        workspace_dir=ws,
        model="claude-sonnet-5",
    )
    try:
        out = client.start(goal="first")
        client.send_goal(out.task_id, goal="second")
    finally:
        client.shutdown()

    live_req, replayed_req = provider.received_requests[1], provider.received_requests[2]
    live = _assistant_with_call(live_req)
    replayed = _assistant_with_call(replayed_req)
    (live_call,) = [b for b in live.content if isinstance(b, ToolUseBlock)]
    assert list(live_call.arguments) == ["a", "b"]

    assert json.dumps(to_canonical(live)) == json.dumps(to_canonical(replayed))

    anth = AnthropicProvider(api_key="x")

    def wire_call(req: LLMRequest) -> str:
        body = anth._build_request_body(req)
        for m in body["messages"]:
            for blk in m["content"]:
                if blk.get("type") == "tool_use":
                    blk = {k: v for k, v in blk.items() if k != "cache_control"}
                    return json.dumps(blk)
        raise AssertionError("no tool_use on the wire")

    assert wire_call(live_req) == wire_call(replayed_req)
