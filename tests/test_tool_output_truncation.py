"""Microcompact — truncate oversized tool results on the way into the log (noeta shape).

Test classes covering the design contract:
  1. truncate_tool_output pure-function unit tests
  2. Config validation (Engine / CodeSessionConfig: 0 / negative -> ValueError)
  3. Default-off, zero impact — matches current behavior (no limit set, same scenario, dict/str passed through unchanged)
  4. End-to-end with limit on — (a) truncated form in the message stream; (b) ToolResultRecorded.output_ref holds the full original;
     (c) the model's next LLMRequest also sees the truncated form
  6. Product-layer pass-through — CodeSessionConfig.tool_output_inline_limit reaches the engine
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from noeta.context.composer import _COMPOSER_VERSION, ThreeSegmentComposer
from noeta.core._decision_handlers import (
    _validate_tool_output_inline_limit,
    truncate_tool_output,
)
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.react import ReActPolicy
from noeta.protocols.events import (
    EventEnvelope,
    ToolResultRecordedPayload,
)
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool

from tests._sdk_session import coding_replay_budget

# ---------------------------------------------------------------------------
# 1. Pure-function unit tests
# ---------------------------------------------------------------------------


def test_truncate_disabled_when_none() -> None:
    s = "x" * 1000
    assert truncate_tool_output(s, None) is s


def test_truncate_noop_at_boundary() -> None:
    """No truncation when length is exactly at the limit."""
    s = "abcde"
    assert truncate_tool_output(s, 5) == s
    assert truncate_tool_output(s, 6) == s


def test_truncate_drops_excess_and_appends_marker() -> None:
    s = "abcdefghij"  # len=10
    out = truncate_tool_output(s, 4)
    assert out.startswith("abcd")
    assert "6 of 10 chars dropped" in out
    # Lean marker: no ContentStore hash leaked into the model-facing text.
    assert "content ref" not in out
    # Stable ASCII-only marker prefix
    assert "\n...[tool output truncated:" in out


def test_truncate_marker_order_deterministic() -> None:
    """Same input and config across calls -> same output (including dropped/total)."""
    s = "hello world this is a long string" * 3
    r1 = truncate_tool_output(s, 12)
    r2 = truncate_tool_output(s, 12)
    assert r1 == r2


def test_truncate_handles_unicode() -> None:
    s = "你好世界" * 5  # len=20 (Python char count, not bytes)
    out = truncate_tool_output(s, 4)
    assert out.startswith("你好世界")
    assert "16 of 20 chars dropped" in out


# ---------------------------------------------------------------------------
# 2. Config validation
# ---------------------------------------------------------------------------


def test_validate_rejects_zero_and_negative() -> None:
    with pytest.raises(ValueError, match="tool_output_inline_limit must be > 0"):
        _validate_tool_output_inline_limit(0)
    with pytest.raises(ValueError, match="tool_output_inline_limit must be > 0"):
        _validate_tool_output_inline_limit(-5)


def test_engine_constructor_rejects_bad_limit() -> None:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    from noeta.core.composer import PassthroughComposer

    with pytest.raises(ValueError, match="tool_output_inline_limit must be > 0"):
        Engine(
            event_log=event_log,
            content_store=content_store,
            composer=PassthroughComposer(),
            tool_output_inline_limit=0,
        )


# ---------------------------------------------------------------------------
# Shared helper: build a ReActPolicy + Engine with a tool, run one turn whose
# tool produces oversized output
# ---------------------------------------------------------------------------

_SYSTEM = "You are an agent. Work the task then finish."


def _big_echo_tool(size: int = 2000) -> FakeTool:
    """FakeTool named echo that returns a string of the given size."""
    content = "~" * size
    return FakeTool(
        name="echo",
        script={("big",): content},
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )


def _engine_and_run(*, tool_output_inline_limit: int | None, tool_size: int = 2000):
    """Run both turns (tool -> end_turn), return (task, engine, event_log, content_store)."""
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    wire_default_observers(event_log, dispatcher)

    tool = _big_echo_tool(tool_size)
    tools = {tool.name: tool}

    def policy_fn(llm):
        return ReActPolicy(
            llm=llm,
            tools=tools,
            system_prompt=_SYSTEM,
            model="stub-model",
            max_steps=5,
            composer_version=_COMPOSER_VERSION,
        )

    # Round 1: LLM decides to call echo. Round 2: end_turn.
    call_id = "call-big-1"
    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id=call_id,
                        tool_name="echo",
                        arguments={"text": "big"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
                raw={"id": "r1"},
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="done")],
                usage=Usage(uncached=1, output=1),
                raw={"id": "r2"},
            ),
        ]
    )
    llm = RuntimeLLMClient(
        provider=provider, event_log=event_log, content_store=content_store
    )
    policy = policy_fn(llm)
    composer = ThreeSegmentComposer(
        system_prompt=_SYSTEM,
        tools=tools,
        content_store=content_store,
    )
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools=tools,
        tool_output_inline_limit=tool_output_inline_limit,
    )
    task = engine.create_task(goal="run the echo", policy_name="react")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    engine.append_user_message(task, content=[TextBlock(text="run the echo")], lease_id=lease.lease_id)
    engine.run_one_step(task, lease_id=lease.lease_id)
    return task, engine, event_log, content_store, call_id


# ---------------------------------------------------------------------------
# 3. Default off, zero impact
# ---------------------------------------------------------------------------


def test_default_no_limit_keeps_dict_and_full_string() -> None:
    """None = behavior unchanged across the board — dict output stays dict, string stays full."""
    task, engine, event_log, content_store, _ = _engine_and_run(
        tool_output_inline_limit=None, tool_size=2000
    )
    assert task.status == "terminal"
    folded = fold(event_log, content_store, task.task_id)
    tool_msg = [m for m in folded.runtime.messages if m.role == "tool"][0]
    block: ToolResultBlock = tool_msg.content[0]  # type: ignore[assignment]
    # Default off: skips _coerce_inline_output -> string passed through (matches baseline)
    assert isinstance(block.output, str)
    assert block.output == "~" * 2000
    assert "tool output truncated" not in block.output


# ---------------------------------------------------------------------------
# 4. End-to-end with limit on — truncated message / full recording / model request sees truncation
# ---------------------------------------------------------------------------


def test_limit_truncates_inline_but_keeps_recorded_full() -> None:
    tool_size = 2000
    limit = 120
    task, engine, event_log, content_store, call_id = _engine_and_run(
        tool_output_inline_limit=limit, tool_size=tool_size
    )
    assert task.status == "terminal"

    # (a) Message stream: truncated form
    folded = fold(event_log, content_store, task.task_id)
    tool_msg = [m for m in folded.runtime.messages if m.role == "tool"][0]
    block: ToolResultBlock = tool_msg.content[0]  # type: ignore[assignment]
    inline = block.output
    assert isinstance(inline, str)
    assert inline.startswith("~" * limit)
    assert f"{tool_size - limit} of {tool_size} chars dropped" in inline
    # Lean marker: the model-facing inline form carries NO ContentStore hash
    # (the model has no ref-deref tool). The full body is recovered from the
    # ToolResultRecorded.output_ref event below, not from the prompt text.
    assert "content ref" not in inline

    # (b) ToolResultRecorded.output_ref deref = full original
    events = list(event_log.read(task.task_id))
    trr = [e for e in events if e.type == "ToolResultRecorded"]
    assert len(trr) == 1
    trr_p: ToolResultRecordedPayload = trr[0].payload
    raw = content_store.get(trr_p.output_ref)
    decoded = json.loads(raw.decode("utf-8"))
    assert decoded == "~" * tool_size  # JSON-encoded original output

    # (c) The model's next LLMRequest is also the truncated form — read the
    # request_ref in LLMRequestStarted (the request after the tool call, before end_turn)
    llm_request_bodies = _llm_request_bodies(events, content_store)
    assert len(llm_request_bodies) >= 2  # at least the tool -> end_turn two rounds
    second_req = llm_request_bodies[1]  # round 2 (after tool result, end_turn)
    # Flatten the user/tool-role message content in the request body, check for the truncated string
    req_text_blob = json.dumps(second_req, default=str)
    # Truncation suffix must appear in the request (meaning the model saw the truncated version)
    assert f"{tool_size - limit} of {tool_size} chars dropped" in req_text_blob
    # The full original must never appear in the next request (or truncation was pointless)
    assert ("~" * tool_size) not in req_text_blob


def _llm_request_bodies(events: list[EventEnvelope], cs) -> list[dict[str, Any]]:
    """Extract the JSON bodies deref'd from LLMRequestStarted.request_ref, in order of appearance."""
    out = []
    for e in events:
        if e.type != "LLMRequestStarted":
            continue
        p = e.payload
        ref = getattr(p, "request_ref", None)
        if ref is None:
            continue
        raw = cs.get(ref)
        out.append(json.loads(raw.decode("utf-8")))
    return out


# ---------------------------------------------------------------------------
# 6. Product-layer pass-through — CodeSessionConfig -> Engine takes effect
# ---------------------------------------------------------------------------


def _end_resp(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _tool_resp(call_id: str, tool_name: str = "echo") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id, tool_name=tool_name, arguments={"text": "big"}
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "tu"},
    )


def test_product_limit_propagates_to_handler_via_custom_echo(tmp_path: Path) -> None:
    """tool_output_inline_limit -> engine -> handler takes effect.

    Approach: test three-layer pass-through via the isomorphic chain
    build_session_inputs -> SessionInputs -> Engine, the same construction
    the production ``SdkHost`` runs (byte-identical to the product layer)."""
    from noeta.execution.builder import build_session_inputs, derive_compaction_config

    ws = tmp_path / "ws"
    ws.mkdir()
    cs = InMemoryContentStore()
    limit = 50
    big_echo = FakeTool(
        name="echo",
        script={("big",): "X" * 500},
    )

    inputs = build_session_inputs(
        workspace_dir=ws,
        system_prompt=_SYSTEM,
        allowed_tools=frozenset({"echo"}),
        content_store=cs,
        model="stub-model",
        compaction=derive_compaction_config("stub-model"),
        budget=coding_replay_budget(None),
        custom_tools={"echo": big_echo},
        tool_output_inline_limit=limit,
    )
    assert inputs.tool_output_inline_limit == limit
    # SessionInputs -> Engine is isomorphic to the Engine() at line 726 of CodeSessionConfig.prepare()
    tools_d = dict(inputs.tools)
    dispatcher = InMemoryDispatcher()
    el = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(el, dispatcher)
    llm = RuntimeLLMClient(
        provider=FakeLLMProvider(
            responses=[
                _tool_resp("c1", "echo"),
                _end_resp("done"),
            ]
        ),
        event_log=el,
        content_store=cs,
    )
    engine = Engine(
        event_log=el,
        content_store=cs,
        composer=inputs.composer,
        policy=inputs.policy_factory(llm),
        tools=tools_d,
        hooks=inputs.hooks,
        tool_output_inline_limit=inputs.tool_output_inline_limit,
    )
    task = engine.create_task(goal="hi", policy_name="react")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    engine.append_user_message(task, content=[TextBlock(text="hi")], lease_id=lease.lease_id)
    final = engine.run_one_step(task, lease_id=lease.lease_id)
    assert final.status == "terminal"
    folded = fold(el, cs, task.task_id)
    tool_blocks = [
        b
        for m in folded.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert len(tool_blocks) == 1
    inline = tool_blocks[0].output
    assert isinstance(inline, str)
    assert inline.startswith("X" * limit)
    assert f"{500 - limit} of 500 chars dropped" in inline
    # Red line: ToolResultRecorded still records the full output
    trrs = [e for e in el.read(task.task_id) if e.type == "ToolResultRecorded"]
    assert len(trrs) == 1
    payload: ToolResultRecordedPayload = trrs[0].payload
    raw = cs.get(payload.output_ref)
    assert json.loads(raw.decode("utf-8")) == "X" * 500


def test_product_default_none_zero_impact(tmp_path: Path) -> None:
    """Product layer passes no explicit limit -> SessionInputs.tool_output_inline_limit is None."""
    from noeta.execution.builder import build_session_inputs, derive_compaction_config

    ws = tmp_path / "ws"
    ws.mkdir()
    cs = InMemoryContentStore()
    big_echo = FakeTool(
        name="echo",
        script={("big",): "Y" * 500},
    )
    inputs = build_session_inputs(
        workspace_dir=ws,
        system_prompt=_SYSTEM,
        allowed_tools=frozenset({"echo"}),
        content_store=cs,
        model="stub-model",
        compaction=derive_compaction_config("stub-model"),
        budget=coding_replay_budget(None),
        custom_tools={"echo": big_echo},
        # <- Explicitly omitted, equivalent to the CodeSessionConfig default of None
    )
    assert inputs.tool_output_inline_limit is None


# ---------------------------------------------------------------------------
# Extra guard: ToolResult.output_ref is populated by ToolRuntime (frozen-slot semantics)
# ---------------------------------------------------------------------------


def test_tool_runtime_populates_output_ref() -> None:
    """After ToolRuntime.invoke, the returned ToolResult.output_ref is not None."""
    from noeta.runtime.tool import ToolRuntime

    dispatcher = InMemoryDispatcher()
    el = InMemoryEventLog(lease_validator=dispatcher)
    cs = InMemoryContentStore()
    wire_default_observers(el, dispatcher)
    rt = ToolRuntime(event_log=el, content_store=cs)
    tool = FakeTool(
        name="t",
        script={tuple(): "hello"},
    )
    from noeta.protocols.decisions import ToolCall

    task_id = "tid"
    # ToolRuntime.emit goes through the lease-checked path -> must lease first (no
    # TaskCreated is fine, since InMemoryEventLog only checks that lease_id is in the
    # dispatcher; but the task must still be created). Simplest: register via
    # Engine.create_task + lease.
    from noeta.core.composer import PassthroughComposer
    from noeta.core.engine import Engine

    e = Engine(event_log=el, content_store=cs, composer=PassthroughComposer())
    e.create_task(goal="g", policy_name="p", task_id=task_id)
    dispatcher.enqueue(task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None

    tc = ToolCall(call_id="c1", tool_name="t", arguments={})
    result = rt.invoke(
        tool, tc, task_id=task_id, lease_id=lease.lease_id, trace_id="trace"
    )
    assert result.output_ref is not None
    assert hasattr(result.output_ref, "hash")
    # Matches the ref in the event
    ev = [e for e in el.read("tid") if e.type == "ToolResultRecorded"][0]
    assert ev.payload.output_ref.hash == result.output_ref.hash
