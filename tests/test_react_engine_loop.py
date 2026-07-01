"""Issue 13: end-to-end ReActPolicy + Engine loop integration.

Drives ``Engine.run_one_step`` with a real :class:`ReActPolicy` wired to
:class:`RuntimeLLMClient` + :class:`FakeLLMProvider` + :class:`FakeTool`,
mirroring the production hot path. Asserts:

* The Engine reaches ``terminal`` in one ``run_one_step`` after the
  scripted ``tool_use → tool_use → end_turn`` cycle.
* Each LLM round produces exactly 3 LLM events
  (``LLMRequestStarted`` / ``LLMResponseRecorded`` / ``LLMRequestFinished``)
  on the stream.
* ``MessagesAppended`` payloads are typed :class:`Message` instances
  (Phase 1 contract).
* The Decision-derived ``assistant_message`` carrying mixed Block
  content (Text + ToolUse) round-trips intact into the recorded
  event payload.
* The terminal ``TaskCompleted.answer`` matches the joined TextBlocks
  from the final ``end_turn`` LLMResponse.
"""

from __future__ import annotations

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.fold import messages_from_appended
from noeta.policies.react import ReActPolicy
from noeta.protocols.events import EventEnvelope
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool


def _three_step_react_script() -> list[LLMResponse]:
    """Script: ``tool_use → tool_use → end_turn``. Mixed Text + ToolUse
    content on the tool_use rounds to exercise the
    ``assistant_message`` round-trip with multiple Block types."""
    return [
        LLMResponse(
            stop_reason="tool_use",
            content=[
                TextBlock(text="let me call echo once"),
                ToolUseBlock(
                    call_id="c-1",
                    tool_name="echo",
                    arguments={"text": "hi"},
                ),
            ],
        ),
        LLMResponse(
            stop_reason="tool_use",
            content=[
                TextBlock(text="one more time"),
                ToolUseBlock(
                    call_id="c-2",
                    tool_name="echo",
                    arguments={"text": "hello"},
                ),
            ],
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[
                TextBlock(text="all done"),
                TextBlock(text="goodbye"),
            ],
        ),
    ]


def _build_engine_and_run() -> tuple[
    InMemoryEventLog, InMemoryContentStore, str, FakeLLMProvider
]:
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    provider = FakeLLMProvider(responses=_three_step_react_script())
    llm = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=cs
    )
    tool = FakeTool(
        name="echo", script={("hi",): "echo:hi", ("hello",): "echo:hello"}
    )
    policy = ReActPolicy(
        llm=llm,
        tools={"echo": tool},
        system_prompt="be a helpful assistant",
        model="gpt-test",
    )
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=policy,
        tools={"echo": tool},
    )
    task = engine.create_task(goal="say hi twice", policy_name="react")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w-test")
    assert lease is not None
    engine.run_one_step(task, lease_id=lease.lease_id)
    return log, cs, task.task_id, provider


def _llm_events(events: list[EventEnvelope]) -> list[str]:
    return [e.type for e in events if e.type.startswith("LLM")]


def test_three_step_react_loop_reaches_terminal() -> None:
    """End-to-end: ``tool_use → tool_use → end_turn`` script drives the
    Engine to ``terminal`` in a single ``run_one_step`` call."""
    log, _cs, task_id, provider = _build_engine_and_run()
    # 3 scripted LLM responses must all have been consumed.
    assert len(provider.received_requests) == 3
    # Final event on the stream must be ``TaskCompleted``.
    types = [e.type for e in log.read(task_id)]
    assert types[-1] == "TaskCompleted"
    # And the task transitioned through TaskStarted → … → TaskCompleted.
    assert types[0] == "TaskCreated"
    assert "TaskStarted" in types


def test_each_llm_round_emits_three_llm_events() -> None:
    """The three-event LLM contract fires once per round.
    Three scripted responses → 9 LLM-prefixed events, perfectly grouped
    in 3-event blocks of ``Started / Recorded / Finished``."""
    log, _cs, task_id, _provider = _build_engine_and_run()
    llm_types = _llm_events(log.read(task_id))
    expected = [
        "LLMRequestStarted",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ] * 3
    assert llm_types == expected


def test_messages_appended_payloads_are_typed_message_instances() -> None:
    """Every ``MessagesAppended`` event's payload carries a list of
    :class:`Message` typed dataclasses (Phase 1 contract — no plain
    dicts)."""
    log, _cs, task_id, _provider = _build_engine_and_run()
    appended = [
        e for e in log.read(task_id) if e.type == "MessagesAppended"
    ]
    assert appended, "expected at least one MessagesAppended event"
    for env in appended:
        messages = messages_from_appended(env, _cs)
        assert isinstance(messages, list)
        for m in messages:
            assert isinstance(m, Message)


def test_first_assistant_messages_appended_preserves_text_and_tool_use_blocks() -> None:
    """The first ``MessagesAppended`` event (assistant_message from
    Decision 1) preserves mixed ``TextBlock + ToolUseBlock`` content
    in order — the Phase 1 thinking-not-lost contract."""
    log, _cs, task_id, _provider = _build_engine_and_run()
    first_appended = next(
        e for e in log.read(task_id) if e.type == "MessagesAppended"
    )
    msg = messages_from_appended(first_appended, _cs)[0]
    assert msg.role == "assistant"
    assert len(msg.content) == 2
    assert isinstance(msg.content[0], TextBlock)
    assert msg.content[0].text == "let me call echo once"
    assert isinstance(msg.content[1], ToolUseBlock)
    assert msg.content[1].tool_name == "echo"
    assert msg.content[1].call_id == "c-1"


def test_tool_result_messages_appended_carries_typed_tool_result_blocks() -> None:
    """Between assistant turns, the Engine batches tool results into a
    single ``Message(role="tool", content=[ToolResultBlock, ...])``;
    blocks are typed and pair back to the originating ToolUseBlock by
    ``call_id``."""
    log, _cs, task_id, _provider = _build_engine_and_run()
    # Sequence of MessagesAppended on the stream: assistant-1, tool-1,
    # assistant-2, tool-2, assistant-final (end_turn).
    appended = [
        e for e in log.read(task_id) if e.type == "MessagesAppended"
    ]
    # We expect 5 MessagesAppended events for this script.
    assert len(appended) == 5
    # The second one is the tool-result batch for c-1.
    tool_msg = messages_from_appended(appended[1], _cs)[0]
    assert tool_msg.role == "tool"
    assert len(tool_msg.content) == 1
    assert isinstance(tool_msg.content[0], ToolResultBlock)
    assert tool_msg.content[0].call_id == "c-1"


def test_task_completed_answer_is_joined_text_blocks_from_end_turn() -> None:
    """The terminal ``TaskCompleted.answer`` is the joined ``TextBlock``
    text from the final ``end_turn`` LLMResponse, ``\n``-joined."""
    log, _cs, task_id, _provider = _build_engine_and_run()
    completed = next(
        e for e in log.read(task_id) if e.type == "TaskCompleted"
    )
    assert completed.payload.answer == "all done\ngoodbye"
