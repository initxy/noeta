"""Storage-backend-neutral EventLog contract.

Issue 15 introduces the second EventLog adapter (``SqliteEventLog``)
on top of the existing ``InMemoryEventLog``. This module runs the
behavioural contract — three concurrency layers, payload cap, snapshot
lookup, typed-payload restore, subscriber semantics — against **both**
backends so any adapter that satisfies the L0 Protocols continues to
behave identically from a caller's perspective.

Existing ``test_event_log.py`` / ``test_event_log_strict.py`` keep
exercising InMemory-specific call patterns; this suite adds the
behavioural contract guarantees that every adapter has to honour.
"""

from __future__ import annotations

import inspect
import sqlite3
from typing import Any, Callable

import pytest

from noeta.protocols.errors import InvalidLease, PayloadTooLarge, StaleSequence
from noeta.protocols.events import (
    ContextPlanComposedPayload,
    EventEnvelope,
    LeaseGrantedPayload,
    LLMRequestFinishedPayload,
    LLMRequestStartedPayload,
    LLMResponseRecordedPayload,
    MessagesAppendedPayload,
    SubtaskCompletedPayload,
    SubtaskDeniedPayload,
    SubtaskSpawnedPayload,
    TaskCancelledPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskFailedPayload,
    TaskSnapshotPayload,
    TaskStartedPayload,
    TaskStatePatchedPayload,
    TaskSuspendedPayload,
    TaskWokenPayload,
    ToolCallDeniedPayload,
    ToolCallFinishedPayload,
    ToolCallStartedPayload,
    ToolResultRecordedPayload,
)
from noeta.protocols.values import ContentRef
from noeta.protocols.wake import (
    HumanResponseReceived,
    SubtaskCompleted,
    SubtaskResult,
)
from noeta.storage.memory import MAX_PAYLOAD_BYTES, InMemoryDispatcher, InMemoryEventLog
from noeta.storage.sqlite.eventlog import _PAYLOAD_RESTORERS, SqliteEventLog


# ---------------------------------------------------------------------------
# Adapter fixture: parametrise every test over InMemory + Sqlite
# ---------------------------------------------------------------------------


def _make_in_memory(
    *,
    lease_validator: Any = None,
    clock: Callable[[], float] | None = None,
):
    kwargs: dict[str, Any] = {"lease_validator": lease_validator}
    if clock is not None:
        kwargs["clock"] = clock
    return InMemoryEventLog(**kwargs)


def _make_sqlite(
    *,
    lease_validator: Any = None,
    clock: Callable[[], float] | None = None,
):
    kwargs: dict[str, Any] = {"lease_validator": lease_validator}
    if clock is not None:
        kwargs["clock"] = clock
    return SqliteEventLog(":memory:", **kwargs)


@pytest.fixture(params=["memory", "sqlite"])
def make_log(request):
    if request.param == "memory":
        builder = _make_in_memory
    else:
        builder = _make_sqlite

    instances: list[Any] = []

    def _factory(**kwargs):
        log = builder(**kwargs)
        instances.append(log)
        return log

    yield _factory

    for log in instances:
        close = getattr(log, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------------
# Basic shape: emit / read / seq / streams
# ---------------------------------------------------------------------------


def test_emit_assigns_monotonic_seq_starting_from_zero(make_log) -> None:
    log = make_log()
    e1 = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="stub"),
    )
    e2 = log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="lease-1"),
    )

    assert e1.seq == 0
    assert e2.seq == 1


def test_read_returns_appended_events_in_order(make_log) -> None:
    log = make_log()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
    )

    events = log.read("t1")
    assert [e.type for e in events] == ["TaskCreated", "TaskStarted"]


def test_read_after_seq_returns_only_later_events(make_log) -> None:
    log = make_log()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
    )
    log.emit(
        task_id="t1",
        type="TaskCompleted",
        payload=TaskCompletedPayload(answer="x"),
    )

    tail = log.read("t1", after_seq=0)
    assert [e.type for e in tail] == ["TaskStarted", "TaskCompleted"]


def test_streams_are_isolated_by_task_id(make_log) -> None:
    log = make_log()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="a", policy_name="p"),
    )
    log.emit(
        task_id="t2",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="b", policy_name="p"),
    )

    assert len(log.read("t1")) == 1
    assert len(log.read("t2")) == 1


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_find_latest_snapshot_returns_most_recent(make_log) -> None:
    log = make_log()
    ref1 = ContentRef(hash="a" * 64, size=10, media_type="application/json")
    ref2 = ContentRef(hash="b" * 64, size=12, media_type="application/json")

    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1",
        type="TaskSnapshot",
        payload=TaskSnapshotPayload(state_ref=ref1),
    )
    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
    )
    log.emit(
        task_id="t1",
        type="TaskSnapshot",
        payload=TaskSnapshotPayload(state_ref=ref2),
    )

    snap = log.find_latest_snapshot("t1")
    assert snap is not None
    assert snap.payload.state_ref == ref2


def test_find_latest_snapshot_returns_none_without_snapshot(make_log) -> None:
    log = make_log()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    assert log.find_latest_snapshot("t1") is None


# ---------------------------------------------------------------------------
# Layer 1: expected_seq optimistic concurrency
# ---------------------------------------------------------------------------


def test_expected_seq_match_appends(make_log) -> None:
    log = make_log()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        expected_seq=0,
    )
    ev = log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
        expected_seq=1,
    )
    assert ev.seq == 1


def test_expected_seq_too_low_raises(make_log) -> None:
    log = make_log()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        expected_seq=0,
    )
    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
        expected_seq=1,
    )

    with pytest.raises(StaleSequence):
        log.emit(
            task_id="t1",
            type="TaskCompleted",
            payload=TaskCompletedPayload(answer="x"),
            expected_seq=1,
        )


def test_expected_seq_too_high_raises(make_log) -> None:
    log = make_log()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        expected_seq=0,
    )
    with pytest.raises(StaleSequence):
        log.emit(
            task_id="t1",
            type="TaskStarted",
            payload=TaskStartedPayload(lease_id="L"),
            expected_seq=5,
        )


def test_stale_sequence_message_carries_task_and_seq(make_log) -> None:
    log = make_log()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    with pytest.raises(StaleSequence) as exc:
        log.emit(
            task_id="t1",
            type="TaskStarted",
            payload=TaskStartedPayload(lease_id="L"),
            expected_seq=5,
        )
    msg = str(exc.value)
    assert "t1" in msg
    assert "expected=5" in msg
    assert "actual=1" in msg


# ---------------------------------------------------------------------------
# Layer 2: lease validity (delegated to LeaseRegistry)
# ---------------------------------------------------------------------------


def _wire_log_to_dispatcher(make_log):
    disp = InMemoryDispatcher()
    log = make_log(lease_validator=disp)
    return log, disp


def test_valid_lease_succeeds(make_log) -> None:
    log, disp = _wire_log_to_dispatcher(make_log)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None
    ev = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        lease_id=lease.lease_id,
    )
    assert ev.seq == 0


def test_invalid_lease_raises(make_log) -> None:
    log, disp = _wire_log_to_dispatcher(make_log)
    disp.enqueue("t1")
    disp.lease(worker_id="w1")
    with pytest.raises(InvalidLease):
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            lease_id="lease-bogus",
        )


def test_released_lease_raises(make_log) -> None:
    log, disp = _wire_log_to_dispatcher(make_log)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None
    disp.release(lease.lease_id, next_state="terminal")
    with pytest.raises(InvalidLease):
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            lease_id=lease.lease_id,
        )


def test_expired_lease_raises(make_log) -> None:
    now = [0.0]
    disp = InMemoryDispatcher(now=lambda: now[0])
    log = make_log(lease_validator=disp)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1", lease_seconds=5.0)
    assert lease is not None
    now[0] = 100.0
    with pytest.raises(InvalidLease):
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            lease_id=lease.lease_id,
        )


def test_lease_for_different_task_raises(make_log) -> None:
    log, disp = _wire_log_to_dispatcher(make_log)
    disp.enqueue("t1")
    disp.enqueue("t2")
    lease_t1 = disp.lease(worker_id="w1")
    assert lease_t1 is not None
    with pytest.raises(InvalidLease):
        log.emit(
            task_id="t2",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            lease_id=lease_t1.lease_id,
        )


def test_no_validator_accepts_any_lease_id(make_log) -> None:
    log = make_log()
    ev = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        lease_id="lease-anything",
    )
    assert ev.seq == 0


def test_bind_lease_registry_after_construction(make_log) -> None:
    log = make_log()
    disp = InMemoryDispatcher()
    log.bind_lease_registry(disp)
    disp.enqueue("t1")
    disp.lease(worker_id="w1")
    with pytest.raises(InvalidLease):
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            lease_id="lease-bogus",
        )


# ---------------------------------------------------------------------------
# Layer 3: idempotency
# ---------------------------------------------------------------------------


def test_idempotent_emit_returns_cached_envelope(make_log) -> None:
    log, disp = _wire_log_to_dispatcher(make_log)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    first = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        lease_id=lease.lease_id,
        idempotency_key="op-1",
    )
    second = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        lease_id=lease.lease_id,
        idempotency_key="op-1",
    )

    assert first.seq == 0
    assert second.seq == 0
    assert second.id == first.id
    assert len(log.read("t1")) == 1


def test_idempotency_runs_before_expected_seq_check(make_log) -> None:
    log, disp = _wire_log_to_dispatcher(make_log)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    first = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        lease_id=lease.lease_id,
        idempotency_key="op-1",
        expected_seq=0,
    )
    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
        lease_id=lease.lease_id,
        expected_seq=1,
    )
    retry = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        lease_id=lease.lease_id,
        idempotency_key="op-1",
        expected_seq=0,
    )
    assert retry.seq == 0
    assert retry.id == first.id
    assert len(log.read("t1")) == 2


def test_idempotency_isolated_per_lease(make_log) -> None:
    log, disp = _wire_log_to_dispatcher(make_log)
    disp.enqueue("t1")
    lease_a = disp.lease(worker_id="wA")
    assert lease_a is not None
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        lease_id=lease_a.lease_id,
        idempotency_key="shared-key",
    )
    disp.release(lease_a.lease_id, next_state="suspended")
    disp.enqueue("t1")
    lease_b = disp.lease(worker_id="wB")
    assert lease_b is not None

    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
        lease_id=lease_b.lease_id,
        idempotency_key="shared-key",
    )
    assert [e.type for e in log.read("t1")] == ["TaskCreated", "TaskStarted"]


# ---------------------------------------------------------------------------
# system_emit
# ---------------------------------------------------------------------------


def test_system_emit_writes_without_lease_validation(make_log) -> None:
    log, disp = _wire_log_to_dispatcher(make_log)
    disp.enqueue("t-parent")
    disp.lease(worker_id="w-parent")

    ev = log.system_emit(
        task_id="t-parent",
        type="SubtaskCompleted",
        payload=SubtaskCompletedPayload(
            subtask_id="t-child",
            result=SubtaskResult(status="completed", output=None, error=None),
        ),
        actor="child_observer",
        origin="observer",
    )
    assert ev.seq == 0


def test_system_emit_rejects_concurrency_args(make_log) -> None:
    log = make_log()
    with pytest.raises(TypeError):
        log.system_emit(  # type: ignore[call-arg]
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            actor="engine",
            expected_seq=5,
        )


# ---------------------------------------------------------------------------
# Layer 0: 4-KB payload cap
# ---------------------------------------------------------------------------


def test_emit_rejects_payload_above_4kb(make_log) -> None:
    log = make_log()
    oversized_dict = {"blob": "x" * (MAX_PAYLOAD_BYTES + 1024)}
    with pytest.raises(PayloadTooLarge, match="large bodies must go through ContentStore"):
        log.emit(task_id="t1", type="UnknownLargeType", payload=oversized_dict)


def test_emit_accepts_payload_near_cap(make_log) -> None:
    log = make_log()
    fits = {"blob": "x" * (MAX_PAYLOAD_BYTES - 64)}
    ev = log.emit(task_id="t1", type="UnknownSmallType", payload=fits)
    assert ev.seq == 0


def test_system_emit_also_enforces_cap(make_log) -> None:
    log = make_log()
    oversized_dict = {"blob": "x" * (MAX_PAYLOAD_BYTES + 1024)}
    with pytest.raises(PayloadTooLarge):
        log.system_emit(
            task_id="t1",
            type="UnknownLargeType",
            payload=oversized_dict,
            actor="child_observer",
            origin="observer",
        )


# ---------------------------------------------------------------------------
# Subscribe semantics
# ---------------------------------------------------------------------------


def test_subscribe_invokes_callback_for_each_envelope(make_log) -> None:
    log = make_log()
    seen: list[str] = []
    log.subscribe(lambda ev: seen.append(ev.type))

    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
    )
    assert seen == ["TaskCreated", "TaskStarted"]


def test_subscriber_exception_does_not_break_writer(make_log) -> None:
    log = make_log()

    def boom(_: EventEnvelope) -> None:
        raise RuntimeError("observer crashed")

    log.subscribe(boom)
    ev = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    assert ev.seq == 0
    assert len(log.read("t1")) == 1


def test_subscriber_can_re_emit_inline_in_callback(make_log) -> None:
    """A subscriber must be free to open a new emit during its callback.

    The classic example is :class:`ChildLifecycleObserver`, which
    observes a child stream's terminal event and writes
    ``SubtaskCompleted`` to the parent stream as part of the same
    handoff. The adapter has to release its internal lock before
    invoking subscribers so that this works without deadlocking.
    """
    log = make_log()

    def on_child(ev: EventEnvelope) -> None:
        if ev.task_id == "t-child" and ev.type == "TaskCompleted":
            log.system_emit(
                task_id="t-parent",
                type="SubtaskCompleted",
                payload=SubtaskCompletedPayload(
                    subtask_id="t-child",
                    result=SubtaskResult(
                        status="completed", output=None, error=None
                    ),
                ),
                actor="child_observer",
                origin="observer",
            )

    log.subscribe(on_child)
    log.emit(
        task_id="t-parent",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="parent goal", policy_name="p"),
    )
    log.emit(
        task_id="t-child",
        type="TaskCompleted",
        payload=TaskCompletedPayload(answer="done"),
    )

    parent = log.read("t-parent")
    assert [e.type for e in parent] == ["TaskCreated", "SubtaskCompleted"]
    tail = parent[-1]
    assert tail.payload.subtask_id == "t-child"
    assert tail.origin == "observer"


def test_unsubscribe_stops_callback(make_log) -> None:
    log = make_log()
    seen: list[str] = []
    unsub = log.subscribe(lambda ev: seen.append(ev.type))
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    unsub()
    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
    )
    assert seen == ["TaskCreated"]


# ---------------------------------------------------------------------------
# Typed payload restore
# ---------------------------------------------------------------------------


# (type string, payload instance) — one entry per registered payload type
# we explicitly want to see round-trip cleanly via ``log.read``.
_TYPED_PAYLOAD_SAMPLES: tuple[tuple[str, Any], ...] = (
    ("TaskCreated", TaskCreatedPayload(goal="g", policy_name="p")),
    ("TaskStarted", TaskStartedPayload(lease_id="L1")),
    ("TaskStatePatched", TaskStatePatchedPayload(patch={"phase": "running"})),
    (
        "MessagesAppended",
        MessagesAppendedPayload(
            messages_ref=ContentRef(
                hash="m" * 64, size=33, media_type="application/json"
            ),
            count=2,
        ),
    ),
    (
        "TaskSnapshot",
        TaskSnapshotPayload(
            state_ref=ContentRef(
                hash="s" * 64, size=99, media_type="application/json"
            )
        ),
    ),
    (
        "ContextPlanComposed",
        ContextPlanComposedPayload(
            plan_ref=ContentRef(
                hash="c" * 64, size=10, media_type="application/json"
            )
        ),
    ),
    ("TaskCompleted", TaskCompletedPayload(answer="42")),
    ("TaskFailed", TaskFailedPayload(reason="boom", retryable=True)),
    (
        "ToolCallStarted",
        ToolCallStartedPayload(call_id="c1", tool_name="echo", arguments={"x": 1}),
    ),
    (
        "ToolResultRecorded",
        ToolResultRecordedPayload(
            call_id="c1",
            success=True,
            output_ref=ContentRef(
                hash="o" * 64, size=2, media_type="text/plain"
            ),
            summary="ok",
        ),
    ),
    ("ToolCallFinished", ToolCallFinishedPayload(call_id="c1")),
    (
        "SubtaskSpawned",
        SubtaskSpawnedPayload(subtask_id="t-c", agent_name="a", goal="g"),
    ),
    (
        "SubtaskCompleted",
        SubtaskCompletedPayload(
            subtask_id="t-c",
            result=SubtaskResult(
                status="completed", output=None, error=None
            ),
        ),
    ),
    (
        "SubtaskDenied",
        SubtaskDeniedPayload(agent_name="a", goal="g", reason="policy"),
    ),
    (
        "TaskSuspended",
        TaskSuspendedPayload(
            reason="waiting_human",
            wake_on=HumanResponseReceived(handle="r1"),
        ),
    ),
    (
        "TaskWoken",
        TaskWokenPayload(wake_event=SubtaskCompleted(subtask_id="t-c")),
    ),
    (
        "ToolCallDenied",
        ToolCallDeniedPayload(call_id="c1", tool_name="echo", reason="risk"),
    ),
    (
        "LLMRequestStarted",
        LLMRequestStartedPayload(
            call_id="L1",
            model="m",
            request_ref=ContentRef(
                hash="r" * 64, size=1, media_type="application/json"
            ),
        ),
    ),
    (
        "LLMResponseRecorded",
        LLMResponseRecordedPayload(
            call_id="L1",
            response_ref=ContentRef(
                hash="r" * 64, size=1, media_type="application/json"
            ),
            stop_reason="end_turn",
        ),
    ),
    ("LLMRequestFinished", LLMRequestFinishedPayload(call_id="L1", success=True)),
    ("TaskCancelled", TaskCancelledPayload(reason="abort", cascade=True)),
    (
        "LeaseGranted",
        LeaseGrantedPayload(lease_id="L1", worker_id="w1", expires_at=0.0),
    ),
)


@pytest.mark.parametrize("event_type,payload", _TYPED_PAYLOAD_SAMPLES)
def test_typed_payload_round_trips(make_log, event_type, payload) -> None:
    log = make_log()
    written = log.emit(task_id="t1", type=event_type, payload=payload)
    [readback] = log.read("t1")
    assert readback.payload == payload
    assert type(readback.payload) is type(payload)
    assert readback.id == written.id
    assert readback.seq == 0


def test_unknown_event_type_payload_preserved_as_dict(make_log) -> None:
    """Forward-compatibility: an event type the adapter doesn't yet
    recognise reads back with a plain dict payload, not a crash."""
    log = make_log()
    log.emit(
        task_id="t1",
        type="NewlyMintedTypeThatDoesNotExistYet",
        payload={"hello": "world", "n": 7},
    )
    [readback] = log.read("t1")
    # InMemory keeps the original object; Sqlite reads back the
    # canonical dict. Both surfaces are "dict-shaped, attribute-free".
    assert readback.payload == {"hello": "world", "n": 7}


# ---------------------------------------------------------------------------
# Payload restorer coverage guard
# ---------------------------------------------------------------------------


def test_payload_restorer_covers_all_known_payload_types() -> None:
    """Every ``*Payload`` class in :mod:`noeta.protocols.events` must
    have a corresponding entry in ``_PAYLOAD_RESTORERS``.

    Without this guard, adding a new event type to ``events.py``
    without registering the restorer would silently fall through to
    the forward-compat dict path and break fold-side attribute access
    on Sqlite at runtime. We catch that drift at test time instead.
    """
    import noeta.protocols.events as events_module

    payload_classes = [
        cls
        for name, cls in inspect.getmembers(events_module, inspect.isclass)
        if name.endswith("Payload") and cls.__module__ == events_module.__name__
    ]
    expected_types = {
        cls.__name__.removesuffix("Payload") for cls in payload_classes
    }
    actual_types = set(_PAYLOAD_RESTORERS.keys())

    missing = expected_types - actual_types
    assert not missing, (
        "_PAYLOAD_RESTORERS missing entries for: "
        f"{sorted(missing)}. Add them with "
        "`lambda d: <ClsName>Payload(**d)`."
    )


# ---------------------------------------------------------------------------
# CW5a — EventLogTaskIndex catalog capability (list_task_streams)
# ---------------------------------------------------------------------------


def test_list_task_streams_skips_empty_streams(make_log: Any) -> None:
    """Only tasks with ≥1 event appear. The InMemory adapter's ``_streams`` is a
    defaultdict, so a prior ``read()`` on an unknown task_id materialises an
    empty stream — it must NOT show up as a session."""
    log = make_log()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.read("ghost")  # materialises an empty stream in the InMemory defaultdict

    summaries = log.list_task_streams()
    assert [s.task_id for s in summaries] == ["t1"]


def test_list_task_streams_orders_by_recency_then_task_id(make_log: Any) -> None:
    """Most-recent ``last_event_time`` first; a deterministic ``task_id`` ASC
    tie-break so equal timestamps never reorder flakily — and the tie-break is
    NOT just insertion order (we insert ``tc`` before ``tb`` at the same time)."""
    times = iter([20.0, 20.0, 10.0])
    log = make_log(clock=lambda: next(times))
    log.emit(  # tc @ t=20 (inserted first among the ties)
        task_id="tc",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(  # tb @ t=20
        task_id="tb",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(  # ta @ t=10
        task_id="ta",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )

    summaries = log.list_task_streams()
    # recency desc → the two t=20 first; tie-break task_id ASC → tb before tc.
    assert [s.task_id for s in summaries] == ["tb", "tc", "ta"]


def test_list_task_streams_summary_matches_stream_tail(make_log: Any) -> None:
    """``last_seq`` / ``last_event_time`` equal the task's final event."""
    times = iter([5.0, 7.0])
    log = make_log(clock=lambda: next(times))
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
    )

    [summary] = log.list_task_streams()
    assert summary.task_id == "t1"
    assert summary.last_seq == 1  # second event (seq starts at 0 per task)
    assert summary.last_event_time == 7.0
