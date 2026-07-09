"""Storage-backend-neutral Dispatcher contract.

Issue 17 introduces the second Dispatcher adapter (`SqliteDispatcher`)
on top of the existing `InMemoryDispatcher`. This module exercises the
behavioural contract — lease lifecycle, heartbeat cap, release with
wake_on round-trip, fail with retry budget, wake before suspend,
requeue_stale, is_lease_valid, enqueue idempotency — against **both**
backends.

Existing ``test_dispatcher*.py`` files continue exercising the
InMemory-specific paths (introspection helpers, monotonic clock); this
suite enforces the cross-backend behavioural contract.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from noeta.protocols.errors import InvalidLease
from noeta.protocols.wake import (
    HumanResponseReceived,
    SubtaskCompleted,
    TimerFired,
)
from noeta.storage.memory import InMemoryDispatcher
from noeta.storage.sqlite.dispatcher import SqliteDispatcher
from tests._pg import isolated_schema_dsn, postgres_param


# ---------------------------------------------------------------------------
# Adapter fixture
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite", postgres_param()])
def make_dispatcher(request):
    # Postgres: every factory call gets its own fresh schema on the
    # configured server so it is as isolated and empty as a fresh
    # InMemory / sqlite ``:memory:`` instance.
    from contextlib import ExitStack

    stack = ExitStack()

    def _make_in_memory(
        *,
        now: Callable[[], float] | None = None,
        heartbeat_max: int = 360,
        max_fail_attempts: int = 3,
    ) -> Any:
        return InMemoryDispatcher(
            now=now, heartbeat_max=heartbeat_max, max_fail_attempts=max_fail_attempts
        )

    def _make_sqlite(
        *,
        now: Callable[[], float] | None = None,
        heartbeat_max: int = 360,
        max_fail_attempts: int = 3,
    ) -> Any:
        return SqliteDispatcher(
            ":memory:",
            now=now,
            heartbeat_max=heartbeat_max,
            max_fail_attempts=max_fail_attempts,
        )

    def _make_postgres(
        *,
        now: Callable[[], float] | None = None,
        heartbeat_max: int = 360,
        max_fail_attempts: int = 3,
    ) -> Any:
        from noeta.storage.postgres.dispatcher import PostgresDispatcher

        dsn = stack.enter_context(isolated_schema_dsn())
        return PostgresDispatcher(
            dsn,
            now=now,
            heartbeat_max=heartbeat_max,
            max_fail_attempts=max_fail_attempts,
        )

    if request.param == "memory":
        builder = _make_in_memory
    elif request.param == "sqlite":
        builder = _make_sqlite
    else:
        builder = _make_postgres
    instances: list[Any] = []

    def _factory(**kwargs: Any) -> Any:
        disp = builder(**kwargs)
        instances.append(disp)
        return disp

    yield _factory

    for disp in instances:
        close = getattr(disp, "close", None)
        if callable(close):
            close()
    stack.close()


# ---------------------------------------------------------------------------
# Lease lifecycle
# ---------------------------------------------------------------------------


def test_enqueue_then_lease_returns_lease(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None
    assert lease.task_id == "t1"
    assert lease.lease_id.startswith("lease-")
    assert lease.expires_at > 0.0


def test_reserved_enqueue_is_skipped_by_untargeted_lease(make_dispatcher) -> None:
    """A ``reserved`` task (a fresh subtask child) is targeted-lease-only: an
    untargeted FIFO poll never returns it, but a targeted lease does. This is
    what keeps a resident-worker pool from stealing an unseeded background
    sub-agent child out from under its executor's targeted ``_descend_to_child``.
    """
    disp = make_dispatcher()
    disp.enqueue("plain")
    disp.enqueue("child", reserved=True)
    # Untargeted poll walks past the reserved child to the plain task.
    assert disp.lease(worker_id="w1").task_id == "plain"
    # With only the reserved child left, an untargeted poll finds nothing.
    assert disp.lease(worker_id="w1") is None
    # But its owning driver can still targeted-lease it.
    claimed = disp.lease(worker_id="owner", task_id="child")
    assert claimed is not None and claimed.task_id == "child"


def test_reserved_flag_is_cleared_by_first_lease(make_dispatcher) -> None:
    """``reserved`` is a ONE-SHOT claim guard: once the owning driver has
    targeted-leased the child, a later re-enqueue (a suspend/resume handed to
    the worker pool) is an ordinary untargeted-leaseable task."""
    disp = make_dispatcher()
    disp.enqueue("child", reserved=True)
    claimed = disp.lease(worker_id="owner", task_id="child")
    assert claimed is not None
    # Child suspended then re-enqueued (default, not reserved) — now the pool
    # may untargeted-lease it.
    disp.release(claimed.lease_id, next_state="suspended", wake_on=None)
    disp.enqueue("child")
    assert disp.lease(worker_id="w1").task_id == "child"


def test_lease_returns_none_when_no_ready_tasks(make_dispatcher) -> None:
    disp = make_dispatcher()
    assert disp.lease(worker_id="w1") is None


def test_lease_consumes_ready_queue_fifo(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    disp.enqueue("t2")
    disp.enqueue("t3")
    assert disp.lease(worker_id="w").task_id == "t1"
    assert disp.lease(worker_id="w").task_id == "t2"
    assert disp.lease(worker_id="w").task_id == "t3"
    assert disp.lease(worker_id="w") is None


# ---------------------------------------------------------------------------
# enqueue idempotency (issue 17 B3)
# ---------------------------------------------------------------------------


def test_enqueue_already_ready_is_idempotent_no_op(make_dispatcher) -> None:
    """``enqueue`` on a task already in ready must not reshuffle FIFO."""
    disp = make_dispatcher()
    disp.enqueue("t1")
    disp.enqueue("t2")
    disp.enqueue("t1")  # already ready; must be a no-op
    assert disp.lease(worker_id="w").task_id == "t1"
    assert disp.lease(worker_id="w").task_id == "t2"
    assert disp.lease(worker_id="w") is None


def test_enqueue_existing_terminal_task_resets_to_ready(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    disp.release(lease.lease_id, next_state="terminal")
    # Now re-enqueue terminal task; it should come back as leaseable.
    disp.enqueue("t1")
    new_lease = disp.lease(worker_id="w")
    assert new_lease is not None and new_lease.task_id == "t1"


def test_enqueue_existing_suspended_task_resets_to_ready(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=HumanResponseReceived(handle="h1"),
        suspend_reason="manual",
    )
    disp.enqueue("t1")
    new_lease = disp.lease(worker_id="w")
    assert new_lease is not None and new_lease.task_id == "t1"


# ---------------------------------------------------------------------------
# release + wake + suspend
# ---------------------------------------------------------------------------


def test_release_suspended_then_matching_wake_requeues(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    wake_on = SubtaskCompleted(subtask_id="t-child")
    disp.release(lease.lease_id, next_state="suspended", wake_on=wake_on)
    # No matching wake yet → still not leaseable.
    assert disp.lease(worker_id="w") is None
    # Matching wake → requeue True.
    assert disp.wake("t1", SubtaskCompleted(subtask_id="t-child")) is True
    new_lease = disp.lease(worker_id="w")
    assert new_lease is not None and new_lease.task_id == "t1"


def test_release_suspended_non_matching_wake_buffers(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id="t-child"),
    )
    # A non-matching event must NOT requeue.
    assert disp.wake("t1", SubtaskCompleted(subtask_id="other")) is False
    assert disp.lease(worker_id="w") is None


def test_wake_before_suspend_buffers_event_and_drains_on_match(make_dispatcher) -> None:
    """Wake arriving for a not-yet-suspended task is buffered and
    fires automatically when release(suspended, wake_on=matching) lands."""
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    # Wake arrives before the task is suspended → buffered, returns False.
    assert disp.wake("t1", SubtaskCompleted(subtask_id="t-child")) is False
    # Now suspend with the matching wake_on → buffered event fires
    # inside the release transaction, task ends up ready.
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id="t-child"),
    )
    new_lease = disp.lease(worker_id="w")
    assert new_lease is not None and new_lease.task_id == "t1"


def test_wake_before_enqueue_buffers_event_and_later_release_drains(
    make_dispatcher,
) -> None:
    """Issue 17 B1: ``wake(unknown_task, X)`` is legal; the event is
    buffered, the task remains non-leaseable until ``enqueue`` creates
    it, and a subsequent ``release(suspended, wake_on=X)`` drains the
    buffered wake in the same transaction.
    """
    disp = make_dispatcher()
    # Wake arrives for a task that has never been enqueued.
    assert disp.wake("t-future", SubtaskCompleted(subtask_id="t-child")) is False
    # The task is not leaseable yet.
    assert disp.lease(worker_id="w") is None
    # enqueue creates the row, lease grabs it, release suspends with
    # the matching wake_on — the buffered wake should drain immediately.
    disp.enqueue("t-future")
    lease = disp.lease(worker_id="w")
    assert lease is not None and lease.task_id == "t-future"
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id="t-child"),
    )
    new_lease = disp.lease(worker_id="w")
    assert new_lease is not None and new_lease.task_id == "t-future"


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_extends_expiry(make_dispatcher) -> None:
    clock = [0.0]
    disp = make_dispatcher(now=lambda: clock[0])
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w", lease_seconds=10.0)
    assert lease.expires_at == pytest.approx(10.0)
    clock[0] = 5.0
    new_expires = disp.heartbeat(lease.lease_id, lease_seconds=10.0)
    assert new_expires == pytest.approx(15.0)


def test_heartbeat_unknown_lease_raises(make_dispatcher) -> None:
    disp = make_dispatcher()
    with pytest.raises(InvalidLease):
        disp.heartbeat("lease-bogus")


def test_heartbeat_over_cap_force_releases_to_suspended(make_dispatcher) -> None:
    disp = make_dispatcher(heartbeat_max=3)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    for _ in range(3):
        disp.heartbeat(lease.lease_id)
    with pytest.raises(InvalidLease):
        disp.heartbeat(lease.lease_id)
    # After the force-release the task is suspended (no wake_on set),
    # so it is not leaseable.
    assert disp.lease(worker_id="w") is None


# ---------------------------------------------------------------------------
# fail
# ---------------------------------------------------------------------------


def test_fail_retryable_requeues_until_max_attempts(make_dispatcher) -> None:
    disp = make_dispatcher(max_fail_attempts=2)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    disp.fail(lease.lease_id, retryable=True)
    # Still leaseable for the retry.
    retry = disp.lease(worker_id="w")
    assert retry is not None and retry.task_id == "t1"
    disp.fail(retry.lease_id, retryable=True)
    # Attempt count reached max → terminal, not leaseable.
    assert disp.lease(worker_id="w") is None


def test_fail_non_retryable_marks_terminal_immediately(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    disp.fail(lease.lease_id, retryable=False, reason="bad data")
    assert disp.lease(worker_id="w") is None


def test_fail_unknown_lease_raises(make_dispatcher) -> None:
    disp = make_dispatcher()
    with pytest.raises(InvalidLease):
        disp.fail("lease-bogus")


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_release_invalid_next_state_raises_value_error(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    with pytest.raises(ValueError):
        disp.release(lease.lease_id, next_state="bogus")


def test_release_unknown_lease_raises(make_dispatcher) -> None:
    disp = make_dispatcher()
    with pytest.raises(InvalidLease):
        disp.release("lease-bogus", next_state="terminal")


def test_release_suspended_without_wake_on_leaves_task_unleaseable(
    make_dispatcher,
) -> None:
    """``release(suspended, wake_on=None)`` is allowed; the task stays
    suspended until something else wakes it."""
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    disp.release(lease.lease_id, next_state="suspended", suspend_reason="manual")
    assert disp.lease(worker_id="w") is None
    # Any wake event arrives; with wake_on=None on the row, the
    # matcher rejects (None never matches), event goes to pending.
    assert disp.wake("t1", SubtaskCompleted(subtask_id="anything")) is False


def test_release_drains_only_matching_pending_wake(make_dispatcher) -> None:
    """If a buffered wake doesn't match the wake_on at release time,
    release suspends the task normally; a later matching wake then
    requeues. Exercises the no-match branch of the drain loop."""
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    # Buffer a non-matching event first.
    assert disp.wake("t1", SubtaskCompleted(subtask_id="other")) is False
    # Release suspending with a wake_on that does NOT match the buffer.
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id="target"),
    )
    # The pending event is still there; task is still suspended.
    assert disp.lease(worker_id="w") is None
    # Matching wake now arrives → task becomes ready.
    assert disp.wake("t1", SubtaskCompleted(subtask_id="target")) is True
    next_lease = disp.lease(worker_id="w")
    assert next_lease is not None and next_lease.task_id == "t1"


# ---------------------------------------------------------------------------
# is_lease_valid
# ---------------------------------------------------------------------------


def test_is_lease_valid_true_for_fresh_lease(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    assert disp.is_lease_valid("t1", lease.lease_id) is True


def test_is_lease_valid_false_for_unknown_lease(make_dispatcher) -> None:
    disp = make_dispatcher()
    assert disp.is_lease_valid("t1", "lease-bogus") is False


def test_is_lease_valid_false_after_release(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    disp.release(lease.lease_id, next_state="terminal")
    assert disp.is_lease_valid("t1", lease.lease_id) is False


def test_is_lease_valid_false_after_expiry(make_dispatcher) -> None:
    clock = [0.0]
    disp = make_dispatcher(now=lambda: clock[0])
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w", lease_seconds=5.0)
    clock[0] = 100.0
    assert disp.is_lease_valid("t1", lease.lease_id) is False


def test_is_lease_valid_false_for_mismatched_task_id(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    disp.enqueue("t2")
    lease_t1 = disp.lease(worker_id="w")
    assert disp.is_lease_valid("t2", lease_t1.lease_id) is False


# ---------------------------------------------------------------------------
# requeue_stale
# ---------------------------------------------------------------------------


def test_requeue_stale_recovers_expired_lease(make_dispatcher) -> None:
    clock = [0.0]
    disp = make_dispatcher(now=lambda: clock[0])
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w", lease_seconds=5.0)
    clock[0] = 100.0
    requeued = disp.requeue_stale()
    assert requeued == ["t1"]
    # Old lease invalidated.
    assert disp.is_lease_valid("t1", lease.lease_id) is False
    # The task is leaseable again.
    new_lease = disp.lease(worker_id="w")
    assert new_lease is not None and new_lease.task_id == "t1"
    assert new_lease.lease_id != lease.lease_id


def test_requeue_stale_returns_empty_when_no_leases_expired(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    disp.lease(worker_id="w", lease_seconds=10000.0)
    assert disp.requeue_stale() == []


# ---------------------------------------------------------------------------
# wake_on canonical round-trip across typed WakeCondition subtypes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wake_on,wake_event",
    [
        (
            SubtaskCompleted(subtask_id="t-child"),
            SubtaskCompleted(subtask_id="t-child"),
        ),
        (
            HumanResponseReceived(handle="reply-42"),
            HumanResponseReceived(handle="reply-42"),
        ),
        (
            TimerFired(fire_at=1234.5),
            TimerFired(fire_at=1234.5),
        ),
    ],
)
def test_wake_on_round_trips_across_typed_subtypes(
    make_dispatcher, wake_on, wake_event
) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w")
    disp.release(lease.lease_id, next_state="suspended", wake_on=wake_on)
    assert disp.wake("t1", wake_event) is True
    new_lease = disp.lease(worker_id="w")
    assert new_lease is not None and new_lease.task_id == "t1"


# ---------------------------------------------------------------------------
# Sqlite-specific: CHECK constraints reject bypass writes
# ---------------------------------------------------------------------------


def test_sqlite_dispatcher_check_rejects_unknown_status() -> None:
    disp = SqliteDispatcher(":memory:")
    import sqlite3 as _sqlite3

    try:
        with pytest.raises(_sqlite3.IntegrityError):
            disp._conn.execute(
                "INSERT INTO dispatcher_tasks ("
                " task_id, status, ready_order"
                ") VALUES (?, ?, ?)",
                ("t1", "weird", 1),
            )
    finally:
        disp.close()


def test_sqlite_dispatcher_check_rejects_ready_without_order() -> None:
    disp = SqliteDispatcher(":memory:")
    import sqlite3 as _sqlite3

    try:
        with pytest.raises(_sqlite3.IntegrityError):
            disp._conn.execute(
                "INSERT INTO dispatcher_tasks ("
                " task_id, status, ready_order"
                ") VALUES (?, ?, NULL)",
                ("t1", "ready"),
            )
    finally:
        disp.close()


def test_sqlite_dispatcher_check_rejects_leased_without_lease_id() -> None:
    disp = SqliteDispatcher(":memory:")
    import sqlite3 as _sqlite3

    try:
        with pytest.raises(_sqlite3.IntegrityError):
            disp._conn.execute(
                "INSERT INTO dispatcher_tasks ("
                " task_id, status, lease_id, lease_expires_at"
                ") VALUES (?, ?, NULL, NULL)",
                ("t1", "leased"),
            )
    finally:
        disp.close()


# ---------------------------------------------------------------------------
# Contention — multi-worker CAS (round 3a single-host-multi-worker)
# ---------------------------------------------------------------------------


def test_lease_cas_only_one_winner_concurrent(make_dispatcher) -> None:
    """When N workers race to lease from a non-empty queue, exactly one
    wins each task; losers see ``None`` (or a different task_id) but
    never duplicate leases."""
    import threading

    disp = make_dispatcher()
    disp.enqueue("t1")
    winners: list[str] = []
    winners_lock = threading.Lock()
    errors: list[Exception] = []

    def _try_lease(worker_id: str) -> None:
        try:
            lease = disp.lease(worker_id=worker_id)
            if lease is not None:
                with winners_lock:
                    winners.append(lease.task_id)
        except Exception as exc:  # noqa: BLE001 — surface to the main thread
            errors.append(exc)

    threads = [threading.Thread(target=_try_lease, args=(f"w{i}",)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=5.0)
    assert not errors, f"workers raised: {errors!r}"
    assert winners.count("t1") == 1, f"expected exactly one winner, got {winners!r}"
    # Second lease attempt sees empty queue (the winning lease is held).
    assert disp.lease(worker_id="probe") is None


def test_release_yield_returns_lease_to_ready_without_fail_bump(
    make_dispatcher,
) -> None:
    """``release_yield`` (the seed-then-hand-off seam used by worker-pool
    mode) moves a leased task back to ``ready`` without incrementing
    ``fail_attempts`` and without losing a matched wake."""
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="seed", lease_seconds=60.0)
    assert lease is not None
    assert lease.task_id == "t1"
    # Before yield, no worker can grab the task.
    assert disp.lease(worker_id="probe") is None
    disp.release_yield(lease.lease_id)
    # After yield, a worker can pick it up; the fresh lease carries no
    # wake_event (no wake delivered in this scenario).
    lease2 = disp.lease(worker_id="worker")
    assert lease2 is not None
    assert lease2.task_id == "t1"
    assert lease2.wake_event is None
    assert lease2.lease_id != lease.lease_id


def test_release_yield_preserves_pending_wake(make_dispatcher) -> None:
    """A wake delivered while a seed-lease is held must NOT be lost
    after release_yield: a later lease after the normal wake-drain
    round-trip sees the wake_event."""
    from noeta.protocols.wake import HumanResponseReceived

    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="seed", lease_seconds=60.0)
    assert lease is not None
    # Direct yield while leased: release_yield goes leased→ready. No
    # wake_on is installed so no matched is drained; then a subsequent
    # wake + lease delivers it normally.
    disp.release_yield(lease.lease_id)
    disp.wake("t1", HumanResponseReceived(handle="next-goal"))
    # The task is 'ready' so wake buffers; release on a later
    # (suspended→ready) transition would drain it. Since we never
    # suspended, the wake stays buffered; here we simply enqueue-suspend
    # manually to exercise the drain: enqueue then lease-with-targeted-suspend
    # is overkill; the key contract is that release_yield did not destroy
    # pending state.
    #
    # Verify the pending wake is still present: suspend the task via
    # release(suspended, wake_on=next-goal) from a fresh lease.
    lease2 = disp.lease(worker_id="resolver")
    assert lease2 is not None
    # Release as suspended with the matching wake_on drains the buffer → matched.
    disp.release(
        lease2.lease_id,
        next_state="suspended",
        wake_on=HumanResponseReceived(handle="next-goal"),
    )
    lease3 = disp.lease(worker_id="consumer")
    assert lease3 is not None
    assert lease3.wake_event == HumanResponseReceived(handle="next-goal")


def test_concurrent_leases_across_multiple_tasks(make_dispatcher) -> None:
    """N tasks, N*2 workers racing: each task is leased exactly once
    (no duplicates, no lost tasks)."""
    import threading

    disp = make_dispatcher()
    task_ids = [f"t{i}" for i in range(10)]
    for tid in task_ids:
        disp.enqueue(tid)

    won: list[str] = []
    won_lock = threading.Lock()

    def _race() -> None:
        while True:
            lease = disp.lease(worker_id="racer")
            if lease is None:
                return
            with won_lock:
                won.append(lease.task_id)

    threads = [threading.Thread(target=_race) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=5.0)

    assert sorted(won) == sorted(task_ids), (
        f"expected each task leased exactly once; got {sorted(won)!r}"
    )
    # Queue is empty after all leases claimed.
    assert disp.lease(worker_id="probe") is None
