"""``Engine.append_user_message`` round-trips through the EventLog.

Regression for the Phase 1 demo bug: directly mutating
``task.runtime.messages`` outside Engine emits no ``MessagesAppended``
event, so ``fold(events)`` rebuilds an empty conversation.

The public seeding API must:

* emit exactly one ``MessagesAppended`` event with a typed ``Message``
* leave ``task.runtime.messages`` in sync with the EventLog
* round-trip cleanly through ``fold(events)`` so a rebuilt task is
  byte-equal to the in-memory task
"""

from __future__ import annotations

import pytest

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.fold import fold, messages_from_appended
from noeta.policies.stub import StubFinishPolicy
from noeta.protocols.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _setup() -> tuple[Engine, InMemoryEventLog, InMemoryContentStore, str, str]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=StubFinishPolicy(answer="ok"),
    )
    task = engine.create_task(goal="seed me", policy_name="stub_finish")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    engine.append_user_message(task, content=[TextBlock(text="hello there")], lease_id=lease.lease_id)
    return engine, event_log, content_store, task.task_id, lease.lease_id


def test_emits_typed_messages_appended_event() -> None:
    _engine, log, cs, task_id, _lease = _setup()

    appended = [e for e in log.read(task_id) if e.type == "MessagesAppended"]
    assert len(appended) == 1
    msgs = messages_from_appended(appended[0], cs)
    msg = msgs[0]
    assert isinstance(msg, Message)
    assert msg.role == "user"
    assert isinstance(msg.content[0], TextBlock)
    assert msg.content[0].text == "hello there"


def test_in_memory_state_matches_event_payload() -> None:
    engine, log, cs, task_id, _lease = _setup()

    # Engine returned a task instance whose runtime.messages we want to
    # confirm — re-fetch via fold so we are not depending on the captured
    # reference being mutated.
    in_memory = fold(log, cs, task_id)
    appended = [e for e in log.read(task_id) if e.type == "MessagesAppended"]
    assert in_memory.runtime.messages == messages_from_appended(appended[0], cs)
    assert engine is not None  # silence ARG


def test_fold_round_trip_byte_equal() -> None:
    engine, log, cs, task_id, lease_id = _setup()
    # Drive the task to terminal so we exercise the full event stream.
    # StubFinishPolicy returns FinishDecision on first decide; this also
    # writes a TaskSnapshot which fold() must agree with.
    # Need to fetch the live in-memory Task to feed run_one_step.
    live = fold(log, cs, task_id)
    engine.run_one_step(live, lease_id=lease_id)

    rebuilt = fold(log, cs, task_id)
    assert rebuilt.runtime.messages == live.runtime.messages
    # Sanity: the seeded user message survives fold.
    assert any(m.role == "user" for m in rebuilt.runtime.messages)


# ---------------------------------------------------------------------------
# D5 — the seam takes ``content: list[Block]``; only TextBlock /
# ImageBlock are allowed in a user turn, and an empty list is rejected.
# ---------------------------------------------------------------------------


def _fresh_leased() -> tuple[Engine, InMemoryEventLog, InMemoryContentStore, object, str]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=StubFinishPolicy(answer="ok"),
    )
    task = engine.create_task(goal="seed me", policy_name="stub_finish")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    return engine, event_log, content_store, task, lease.lease_id


def test_image_bearing_user_turn_round_trips_through_ledger() -> None:
    """A mixed text+image turn lands as the exact ``content`` it was given
    and folds back byte-equal — the ledger carries the ~100-byte
    ``ImageBlock(ContentRef)`` handle, never image bytes."""
    engine, log, cs, task, lease_id = _fresh_leased()
    ref = cs.put(b"\x89PNG fake bytes", media_type="image/png")
    content = [TextBlock(text="look at this"), ImageBlock(source=ref)]
    engine.append_user_message(task, content=content, lease_id=lease_id)

    appended = [e for e in log.read(task.task_id) if e.type == "MessagesAppended"]
    assert len(appended) == 1
    msg = messages_from_appended(appended[0], cs)[0]
    assert msg.role == "user"
    assert msg.content == content
    assert isinstance(msg.content[1], ImageBlock)
    assert msg.content[1].source == ref
    # fold round-trips the typed blocks identically.
    rebuilt = fold(log, cs, task.task_id)
    assert rebuilt.runtime.messages[-1].content == content


def test_empty_content_is_rejected() -> None:
    engine, _log, _cs, task, lease_id = _fresh_leased()
    with pytest.raises(ValueError):
        engine.append_user_message(task, content=[], lease_id=lease_id)


@pytest.mark.parametrize(
    "bad_block",
    [
        ThinkingBlock(text="secret reasoning"),
        ToolUseBlock(call_id="c1", tool_name="echo", arguments={}),
        ToolResultBlock(call_id="c1", output="x", success=True),
    ],
)
def test_non_user_block_is_rejected(bad_block: object) -> None:
    """A user turn may only carry TextBlock / ImageBlock — a thinking or
    tool block routed through the user channel is a clear ValueError, and
    no ``MessagesAppended`` is emitted."""
    engine, log, _cs, task, lease_id = _fresh_leased()
    with pytest.raises(ValueError):
        engine.append_user_message(
            task, content=[TextBlock(text="ok"), bad_block], lease_id=lease_id
        )
    assert not [e for e in log.read(task.task_id) if e.type == "MessagesAppended"]
