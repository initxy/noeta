"""InMemoryEventLog three-layer concurrency protection (issue 06).

Augments ``test_event_log.py`` (basic shape) with the strict checks:

1. ``expected_seq`` mismatch raises :class:`StaleSequence`.
2. Invalid / expired ``lease_id`` raises :class:`InvalidLease` whenever
   a ``lease_validator`` is bound.
3. Same ``(lease_id, idempotency_key)`` returns the cached seq without
   writing a second event.

Plus ``system_emit``: skips lease validation for the documented
cross-stream Engine observer path (child completion → parent stream).
"""

from __future__ import annotations

import pytest

from noeta.protocols.errors import InvalidLease, PayloadTooLarge, StaleSequence
from noeta.protocols.events import TaskCreatedPayload
from noeta.storage.memory import (
    MAX_PAYLOAD_BYTES,
    InMemoryDispatcher,
    InMemoryEventLog,
)


# ---------------------------------------------------------------------------
# Layer 1: expected_seq optimistic concurrency
# ---------------------------------------------------------------------------


def test_expected_seq_match_appends_normally() -> None:
    log = InMemoryEventLog()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        expected_seq=0,
    )
    ev = log.emit(task_id="t1", type="TaskStarted", payload={}, expected_seq=1)
    assert ev.seq == 1


def test_expected_seq_too_low_raises_stale_sequence() -> None:
    log = InMemoryEventLog()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        expected_seq=0,
    )
    log.emit(task_id="t1", type="TaskStarted", payload={}, expected_seq=1)

    # Caller thinks the stream still has 1 event; in fact it has 2.
    with pytest.raises(StaleSequence):
        log.emit(
            task_id="t1",
            type="TaskCompleted",
            payload={"answer": "x"},
            expected_seq=1,
        )


def test_expected_seq_too_high_raises_stale_sequence() -> None:
    log = InMemoryEventLog()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        expected_seq=0,
    )

    with pytest.raises(StaleSequence):
        log.emit(task_id="t1", type="TaskStarted", payload={}, expected_seq=5)


def test_stale_sequence_error_message_carries_task_and_seq() -> None:
    log = InMemoryEventLog()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )

    with pytest.raises(StaleSequence) as exc:
        log.emit(task_id="t1", type="TaskStarted", payload={}, expected_seq=5)
    msg = str(exc.value)
    assert "t1" in msg
    assert "expected=5" in msg
    assert "actual=1" in msg


# ---------------------------------------------------------------------------
# Layer 2: lease_id validity
# ---------------------------------------------------------------------------


def _wire_log_to_dispatcher() -> tuple[InMemoryEventLog, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    return log, disp


def test_emit_with_valid_lease_succeeds_when_validator_bound() -> None:
    log, disp = _wire_log_to_dispatcher()
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


def test_emit_with_invalid_lease_id_raises_invalid_lease() -> None:
    log, disp = _wire_log_to_dispatcher()
    disp.enqueue("t1")
    disp.lease(worker_id="w1")

    with pytest.raises(InvalidLease):
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            lease_id="lease-bogus",
        )


def test_emit_with_released_lease_raises_invalid_lease() -> None:
    log, disp = _wire_log_to_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None
    disp.release(lease.lease_id, next_state="terminal")

    # The same lease_id can no longer write — releasing closes the door.
    with pytest.raises(InvalidLease):
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            lease_id=lease.lease_id,
        )


def test_emit_with_expired_lease_raises_invalid_lease() -> None:
    now = [0.0]
    disp = InMemoryDispatcher(now=lambda: now[0])
    log = InMemoryEventLog(lease_validator=disp)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1", lease_seconds=5.0)
    assert lease is not None

    now[0] = 100.0  # past expiry
    with pytest.raises(InvalidLease):
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            lease_id=lease.lease_id,
        )


def test_emit_with_lease_for_different_task_raises_invalid_lease() -> None:
    log, disp = _wire_log_to_dispatcher()
    disp.enqueue("t1")
    disp.enqueue("t2")
    lease_t1 = disp.lease(worker_id="w1")
    assert lease_t1 is not None

    # Writer holds t1's lease but tries to write t2's stream.
    with pytest.raises(InvalidLease):
        log.emit(
            task_id="t2",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            lease_id=lease_t1.lease_id,
        )


def test_emit_without_validator_accepts_any_lease_id() -> None:
    # Pure EventLog unit tests don't wire a dispatcher; the validator
    # defaults to None and lease_id is accepted opaquely.
    log = InMemoryEventLog()
    ev = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
        lease_id="lease-anything",
    )
    assert ev.seq == 0


def test_bind_lease_registry_after_construction_enforces_writes() -> None:
    log = InMemoryEventLog()
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
# Layer 3: idempotency dedup
# ---------------------------------------------------------------------------


def test_idempotent_emit_returns_cached_seq_without_duplicating() -> None:
    log, disp = _wire_log_to_dispatcher()
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
    assert second.seq == 0  # cached, no new write
    # Stream still has exactly one event.
    assert len(log.read("t1")) == 1
    # The cached envelope returned is the original (not the retried one).
    assert second.id == first.id


def test_idempotency_dedup_runs_before_expected_seq_check() -> None:
    """Retried write with a stale expected_seq must still be deduped.

    Without dedup-before-seq-check, a worker that retried an emit
    after the original succeeded would observe StaleSequence and
    panic — which is the bug idempotency is meant to prevent.
    """
    log, disp = _wire_log_to_dispatcher()
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
    # A later legitimate write moves the stream forward.
    log.emit(
        task_id="t1",
        type="TaskStarted",
        payload={},
        lease_id=lease.lease_id,
        expected_seq=1,
    )
    # The retry of op-1 carries the same expected_seq=0 it originally
    # had; it must hit the idempotency cache, not StaleSequence.
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


def test_idempotency_isolated_per_lease() -> None:
    """A different lease using the same key is treated as a new write.

    Idempotency is per-(lease_id, key); two distinct lease holders
    coincidentally using the same key string each get their own emit.
    """
    log, disp = _wire_log_to_dispatcher()
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
        payload={},
        lease_id=lease_b.lease_id,
        idempotency_key="shared-key",  # same key, different lease
    )

    # Both writes landed; idempotency scope is per-lease.
    assert [e.type for e in log.read("t1")] == ["TaskCreated", "TaskStarted"]


# ---------------------------------------------------------------------------
# system_emit: cross-stream system writer (replaces the
# legacy ``bypass_lease=True`` flag with a separate method)
# ---------------------------------------------------------------------------


def test_system_emit_writes_without_lease_validation() -> None:
    log, disp = _wire_log_to_dispatcher()
    disp.enqueue("t-parent")
    # Parent has no active lease (suspended). System writer (the
    # child-completion observer) needs to write to it anyway.
    disp.lease(worker_id="w-parent")  # consume + suspend

    ev = log.system_emit(
        task_id="t-parent",
        type="SubtaskCompleted",
        payload={"subtask_id": "t-child"},
        actor="child_observer",
        origin="observer",
    )
    assert ev.seq == 0


def test_system_emit_does_not_accept_concurrency_args() -> None:
    """system_emit deliberately drops the three concurrency layers.

    The signature does not take ``lease_id`` / ``expected_seq`` /
    ``idempotency_key`` — system writes are orchestrator-driven and
    the caller is responsible for ordering. This test pins the
    surface so a future regression that smuggles them back fails
    loudly.
    """
    log, _disp = _wire_log_to_dispatcher()

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


def test_emit_rejects_payload_above_4kb() -> None:
    log = InMemoryEventLog()
    # A 5-KB blob trips the cap. Real callers should put bodies this
    # size in ContentStore and reference them via a ContentRef.
    oversized = {"blob": "x" * (MAX_PAYLOAD_BYTES + 1024)}
    with pytest.raises(PayloadTooLarge, match="large bodies must go through ContentStore"):
        log.emit(task_id="t1", type="MessagesAppended", payload=oversized)


def test_emit_accepts_payload_at_the_cap() -> None:
    log = InMemoryEventLog()
    # ``MAX_PAYLOAD_BYTES`` exactly should be accepted; one byte over,
    # rejected. We approximate "at the cap" with a payload whose JSON
    # encoding lands a few bytes under, then assert it lands.
    fits = {"blob": "x" * (MAX_PAYLOAD_BYTES - 64)}
    ev = log.emit(task_id="t1", type="MessagesAppended", payload=fits)
    assert ev.seq == 0


def test_system_emit_also_enforces_4kb_cap() -> None:
    log = InMemoryEventLog()
    oversized = {"blob": "x" * (MAX_PAYLOAD_BYTES + 1024)}
    with pytest.raises(PayloadTooLarge):
        log.system_emit(
            task_id="t1",
            type="SubtaskCompleted",
            payload=oversized,
            actor="child_observer",
            origin="observer",
        )
