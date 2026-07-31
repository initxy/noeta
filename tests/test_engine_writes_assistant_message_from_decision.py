"""The Engine is the sole writer of ``RuntimeState.messages``.

A Policy that produced an LLM-shaped Decision attaches the assistant turn it
observed as ``decision.assistant_message``; the Engine appends it and emits
``MessagesAppended`` *before* it dispatches the Decision body, so the
recording orders the model's words ahead of their effects. A Decision that
carries no assistant message falls back to synthesising one from
``FinishDecision.answer`` — exactly one ``MessagesAppended`` either way,
never a duplicate.
"""

from __future__ import annotations

from typing import Sequence

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.fold import messages_from_appended
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import Decision, FinishDecision
from noeta.protocols.messages import (
    Message,
    TextBlock,
    ToolUseBlock,
)
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _build_engine(
    decisions: Sequence[Decision],
) -> tuple[Engine, InMemoryEventLog, InMemoryContentStore, str, str]:
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubScriptedPolicy(list(decisions)),
    )
    task = engine.create_task(goal="t", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None
    return engine, log, cs, lease.lease_id, task.task_id


def test_finish_without_assistant_message_falls_through_phase0_path() -> None:
    """Stub Policy never attaches assistant_message → the Engine synthesises
    a TextBlock-shaped assistant Message from ``decision.answer``."""
    engine, log, _cs, lease_id, task_id = _build_engine(
        [FinishDecision(answer="hello")]
    )
    finished = engine.run_one_step(_get_task(engine, task_id), lease_id=lease_id)

    msg_events = [e for e in log.read(task_id) if e.type == "MessagesAppended"]
    assert len(msg_events) == 1
    payload_msg = messages_from_appended(msg_events[0], _cs)[0]
    assert isinstance(payload_msg, Message)
    assert payload_msg.role == "assistant"
    assert payload_msg.content == [TextBlock(text="hello")]
    assert finished.runtime.messages == [payload_msg]


def test_finish_with_attached_assistant_message_emits_before_terminal() -> None:
    """When the Policy attaches ``assistant_message`` the Engine emits
    MessagesAppended for it ahead of TaskCompleted, and the ``_finish``
    fallback must stay quiet."""
    attached = Message(
        role="assistant", content=[TextBlock(text="here it is")]
    )
    engine, log, _cs, lease_id, task_id = _build_engine(
        [FinishDecision(answer="here it is", assistant_message=attached)]
    )
    finished = engine.run_one_step(_get_task(engine, task_id), lease_id=lease_id)

    types = [e.type for e in log.read(task_id)]
    # One append, not two: the fallback must not fire on top of the attachment.
    assert types.count("MessagesAppended") == 1
    assert types.index("MessagesAppended") < types.index("TaskCompleted")

    msg_event = next(e for e in log.read(task_id) if e.type == "MessagesAppended")
    assert messages_from_appended(msg_event, _cs) == [attached]
    assert finished.runtime.messages == [attached]


def test_assistant_message_preserves_mixed_text_and_tool_use_blocks() -> None:
    """The ReActPolicy happy path blends natural-language ``TextBlock`` with
    ``ToolUseBlock``; the recorded payload must preserve block order and
    types, since the provider replays them back as conversation history."""
    mixed = Message(
        role="assistant",
        content=[
            TextBlock(text="let me check"),
            ToolUseBlock(
                call_id="c-1",
                tool_name="lookup",
                arguments={"q": "weather"},
            ),
        ],
    )
    engine, log, _cs, lease_id, task_id = _build_engine(
        [FinishDecision(answer="done", assistant_message=mixed)]
    )
    engine.run_one_step(_get_task(engine, task_id), lease_id=lease_id)

    msg_event = next(e for e in log.read(task_id) if e.type == "MessagesAppended")
    recorded = messages_from_appended(msg_event, _cs)[0]
    assert isinstance(recorded, Message)
    assert isinstance(recorded.content[0], TextBlock)
    assert isinstance(recorded.content[1], ToolUseBlock)
    assert recorded == mixed


def _get_task(engine: Engine, task_id: str):  # noqa: ANN202
    """Rebuild the Task by folding the log, so its state matches the
    recording rather than a hand-built wrapper."""
    from noeta.core.fold import fold

    return fold(engine._event_log, engine._content_store, task_id)
