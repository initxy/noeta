"""Engine appends + emits ``decision.assistant_message`` before dispatch.

Per PRD §"Decision extends assistant_message", a
Policy that produces an LLM-shaped Decision attaches the assistant turn
it observed as ``decision.assistant_message``. The Engine — the sole
writer of ``RuntimeState.messages`` — appends and emits a
``MessagesAppended`` event for that Message *before* it dispatches the
Decision body. Phase 0 Stub policies leave ``assistant_message=None``,
in which case the legacy ``_finish`` fallback still synthesises an
assistant Message from ``FinishDecision.answer``.

Issue 10 acceptance cases:

* assistant_message=None on a FinishDecision still works (Phase 0 path)
* assistant_message attached on a FinishDecision is emitted as
  MessagesAppended *before* TaskCompleted
* mixed TextBlock + ToolUseBlock content survives intact through the
  recorded event payload
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
    """Stub Policy never attaches assistant_message → Engine synthesises
    a TextBlock-shaped assistant Message from ``decision.answer`` just
    like Phase 0."""
    engine, log, _cs, lease_id, task_id = _build_engine(
        [FinishDecision(answer="hello")]
    )
    finished = engine.run_one_step(_get_task(engine, task_id), lease_id=lease_id)

    # Exactly one MessagesAppended fires the legacy fallback path.
    msg_events = [e for e in log.read(task_id) if e.type == "MessagesAppended"]
    assert len(msg_events) == 1
    payload_msg = messages_from_appended(msg_events[0], _cs)[0]
    assert isinstance(payload_msg, Message)
    assert payload_msg.role == "assistant"
    assert payload_msg.content == [TextBlock(text="hello")]

    # And it landed in the runtime slice.
    assert finished.runtime.messages == [payload_msg]


def test_finish_with_attached_assistant_message_emits_before_terminal() -> None:
    """Phase 1 path: Policy attaches ``assistant_message``; Engine emits
    MessagesAppended for it ahead of TaskCompleted, and the fallback in
    ``_finish`` does *not* fire (no duplicate)."""
    attached = Message(
        role="assistant", content=[TextBlock(text="here it is")]
    )
    engine, log, _cs, lease_id, task_id = _build_engine(
        [FinishDecision(answer="here it is", assistant_message=attached)]
    )
    finished = engine.run_one_step(_get_task(engine, task_id), lease_id=lease_id)

    types = [e.type for e in log.read(task_id)]
    # Exactly one MessagesAppended (no duplicate via the legacy fallback)
    assert types.count("MessagesAppended") == 1
    # ...and it lands strictly before TaskCompleted.
    assert types.index("MessagesAppended") < types.index("TaskCompleted")

    msg_event = next(e for e in log.read(task_id) if e.type == "MessagesAppended")
    assert messages_from_appended(msg_event, _cs) == [attached]
    assert finished.runtime.messages == [attached]


def test_assistant_message_preserves_mixed_text_and_tool_use_blocks() -> None:
    """A Decision can carry an assistant Message whose content blends
    natural-language ``TextBlock`` and ``ToolUseBlock`` (ReActPolicy
    happy path). The recorded payload preserves block order + types."""
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
    """Reconstruct the in-memory Task object for a run.

    ``create_task`` already happened inside ``_build_engine``; for the
    Phase 0 helpers we just need a fresh Task wrapper with the same id.
    Use ``fold`` so the resulting state matches the recording.
    """
    from noeta.core.fold import fold

    return fold(engine._event_log, engine._content_store, task_id)
