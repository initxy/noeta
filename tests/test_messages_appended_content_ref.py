"""Issue 14: MessagesAppendedPayload moves to ContentRef shape.

PRD §D + Grill round 1 #3: ``MessagesAppendedPayload`` carries
``messages_ref: ContentRef`` + ``count: int`` instead of an inline
``messages`` list, so the EventLog envelope size stays bounded by
the EventLog's 4 KB ceiling regardless of how large the underlying
message body is.

Tests:

* 4 KB independence: a 5 KB Message body emits without ``PayloadTooLarge``.
* fold reconstructs ``task.runtime.messages`` from the ContentStore body.
* Engine writes ref + count + body in one round-trip.
"""

from __future__ import annotations

from noeta.context.composer import ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.policies.stub import StubFinishPolicy
from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.events import MessagesAppendedPayload
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.task import Task
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _bootstrap() -> tuple[
    Task, Engine, InMemoryEventLog, InMemoryContentStore, str
]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    composer = ThreeSegmentComposer(
        system_prompt="be helpful",
        tools={},
        content_store=content_store,
    )
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=StubFinishPolicy(answer="done"),
    )
    task = engine.create_task(goal="big", policy_name="stub_finish")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    return task, engine, event_log, content_store, lease.lease_id


def test_messages_appended_payload_carries_ref_and_count() -> None:
    task, engine, event_log, content_store, lease_id = _bootstrap()

    engine.append_user_message(task, content=[TextBlock(text="hello")], lease_id=lease_id)

    appended = [
        e for e in event_log.read(task.task_id) if e.type == "MessagesAppended"
    ]
    assert appended, "expected at least one MessagesAppended event"
    payload = appended[-1].payload
    assert isinstance(payload, MessagesAppendedPayload)
    assert payload.count == 1
    assert payload.messages_ref is not None
    body = content_store.get(payload.messages_ref)
    restored = from_canonical_bytes(body)
    assert isinstance(restored, list)
    assert restored[0].role == "user"


def test_5kb_message_does_not_trip_payload_too_large() -> None:
    # The whole reason this slice exists: 4 KB envelope ceiling
    # used to bite tool_result / assistant turns whose body
    # crossed 4 KB. messages_ref keeps the envelope size constant.
    task, engine, _log, _cs, lease_id = _bootstrap()

    engine.append_user_message(task, content=[TextBlock(text="x" * 5000)], lease_id=lease_id)

    # If we got here without PayloadTooLarge, the 5 KB message
    # successfully emitted through the new ref-based payload.
    assert any(m.role == "user" for m in task.runtime.messages)


def test_fold_reconstructs_runtime_messages_from_content_store() -> None:
    task, engine, event_log, content_store, lease_id = _bootstrap()
    engine.append_user_message(task, content=[TextBlock(text="x" * 5000)], lease_id=lease_id)

    rebuilt = fold(event_log, content_store, task.task_id)

    assert len(rebuilt.runtime.messages) >= 1
    big = rebuilt.runtime.messages[0]
    assert isinstance(big, Message)
    assert big.role == "user"
    assert any(
        isinstance(b, TextBlock) and len(b.text) == 5000 for b in big.content
    )


def test_appended_payload_envelope_stays_under_4kb_for_huge_message() -> None:
    """A 50 KB Message must still produce a ≤4 KB envelope."""
    task, engine, event_log, content_store, lease_id = _bootstrap()

    engine.append_user_message(task, content=[TextBlock(text="z" * 50_000)], lease_id=lease_id)

    from noeta.protocols.canonical import to_canonical_bytes

    appended = [
        e for e in event_log.read(task.task_id) if e.type == "MessagesAppended"
    ]
    payload_bytes = to_canonical_bytes(appended[-1].payload)
    assert len(payload_bytes) < 4096, (
        f"envelope payload should stay small with ref-based shape; got {len(payload_bytes)}"
    )
