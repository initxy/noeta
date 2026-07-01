"""InMemoryDispatcher: minimal usable shape of the 7 protocol methods."""

from __future__ import annotations

import itertools

import pytest

from noeta.protocols.errors import InvalidLease
from noeta.protocols.wake import HumanResponseReceived
from noeta.storage.memory import InMemoryDispatcher


def test_enqueue_then_lease_returns_lease_for_task() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")

    lease = disp.lease(worker_id="w1")

    assert lease is not None
    assert lease.task_id == "t1"
    assert lease.lease_id.startswith("lease-")


def test_lease_returns_none_when_no_ready_tasks() -> None:
    disp = InMemoryDispatcher()

    assert disp.lease(worker_id="w1") is None


def test_lease_then_release_terminal_removes_task_from_ready_pool() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    disp.release(lease.lease_id, next_state="terminal")

    assert disp.lease(worker_id="w1") is None


def test_release_suspended_then_wake_requeues_task() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None
    wake_on = HumanResponseReceived(handle="ping")
    disp.release(lease.lease_id, next_state="suspended", wake_on=wake_on)

    requeued = disp.wake("t1", HumanResponseReceived(handle="ping"))

    assert requeued is True
    lease2 = disp.lease(worker_id="w1")
    assert lease2 is not None
    assert lease2.task_id == "t1"


def test_wake_before_suspend_queues_pending_and_takes_effect_on_release() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    # Wake arrives while task is still leased: queue as pending.
    accepted = disp.wake("t1", HumanResponseReceived(handle="ping"))
    assert accepted is False

    # When the task suspends with matching wake_on, the pending event
    # should immediately requeue it.
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=HumanResponseReceived(handle="ping"),
    )

    lease2 = disp.lease(worker_id="w1")
    assert lease2 is not None
    assert lease2.task_id == "t1"


def test_heartbeat_extends_lease_expiry() -> None:
    clock = iter(itertools.count(0, 10))

    def now() -> float:
        return float(next(clock))

    disp = InMemoryDispatcher(now=now)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1", lease_seconds=5)
    assert lease is not None
    first_expiry = lease.expires_at

    new_expiry = disp.heartbeat(lease.lease_id, lease_seconds=5)

    assert new_expiry > first_expiry


def test_heartbeat_unknown_lease_raises_invalid_lease() -> None:
    disp = InMemoryDispatcher()

    with pytest.raises(InvalidLease):
        disp.heartbeat("lease-bogus")


def test_requeue_stale_recovers_expired_lease() -> None:
    current_time = [0.0]

    def now() -> float:
        return current_time[0]

    disp = InMemoryDispatcher(now=now)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1", lease_seconds=5.0)
    assert lease is not None

    # Advance past the lease window.
    current_time[0] = 100.0
    requeued = disp.requeue_stale()

    assert requeued == ["t1"]
    lease2 = disp.lease(worker_id="w1")
    assert lease2 is not None and lease2.task_id == "t1"


def test_fail_retryable_requeues_task() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    disp.fail(lease.lease_id, retryable=True)

    assert disp.lease(worker_id="w1") is not None


def test_fail_terminal_does_not_requeue() -> None:
    disp = InMemoryDispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    disp.fail(lease.lease_id, retryable=False)

    assert disp.lease(worker_id="w1") is None
