"""Extended-thinking end-to-end acceptance (Slices B + C).

Drives the *real* Engine + ReActPolicy + ThreeSegmentComposer over a
two-turn run backed by a FakeLLMProvider. Turn 1 emits a ThinkingBlock
ahead of a ``tool_use``; the Engine records it out-of-band
(``AssistantThinkingRecorded`` → ``ContextState.thinking_by_call_id``),
and turn 2's compose re-attaches it — so the SECOND provider request
carries the thinking ahead of the assistant turn's ``tool_use``, exactly
what an Anthropic extended-thinking continuation needs.

This closes the loop the per-layer tests pin individually:
``react`` extraction (test_policy_react), composer re-attach
(test_three_segment_composer), and the Engine/fold rebuild
(thinking stays out of persisted history). Here the slice's writer and
reader meet through a live run.
"""

from __future__ import annotations

from noeta.context.composer import ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies._control_translate import (
    ControlToggles,
    translate_control_tool,
)
from noeta.policies.react import ReActPolicy
from noeta.protocols.decisions import (
    SpawnSubtaskDecision,
    StatePatchDecision,
    YieldForHumanDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from noeta.runtime.llm import RuntimeLLMClient
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool


def _run_two_turn_thinking() -> tuple[FakeLLMProvider, object, object, str, ThinkingBlock]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    cs = InMemoryContentStore()

    thinking = ThinkingBlock(text="reason about echo", signature="sig-1")
    r1 = LLMResponse(
        stop_reason="tool_use",
        content=[
            thinking,
            ToolUseBlock(call_id="c1", tool_name="echo", arguments={"k": "hi"}),
        ],
    )
    r2 = LLMResponse(stop_reason="end_turn", content=[TextBlock(text="done")])
    provider = FakeLLMProvider(responses=[r1, r2])
    client = RuntimeLLMClient(provider=provider, event_log=log, content_store=cs)

    tools = {"echo": FakeTool(name="echo", script={("hi",): "ok"})}
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=ThreeSegmentComposer(
            system_prompt="", tools=tools, content_store=cs
        ),
        policy=ReActPolicy(
            llm=client, tools=tools, system_prompt="", model="gpt-4o"
        ),
        tools=tools,
        tool_runtime=ToolRuntime(event_log=log, content_store=cs),
    )
    task = engine.create_task(goal="g", policy_name="react")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None
    engine.run_one_step(task, lease_id=lease.lease_id)
    return provider, log, cs, task.task_id, thinking


def test_thinking_reattached_into_continuation_request() -> None:
    provider, _log, _cs, _task_id, thinking = _run_two_turn_thinking()

    # Two provider round-trips: the tool_use turn, then the continuation.
    assert len(provider.received_requests) == 2
    second = provider.received_requests[1]

    assistant_msgs = [m for m in second.messages if m.role == "assistant"]
    assert assistant_msgs, "continuation request should carry the prior assistant turn"
    first_assistant = assistant_msgs[0]
    # thinking is re-attached at the head, ahead of the tool_use it preceded.
    assert first_assistant.content[0] == thinking
    assert any(
        isinstance(b, ToolUseBlock) and b.call_id == "c1"
        for b in first_assistant.content
    )


def test_thinking_stays_out_of_persisted_history() -> None:
    """The re-attach is View-only: the persisted ``runtime.messages``
    (what a fold rebuilds) never contains the ThinkingBlock — that is what
    keeps a resume's rebuilt history identical. The signature lives solely in the
    ``thinking_by_call_id`` slice."""
    _provider, log, cs, task_id, thinking = _run_two_turn_thinking()

    refolded = fold(log, cs, task_id, ignore_snapshots=True)

    assert refolded.context.thinking_by_call_id == {"c1": [thinking]}
    for msg in refolded.runtime.messages:
        assert not any(isinstance(b, ThinkingBlock) for b in msg.content)


# ---------------------------------------------------------------------------
# Regression: control tools (spawn / todo_write / ask / plan-mode) also carry
# their preceding extended-thinking signature through
# AssistantThinkingRecorded → thinking_by_call_id → composer re-attach.
# Previously only the normal ToolCallsDecision path did this, so a reasoning
# model that emitted a ThinkingBlock + a control tool_use in one turn would
# lose the signature and Anthropic would return 400 on the continuation.
# ---------------------------------------------------------------------------


def test_translate_control_tool_carries_thinking_on_spawn() -> None:
    """Spawn path (single + fan-out): the returned Decision must expose
    the out-of-band ThinkingBlocks under ``assistant_thinking`` so the
    Engine's ``_append_assistant_message`` records them."""
    thinking = ThinkingBlock(text="why delegate", signature="sig-spawn")
    tool = ToolUseBlock(
        call_id="s1",
        tool_name="spawn_subagent",
        arguments={"agent": "coder", "goal": "write tests"},
    )
    response = LLMResponse(
        stop_reason="tool_use", content=[thinking, tool]
    )
    # The assistant_message side-channel has thinking stripped (mirrors
    # react.py's ``_strip_thinking``).
    assistant_message = Message(role="assistant", content=[tool])
    decision = translate_control_tool(
        response,
        assistant_message,
        toggles=ControlToggles(delegation=True),
    )
    assert isinstance(decision, SpawnSubtaskDecision)
    assert decision.assistant_thinking == (thinking,)


def test_translate_control_tool_carries_thinking_on_todo_write() -> None:
    """todo_write builds a StatePatchDecision (loop-continues through
    handle_state_patch); thinking must ship on the decision."""
    thinking = ThinkingBlock(text="plan todos", signature="sig-todo")
    tool = ToolUseBlock(
        call_id="t1",
        tool_name="todo_write",
        arguments={"items": [{"id": "a", "content": "x", "status": "todo"}]},
    )
    response = LLMResponse(
        stop_reason="tool_use", content=[thinking, tool]
    )
    assistant_message = Message(role="assistant", content=[tool])
    decision = translate_control_tool(
        response,
        assistant_message,
        toggles=ControlToggles(todo_write=True),
    )
    assert isinstance(decision, StatePatchDecision)
    assert decision.assistant_thinking == (thinking,)


def test_translate_control_tool_carries_thinking_on_ask() -> None:
    """ask_user_question (valid) builds a YieldForHumanDecision; thinking
    must ship on it so the yield-suspend path still records the sig."""
    cs = InMemoryContentStore()
    thinking = ThinkingBlock(text="need clarification", signature="sig-ask")
    tool = ToolUseBlock(
        call_id="q1",
        tool_name="ask_user_question",
        arguments={
            "questions": [{"id": "q", "question": "which color?"}],
            "reason": "scope unclear",
        },
    )
    response = LLMResponse(
        stop_reason="tool_use", content=[thinking, tool]
    )
    assistant_message = Message(role="assistant", content=[tool])
    decision = translate_control_tool(
        response,
        assistant_message,
        toggles=ControlToggles(ask_user_question=True),
        content_store=cs,
    )
    assert isinstance(decision, YieldForHumanDecision)
    assert decision.assistant_thinking == (thinking,)


def test_translate_control_tool_thinking_empty_for_non_reasoning() -> None:
    """Non-reasoning model: no ThinkingBlock in the response →
    ``assistant_thinking`` defaults to empty tuple, byte-safe (no events
    are added to a non-reasoning recording)."""
    tool = ToolUseBlock(
        call_id="t1",
        tool_name="todo_write",
        arguments={"items": [{"id": "a", "content": "x", "status": "todo"}]},
    )
    response = LLMResponse(stop_reason="tool_use", content=[tool])
    assistant_message = Message(role="assistant", content=[tool])
    decision = translate_control_tool(
        response,
        assistant_message,
        toggles=ControlToggles(todo_write=True),
    )
    assert isinstance(decision, StatePatchDecision)
    assert decision.assistant_thinking == ()


def test_thinking_reattached_after_todo_write_control_tool() -> None:
    """End-to-end mirror of ``test_thinking_reattached_into_continuation_request``
    but for the ``todo_write`` CONTROL tool (→ StatePatchDecision, which
    loop-continues instead of spawning a ToolRuntime call). The reasoning
    model emits [ThinkingBlock, todo_write tool_use] in turn 1; after the
    state patch, turn 2's compose MUST re-attach the thinking block in
    front of the assistant tool_use — this is what prevents the Anthropic
    400 invalid_request_error on extended-thinking continuations that
    follow a control-tool turn."""
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    cs = InMemoryContentStore()

    thinking = ThinkingBlock(text="let me plan the work", signature="sig-plan")
    r1 = LLMResponse(
        stop_reason="tool_use",
        content=[
            thinking,
            ToolUseBlock(
                call_id="c1",
                tool_name="todo_write",
                arguments={
                    "items": [
                        {"id": "a", "content": "step one", "status": "todo"},
                    ],
                },
            ),
        ],
    )
    r2 = LLMResponse(stop_reason="end_turn", content=[TextBlock(text="done")])
    provider = FakeLLMProvider(responses=[r1, r2])
    client = RuntimeLLMClient(provider=provider, event_log=log, content_store=cs)

    tools: dict = {}
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=ThreeSegmentComposer(
            system_prompt="", tools=tools, content_store=cs
        ),
        policy=ReActPolicy(
            llm=client,
            tools=tools,
            system_prompt="",
            model="gpt-4o",
            todo_write_enabled=True,
        ),
        tools=tools,
        tool_runtime=ToolRuntime(event_log=log, content_store=cs),
    )
    task = engine.create_task(goal="g", policy_name="react")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None
    engine.run_one_step(task, lease_id=lease.lease_id)

    # Turn 1 was todo_write (StatePatchDecision → loop-continue), so we
    # expect two provider round-trips: turn 1's todo_write call, and turn
    # 2's continuation request.
    assert len(provider.received_requests) == 2, (
        f"expected two LLM round-trips, got "
        f"{len(provider.received_requests)}: check todo_write loop-continue"
    )
    second = provider.received_requests[1]

    # --- Writer check: AssistantThinkingRecorded stashed it keyed by c1.
    refolded = fold(log, cs, task.task_id, ignore_snapshots=True)
    assert refolded.context.thinking_by_call_id == {"c1": [thinking]}, (
        "StatePatchDecision path must still write thinking_by_call_id"
    )

    # --- Reader check: the composer re-attaches it in the continuation.
    assistant_msgs = [m for m in second.messages if m.role == "assistant"]
    assert assistant_msgs, (
        "continuation request should carry the prior assistant turn"
    )
    first_assistant = assistant_msgs[0]
    assert first_assistant.content[0] == thinking, (
        "thinking must be re-attached at the head of the assistant turn "
        "it preceded (even after a todo_write control tool)"
    )
    assert any(
        isinstance(b, ToolUseBlock) and b.call_id == "c1"
        for b in first_assistant.content
    )
    # And the persisted history never absorbs the signature.
    for msg in refolded.runtime.messages:
        assert not any(isinstance(b, ThinkingBlock) for b in msg.content)

