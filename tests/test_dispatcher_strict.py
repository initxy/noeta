"""Dispatcher full lease lifecycle (issue 06).

Augments the relaxed Phase-0 tests in ``test_dispatcher.py`` by checking
the strict behaviours the kernel needs once Engine + EventLog enforce
the three concurrency layers:

* ``is_lease_valid(task_id, lease_id)`` reports lease liveness so the
  EventLog can refuse writes from expired / released leases.
* ``heartbeat`` honours a per-lease hard cap and force-releases the
  task to ``suspended`` with ``lease_quota_exceeded`` when exceeded.
* ``release`` rejects unknown / already-released leases.
* ``fail`` retryable bounded by ``max_attempts`` (default 3).
* Two workers racing for the same task: only one wins the lease.
* ``requeue_stale`` makes the stale task available; the original
  lease_id becomes invalid for any subsequent write.
"""

from __future__ import annotations

import pytest

from noeta.protocols.errors import InvalidLease
from noeta.storage.memory import InMemoryDispatcher


# ---------------------------------------------------------------------------
# is_lease_valid
# ---------------------------------------------------------------------------


def test_is_lease_valid_true_for_freshly_granted_lease() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    assert disp.is_lease_valid("t1", lease.lease_id) is True


def test_is_lease_valid_false_for_unknown_lease() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    disp.lease(worker_id="w1")

    assert disp.is_lease_valid("t1", "lease-bogus") is False


def test_is_lease_valid_false_after_release() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None
    disp.release(lease.lease_id, next_state="terminal")

    assert disp.is_lease_valid("t1", lease.lease_id) is False


def test_is_lease_valid_false_after_expiry() -> None:
    now = [0.0]
    disp = InMemoryDispatcher(now=lambda: now[0])
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1", lease_seconds=5.0)
    assert lease is not None

    now[0] = 100.0  # past expiry
    assert disp.is_lease_valid("t1", lease.lease_id) is False


def test_is_lease_valid_false_when_task_id_does_not_match() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    disp.enqueue("t2")
    lease1 = disp.lease(worker_id="w1")
    assert lease1 is not None

    # lease_id is real but belongs to a different task.
    assert disp.is_lease_valid("t2", lease1.lease_id) is False


# ---------------------------------------------------------------------------
# heartbeat hard cap
# ---------------------------------------------------------------------------


def test_heartbeat_within_cap_keeps_extending() -> None:
    now = [0.0]
    disp = InMemoryDispatcher(now=lambda: now[0], heartbeat_max=3)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1", lease_seconds=5.0)
    assert lease is not None

    for _ in range(3):
        now[0] += 1.0
        disp.heartbeat(lease.lease_id, lease_seconds=5.0)

    # After 3 heartbeats (== cap) the lease is still alive.
    assert disp.is_lease_valid("t1", lease.lease_id) is True


def test_heartbeat_over_cap_force_releases_to_suspended() -> None:
    now = [0.0]
    disp = InMemoryDispatcher(now=lambda: now[0], heartbeat_max=2)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1", lease_seconds=5.0)
    assert lease is not None

    # Two heartbeats are fine; the third must trip the cap and force a
    # release with reason ``lease_quota_exceeded``.
    disp.heartbeat(lease.lease_id, lease_seconds=5.0)
    disp.heartbeat(lease.lease_id, lease_seconds=5.0)
    with pytest.raises(InvalidLease):
        disp.heartbeat(lease.lease_id, lease_seconds=5.0)

    # The lease is gone, the task suspended with the documented reason.
    assert disp.is_lease_valid("t1", lease.lease_id) is False
    assert disp.task_status("t1") == "suspended"
    assert disp.suspend_reason("t1") == "lease_quota_exceeded"


# ---------------------------------------------------------------------------
# release validation
# ---------------------------------------------------------------------------


def test_release_unknown_lease_raises_invalid_lease() -> None:
    disp = InMemoryDispatcher()
    with pytest.raises(InvalidLease):
        disp.release("lease-bogus", next_state="terminal")


def test_release_twice_raises_invalid_lease_second_time() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None
    disp.release(lease.lease_id, next_state="terminal")

    with pytest.raises(InvalidLease):
        disp.release(lease.lease_id, next_state="terminal")


def test_release_suspended_persists_wake_on_field() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    disp.release(lease.lease_id, next_state="suspended", wake_on="ping")

    assert disp.wake_on("t1") == "ping"


# ---------------------------------------------------------------------------
# fail attempts
# ---------------------------------------------------------------------------


def test_fail_retryable_within_max_attempts_requeues() -> None:
    disp = InMemoryDispatcher(max_fail_attempts=3)
    disp.enqueue("t1")

    for _ in range(3):
        lease = disp.lease(worker_id="w1")
        assert lease is not None
        disp.fail(lease.lease_id, retryable=True)

    # After max attempts, the task should be terminal/failed, not ready.
    assert disp.lease(worker_id="w1") is None
    assert disp.task_status("t1") == "terminal"


def test_fail_non_retryable_marks_terminal_immediately() -> None:
    disp = InMemoryDispatcher(max_fail_attempts=3)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    disp.fail(lease.lease_id, retryable=False, reason="boom")

    assert disp.task_status("t1") == "terminal"


def test_fail_unknown_lease_raises_invalid_lease() -> None:
    disp = InMemoryDispatcher()
    with pytest.raises(InvalidLease):
        disp.fail("lease-bogus", retryable=False)


# ---------------------------------------------------------------------------
# Multi-worker race
# ---------------------------------------------------------------------------


def test_two_workers_only_one_wins_a_single_task() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")

    lease_a = disp.lease(worker_id="wA")
    lease_b = disp.lease(worker_id="wB")

    assert lease_a is not None
    assert lease_b is None  # Only one ready task; wB gets nothing.


def test_release_invalid_next_state_raises_value_error() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    with pytest.raises(ValueError, match="invalid next_state"):
        disp.release(lease.lease_id, next_state="bogus")


def test_task_status_and_wake_on_return_none_for_unknown_task() -> None:
    disp = InMemoryDispatcher()
    assert disp.task_status("unknown") is None
    assert disp.wake_on("unknown") is None
    assert disp.suspend_reason("unknown") is None


def test_release_terminal_clears_wake_on() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    disp.release(lease.lease_id, next_state="terminal")

    # Terminal release must not carry a wake_on (it's a one-way exit).
    assert disp.wake_on("t1") is None
    assert disp.task_status("t1") == "terminal"


def test_requeue_stale_invalidates_original_lease_id() -> None:
    now = [0.0]
    disp = InMemoryDispatcher(now=lambda: now[0])
    disp.enqueue("t1")
    lease_a = disp.lease(worker_id="wA", lease_seconds=5.0)
    assert lease_a is not None

    now[0] = 100.0
    requeued = disp.requeue_stale()
    assert requeued == ["t1"]

    # Original lease must not validate anymore — a stale worker that
    # wakes up cannot continue writing.
    assert disp.is_lease_valid("t1", lease_a.lease_id) is False

    # Another worker can now lease.
    lease_b = disp.lease(worker_id="wB")
    assert lease_b is not None and lease_b.task_id == "t1"
    assert lease_b.lease_id != lease_a.lease_id
