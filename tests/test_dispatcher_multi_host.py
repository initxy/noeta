"""Multi-host Postgres lease fencing (docs/adr/multi-host-lease-fencing.md).

Every test opens TWO PostgresDispatcher + PostgresEventLog instances
against the same isolated schema — two hosts sharing one database. The
scenarios pin the three ADR decisions: the in-tx ``SELECT ... FOR
SHARE`` fence on the emit path (D1), database-clock lease expiry when
no clock is injected (D2), and the ``worker_id`` audit column (D3) —
plus the completion-order theorem the step-attempt-recovery seal relies
on, exercised end-to-end with a crash on host A recovered on host B.

Postgres-only: sqlite / in-memory are single-host by definition.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Iterator, Optional

import pytest

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.protocols.decisions import FinishDecision, YieldForHumanDecision
from noeta.protocols.errors import InvalidLease
from noeta.protocols.events import TaskCreatedPayload, TaskStartedPayload
from noeta.protocols.messages import TextBlock
from noeta.protocols.wake import (
    NEXT_GOAL_WAKE_HANDLE,
    HumanResponseReceived,
    TimerFired,
)
from noeta.runtime.worker import run_leased_task
from noeta.testing.composer import trivial_three_segment
from tests._pg import POSTGRES_DSN_ENV, isolated_schema_dsn

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_DSN_ENV),
    reason=f"{POSTGRES_DSN_ENV} not set",
)


@pytest.fixture()
def schema_dsn() -> Iterator[str]:
    with isolated_schema_dsn() as dsn:
        yield dsn


@pytest.fixture()
def closing() -> Iterator[Callable[[Any], Any]]:
    opened: list[Any] = []

    def _track(adapter: Any) -> Any:
        opened.append(adapter)
        return adapter

    yield _track
    for adapter in opened:
        try:
            adapter.close()
        except Exception:
            pass


def _dispatcher(
    closing: Callable[[Any], Any],
    dsn: str,
    *,
    now: Optional[Callable[[], float]] = None,
    row_lock_timeout_ms: int = 5_000,
) -> Any:
    from noeta.storage.postgres import PostgresDispatcher

    return closing(
        PostgresDispatcher(
            dsn, now=now, row_lock_timeout_ms=row_lock_timeout_ms
        )
    )


def _event_log(
    closing: Callable[[Any], Any],
    dsn: str,
    *,
    lease_validator: Any = None,
    clock: Optional[Callable[[], float]] = None,
    _emit_pause: Optional[Callable[[], None]] = None,
) -> Any:
    from noeta.storage.postgres import PostgresEventLog

    return closing(
        PostgresEventLog(
            dsn,
            lease_validator=lease_validator,
            clock=clock,
            _emit_pause=_emit_pause,
        )
    )


def _task_row(disp: Any, task_id: str) -> Any:
    """Raw dispatcher row (the class docstring blesses ``_conn`` access
    for tests needing the raw rows)."""
    return disp._conn.execute(
        "SELECT * FROM dispatcher_tasks WHERE task_id = %s", (task_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# D3 — worker_id audit column
# ---------------------------------------------------------------------------


def test_lease_records_worker_id(schema_dsn, closing) -> None:
    disp = _dispatcher(closing, schema_dsn)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w42")
    assert lease is not None
    assert _task_row(disp, "t1")["worker_id"] == "w42"

    disp.release(lease.lease_id, next_state="terminal")
    assert _task_row(disp, "t1")["worker_id"] is None


def test_enqueue_force_clears_worker_id(schema_dsn, closing) -> None:
    disp = _dispatcher(closing, schema_dsn)
    disp.enqueue("t1")
    assert disp.lease(worker_id="w1") is not None

    disp.enqueue("t1")  # force-clear of the live lease
    row = _task_row(disp, "t1")
    assert row["status"] == "ready" and row["worker_id"] is None

    assert disp.lease(worker_id="w2") is not None
    assert _task_row(disp, "t1")["worker_id"] == "w2"


# ---------------------------------------------------------------------------
# D2 — database clock when ``now`` is not injected
# ---------------------------------------------------------------------------


def test_db_clock_used_when_now_not_injected(schema_dsn, closing) -> None:
    disp = _dispatcher(closing, schema_dsn)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w", lease_seconds=30.0)
    assert lease is not None

    row = _task_row(disp, "t1")
    assert float(row["lease_expires_at"]) == pytest.approx(lease.expires_at)

    db_now = float(
        disp._conn.execute(
            "SELECT EXTRACT(EPOCH FROM clock_timestamp())"
            "::double precision AS now"
        ).fetchone()["now"]
    )
    # The deadline came from the DB server's clock, not this host's:
    # within a loose RTT/latency tolerance of db_now + 30.
    assert abs(lease.expires_at - (db_now + 30.0)) < 5.0


# ---------------------------------------------------------------------------
# Cross-host lease lifecycle
# ---------------------------------------------------------------------------


def test_heartbeat_invalid_after_remote_reclaim(schema_dsn, closing) -> None:
    a_now, b_now = [1_000.0], [1_000.0]
    disp_a = _dispatcher(closing, schema_dsn, now=lambda: a_now[0])
    disp_b = _dispatcher(closing, schema_dsn, now=lambda: b_now[0])

    disp_a.enqueue("t1")
    lease = disp_a.lease(worker_id="host-a", lease_seconds=30.0)
    assert lease is not None

    b_now[0] = 10_000.0
    assert disp_b.requeue_stale() == ["t1"]

    with pytest.raises(InvalidLease):
        disp_a.heartbeat(lease.lease_id)


def test_release_after_remote_reclaim_is_invalid(schema_dsn, closing) -> None:
    a_now, b_now = [1_000.0], [1_000.0]
    disp_a = _dispatcher(closing, schema_dsn, now=lambda: a_now[0])
    disp_b = _dispatcher(closing, schema_dsn, now=lambda: b_now[0])

    disp_a.enqueue("t1")
    lease = disp_a.lease(worker_id="host-a", lease_seconds=30.0)
    assert lease is not None

    b_now[0] = 10_000.0
    assert disp_b.requeue_stale() == ["t1"]

    with pytest.raises(InvalidLease):
        disp_a.release(lease.lease_id, next_state="terminal")


def test_two_hosts_lease_fifo_no_duplicate(schema_dsn, closing) -> None:
    now = [1_000.0]
    disp_a = _dispatcher(closing, schema_dsn, now=lambda: now[0])
    disp_b = _dispatcher(closing, schema_dsn, now=lambda: now[0])

    task_ids = [f"t{i:02d}" for i in range(20)]
    for tid in task_ids:
        disp_a.enqueue(tid)

    grabbed: dict[str, list[str]] = {"a": [], "b": []}

    def _drain(disp: Any, key: str) -> None:
        while True:
            lease = disp.lease(worker_id=f"host-{key}", lease_seconds=600.0)
            if lease is None:
                return
            grabbed[key].append(lease.task_id)

    threads = [
        threading.Thread(target=_drain, args=(disp_a, "a"), daemon=True),
        threading.Thread(target=_drain, args=(disp_b, "b"), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    combined = grabbed["a"] + grabbed["b"]
    assert sorted(combined) == task_ids  # each exactly once, none lost


def test_double_sweeper_fires_timer_once(schema_dsn, closing) -> None:
    now = [1_000.0]
    disp_a = _dispatcher(closing, schema_dsn, now=lambda: now[0])
    disp_b = _dispatcher(closing, schema_dsn, now=lambda: now[0])

    disp_a.enqueue("t1")
    lease = disp_a.lease(worker_id="host-a", task_id="t1")
    assert lease is not None
    disp_a.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=TimerFired(fire_at=1_500.0),
        suspend_reason="waiting_timer",
    )

    now[0] = 2_000.0
    barrier = threading.Barrier(2)
    fired: dict[str, list[str]] = {}

    def _sweep(disp: Any, key: str) -> None:
        barrier.wait(timeout=30)
        fired[key] = disp.fire_due_timers(now=now[0])

    threads = [
        threading.Thread(target=_sweep, args=(disp_a, "a"), daemon=True),
        threading.Thread(target=_sweep, args=(disp_b, "b"), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    # Exactly one sweeper wins; the loser's locked SELECT starts after
    # the winner's COMMIT cleared fire_at and sees nothing due.
    assert sorted(fired["a"] + fired["b"]) == ["t1"]

    woken = disp_b.lease(worker_id="host-b", task_id="t1")
    assert woken is not None
    # Delivered wake is the recorded deadline blob, delivered once.
    assert woken.wake_event == TimerFired(fire_at=1_500.0)


# ---------------------------------------------------------------------------
# D1 — zombie-append fencing on the emit path
# ---------------------------------------------------------------------------


def test_zombie_emit_after_reclaim_is_rejected(schema_dsn, closing) -> None:
    """G1 aftermath: once another host reclaimed the lease and started a
    new generation, the zombie's emit raises InvalidLease and leaves no
    trace on the stream."""
    a_now, b_now = [1_000.0], [1_000.0]
    disp_a = _dispatcher(closing, schema_dsn, now=lambda: a_now[0])
    disp_b = _dispatcher(closing, schema_dsn, now=lambda: b_now[0])
    log_a = _event_log(
        closing, schema_dsn, lease_validator=disp_a, clock=lambda: a_now[0]
    )
    log_b = _event_log(
        closing, schema_dsn, lease_validator=disp_b, clock=lambda: b_now[0]
    )

    disp_a.enqueue("t1")
    lease_a = disp_a.lease(worker_id="host-a", lease_seconds=30.0)
    assert lease_a is not None

    b_now[0] = 10_000.0
    assert disp_b.requeue_stale() == ["t1"]
    lease_b = disp_b.lease(worker_id="host-b", task_id="t1")
    assert lease_b is not None
    ev_b = log_b.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id=lease_b.lease_id),
        lease_id=lease_b.lease_id,
    )

    with pytest.raises(InvalidLease):
        log_a.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="zombie", policy_name="p"),
            lease_id=lease_a.lease_id,
        )

    events = log_b.read("t1")
    assert [e.id for e in events] == [ev_b.id]


def test_inflight_emit_blocks_reclaim_and_commits_first(
    schema_dsn, closing
) -> None:
    """G1 window itself: an emit holding the FOR SHARE row lock forces a
    concurrent ``requeue_stale`` to wait, so the in-flight write commits
    BEFORE the reclaim — every L_i event seq-precedes every L_{i+1}
    event (the completion-order theorem)."""
    a_now, b_now = [1_000.0], [1_000.0]

    pause_armed = threading.Event()
    gate_reached = threading.Event()
    gate_release = threading.Event()

    def _pause() -> None:
        if not pause_armed.is_set():
            return
        gate_reached.set()
        assert gate_release.wait(timeout=60)

    disp_a = _dispatcher(closing, schema_dsn, now=lambda: a_now[0])
    disp_b = _dispatcher(closing, schema_dsn, now=lambda: b_now[0])
    log_a = _event_log(
        closing,
        schema_dsn,
        lease_validator=disp_a,
        clock=lambda: a_now[0],
        _emit_pause=_pause,
    )
    log_b = _event_log(
        closing, schema_dsn, lease_validator=disp_b, clock=lambda: b_now[0]
    )

    disp_a.enqueue("t1")
    lease_a = disp_a.lease(worker_id="host-a", lease_seconds=30.0)
    assert lease_a is not None

    emitted: dict[str, Any] = {}

    def _emit_zombie() -> None:
        emitted["envelope"] = log_a.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
            lease_id=lease_a.lease_id,
        )

    reclaimed: dict[str, list[str]] = {}

    def _reclaim() -> None:
        reclaimed["ids"] = disp_b.requeue_stale()

    pause_armed.set()
    emit_thread = threading.Thread(target=_emit_zombie, daemon=True)
    reclaim_thread = threading.Thread(target=_reclaim, daemon=True)
    emit_thread.start()
    try:
        # Probe passed, row-share lock held: the emit is parked at the
        # gate before it inserts.
        assert gate_reached.wait(timeout=30)

        b_now[0] = 10_000.0
        reclaim_thread.start()
        # The reclaim UPDATE must wait on the emit's row-share lock
        # (well within the 5s lock_timeout, since we release below).
        reclaim_thread.join(timeout=1.0)
        assert reclaim_thread.is_alive()
    finally:
        # Always release the parked emit so a failed assertion above
        # can't strand the thread mid-transaction across teardown.
        gate_release.set()

    emit_thread.join(timeout=30)
    assert not emit_thread.is_alive()
    reclaim_thread.join(timeout=30)
    assert not reclaim_thread.is_alive()
    assert reclaimed["ids"] == ["t1"]

    lease_b = disp_b.lease(worker_id="host-b", task_id="t1")
    assert lease_b is not None
    pause_armed.clear()
    ev_b = log_b.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id=lease_b.lease_id),
        lease_id=lease_b.lease_id,
    )

    ev_a = emitted["envelope"]
    assert ev_a.seq < ev_b.seq
    assert [e.id for e in log_b.read("t1")] == [ev_a.id, ev_b.id]


def test_wedged_emit_does_not_stall_reclaim_forever(
    schema_dsn, closing
) -> None:
    """Stall containment: a wedged emit holding the FOR SHARE row lock
    must not pin the global dispatcher lock indefinitely. ``requeue_stale``
    hits ``lock_timeout`` and raises rather than hanging; a normal
    lifecycle op for a DIFFERENT task then still succeeds."""
    a_now, b_now = [1_000.0], [1_000.0]

    gate_reached = threading.Event()
    gate_release = threading.Event()

    def _pause() -> None:
        gate_reached.set()
        assert gate_release.wait(timeout=60)

    disp_a = _dispatcher(closing, schema_dsn, now=lambda: a_now[0])
    disp_b = _dispatcher(
        closing,
        schema_dsn,
        now=lambda: b_now[0],
        # Shorten the bound so the test does not wait the full 5s.
        row_lock_timeout_ms=300,
    )
    log_a = _event_log(
        closing,
        schema_dsn,
        lease_validator=disp_a,
        clock=lambda: a_now[0],
        _emit_pause=_pause,
    )

    disp_a.enqueue("t1")
    lease_a = disp_a.lease(worker_id="host-a", lease_seconds=30.0)
    assert lease_a is not None
    # A second, unrelated task that a healthy host should still service.
    disp_b.enqueue("t2")

    def _emit_wedged() -> None:
        try:
            log_a.emit(
                task_id="t1",
                type="TaskCreated",
                payload=TaskCreatedPayload(goal="g", policy_name="p"),
                lease_id=lease_a.lease_id,
            )
        except Exception:  # noqa: BLE001 — released via gate at teardown
            pass

    emit_thread = threading.Thread(target=_emit_wedged, daemon=True)
    emit_thread.start()
    try:
        assert gate_reached.wait(timeout=30)  # row-share lock held, parked

        b_now[0] = 10_000.0
        # The reclaim UPDATE blocks on the wedged row lock and aborts at
        # lock_timeout instead of hanging.
        with pytest.raises(Exception) as exc:
            disp_b.requeue_stale()
        assert "lock" in str(exc.value).lower() or "timeout" in str(
            exc.value
        ).lower()

        # The global dispatcher lock was released on that abort, so an
        # unrelated task is still serviceable while t1 stays wedged.
        lease_t2 = disp_b.lease(worker_id="host-b", task_id="t2")
        assert lease_t2 is not None and lease_t2.task_id == "t2"
    finally:
        gate_release.set()
    emit_thread.join(timeout=30)
    assert not emit_thread.is_alive()


def test_clock_skew_emulation(schema_dsn, closing) -> None:
    """Injected-clock documentation of the G2 shape: host A's clock runs
    10s behind host B's. Once B reclaims, A's next emit is fenced even
    though A's local clock still believes the lease is live. (In
    production DB-clock mode this divergence cannot arise at all.)"""
    a_now, b_now = [1_000.0], [1_010.0]
    disp_a = _dispatcher(closing, schema_dsn, now=lambda: a_now[0])
    disp_b = _dispatcher(closing, schema_dsn, now=lambda: b_now[0])
    log_a = _event_log(
        closing, schema_dsn, lease_validator=disp_a, clock=lambda: a_now[0]
    )

    disp_a.enqueue("t1")
    lease_a = disp_a.lease(worker_id="host-a", lease_seconds=30.0)
    assert lease_a is not None  # expires at 1030 on A's clock

    b_now[0] = 1_040.0  # already past expiry from B's viewpoint
    assert disp_b.requeue_stale() == ["t1"]

    a_now[0] = 1_005.0  # A still believes the lease has 25s left
    assert disp_a.is_lease_valid("t1", lease_a.lease_id) is False
    with pytest.raises(InvalidLease):
        log_a.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="late", policy_name="p"),
            lease_id=lease_a.lease_id,
        )


# ---------------------------------------------------------------------------
# Step-attempt recovery across hosts (acceptance criterion 7)
# ---------------------------------------------------------------------------


class _ScriptOrRaise:
    """Scripted policy whose entries may be Exception instances —
    reaching one raises it (the simulated mid-decide crash)."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)

    def decide(self, ctx: Any, view: Any) -> Any:  # noqa: ARG002
        entry = self._script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry


class _RT:
    def __init__(self, engine: Any, log: Any, cs: Any, dispatcher: Any) -> None:
        self.engine = engine
        self.event_log = log
        self.content_store = cs
        self.dispatcher = dispatcher


def test_step_attempt_recovery_across_hosts(schema_dsn, closing) -> None:
    """A crash on host A is sealed and re-driven on host B: the seal is
    a lease-checked append under B's lease, A's generation is fenced,
    and the stream completes exactly once."""
    from noeta.storage.postgres import PostgresContentStore

    now = [1_000_000.0]

    disp_a = _dispatcher(closing, schema_dsn, now=lambda: now[0])
    log_a = _event_log(
        closing, schema_dsn, lease_validator=disp_a, clock=lambda: now[0]
    )
    cs_a = closing(PostgresContentStore(schema_dsn))
    wire_default_observers(log_a, disp_a)
    engine_a = Engine(
        event_log=log_a,
        content_store=cs_a,
        composer=trivial_three_segment(cs_a),
        policy=_ScriptOrRaise(
            [
                YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE),
                RuntimeError("simulated crash on host A"),
            ]
        ),
    )

    # Host A: opening turn to a next-goal suspend, then release.
    task = engine_a.create_task(goal="g", policy_name="scripted")
    tid = task.task_id
    disp_a.enqueue(tid)
    lease = disp_a.lease(worker_id="host-a")
    assert lease is not None
    engine_a.append_user_message(
        task, content=[TextBlock(text="g")], lease_id=lease.lease_id
    )
    task = engine_a.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "suspended"
    disp_a.release(
        lease.lease_id, next_state="suspended", wake_on=task.wake_on
    )

    # Host A: wake, start turn 2, crash mid-decide holding the lease.
    assert disp_a.wake(
        tid, HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
    )
    lease = disp_a.lease(worker_id="host-a", task_id=tid)
    assert lease is not None and lease.wake_event is not None
    task = fold(log_a, cs_a, tid)
    task = engine_a.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    task = engine_a.append_user_message(
        task, content=[TextBlock(text="turn 2")], lease_id=lease.lease_id
    )
    with pytest.raises(RuntimeError):
        engine_a.run_one_step(task, lease_id=lease.lease_id)

    # Host B: reclaim the stale lease and run recovery + re-drive.
    disp_b = _dispatcher(closing, schema_dsn, now=lambda: now[0])
    log_b = _event_log(
        closing, schema_dsn, lease_validator=disp_b, clock=lambda: now[0]
    )
    cs_b = closing(PostgresContentStore(schema_dsn))
    wire_default_observers(log_b, disp_b)
    engine_b = Engine(
        event_log=log_b,
        content_store=cs_b,
        composer=trivial_three_segment(cs_b),
        policy=_ScriptOrRaise([FinishDecision(answer="done")]),
    )

    now[0] += 100_000.0
    assert tid in disp_b.requeue_stale()
    lease_b = disp_b.lease(worker_id="host-b", task_id=tid)
    assert lease_b is not None

    outcome = run_leased_task(_RT(engine_b, log_b, cs_b, disp_b), lease_b)
    assert outcome == "woken"

    events = log_b.read(tid)
    seals = [e for e in events if e.type == "StepAttemptAbandoned"]
    assert len(seals) == 1 and seals[0].payload.reason == "auto_redrive"
    assert any(e.type == "TaskCompleted" for e in events)

    # The zombie generation on host A is fenced: its lease can no
    # longer write to the stream it lost.
    with pytest.raises(InvalidLease):
        log_a.emit(
            task_id=tid,
            type="TaskStarted",
            payload=TaskStartedPayload(lease_id=lease.lease_id),
            lease_id=lease.lease_id,
        )
