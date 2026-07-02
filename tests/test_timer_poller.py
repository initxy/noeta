"""Timer poller + ``wait_external`` un-stub (deferred structural round).

``wait_timer`` suspends used to hang forever: ``TimerFired`` existed
end-to-end through the storage layer but nothing ever produced it. The
producer is ``Dispatcher.fire_due_timers`` (both adapters) driven by the
WorkerLoop's ``maybe_poll_timers`` interval gate. The delivered wake is
the **recorded deadline** (byte-stable across H2 re-delivery), not
``TimerFired(fire_at=now)``.

``WaitExternalDecision`` used to hit the ``NotImplementedError``
fallthrough; it now suspends on the new ``ExternalEvent`` wake variant
(projection-matching on ``event_kind``).
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.core.engine import Engine
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
    WaitExternalDecision,
    WaitTimerDecision,
)
from noeta.protocols.wake import (
    ExternalEvent,
    HumanResponseReceived,
    TimerFired,
    matches_wake,
)
from noeta.runtime.worker import WorkerLoop
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.storage.sqlite.dispatcher import SqliteDispatcher
from noeta.testing.composer import trivial_three_segment


# ---------------------------------------------------------------------------
# Adapter parametrisation (mirrors test_wake_resume.py)
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def make_dispatcher(request):
    kind = request.param

    def _factory() -> Any:
        if kind == "memory":
            return InMemoryDispatcher()
        return SqliteDispatcher(":memory:")

    factory = _factory
    factory.kind = kind  # type: ignore[attr-defined]
    return factory


def _suspend_on_timer(disp: Any, task_id: str, fire_at: float) -> None:
    """enqueue → lease → release(suspended, TimerFired) — the shape the
    worker produces for a ``wait_timer`` exit."""
    disp.enqueue(task_id)
    lease = disp.lease(worker_id="w", task_id=task_id)
    assert lease is not None
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=TimerFired(fire_at=fire_at),
        suspend_reason="waiting_timer",
    )


# ---------------------------------------------------------------------------
# matches_wake — ExternalEvent projection row of the L0 truth table
# ---------------------------------------------------------------------------


def test_matches_wake_external_event_projects_on_event_kind() -> None:
    assert matches_wake(ExternalEvent("webhook-a"), ExternalEvent("webhook-a"))
    assert not matches_wake(
        ExternalEvent("webhook-a"), ExternalEvent("webhook-b")
    )


def test_matches_wake_external_event_cross_variant_is_false() -> None:
    assert not matches_wake(ExternalEvent("h"), HumanResponseReceived("h"))
    assert not matches_wake(HumanResponseReceived("h"), ExternalEvent("h"))
    assert not matches_wake(ExternalEvent("t"), TimerFired(fire_at=1.0))


def test_external_event_canonical_roundtrip() -> None:
    from noeta.protocols.canonical import (
        from_canonical_bytes,
        to_canonical_bytes,
    )

    evt = ExternalEvent(event_kind="bus:orders")
    assert from_canonical_bytes(to_canonical_bytes(evt)) == evt


# ---------------------------------------------------------------------------
# fire_due_timers — both adapters
# ---------------------------------------------------------------------------


def test_fire_due_timers_wakes_only_due_timers(make_dispatcher) -> None:
    disp = make_dispatcher()
    _suspend_on_timer(disp, "t-due", fire_at=1_000.0)
    _suspend_on_timer(disp, "t-later", fire_at=2_000.0)

    assert disp.fire_due_timers(now=999.9) == []
    # Inclusive boundary (matches_wake's `>=`): fire exactly at deadline.
    assert disp.fire_due_timers(now=1_000.0) == ["t-due"]
    # The due task is ready and leasable; the later one still suspended.
    lease = disp.lease(worker_id="w", task_id="t-due")
    assert lease is not None
    assert disp.lease(worker_id="w", task_id="t-later") is None


def test_fire_due_timers_delivers_recorded_deadline_not_now(
    make_dispatcher,
) -> None:
    """The wake event is the stored deadline — byte-stable across H2
    re-delivery — never ``TimerFired(fire_at=now)``."""
    disp = make_dispatcher()
    _suspend_on_timer(disp, "t1", fire_at=1_000.0)
    assert disp.fire_due_timers(now=5_555.5) == ["t1"]
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease is not None
    assert lease.wake_event == TimerFired(fire_at=1_000.0)


def test_fire_due_timers_skips_undecodable_row_and_still_fires_others() -> None:
    """sqlite-only: one corrupt ``wake_on_canonical`` blob must not abort the
    whole sweep. Before the per-row guard, an undecodable row raised, rolled
    back the transaction, and stalled EVERY ``wait_timer`` suspend every poll."""
    disp = SqliteDispatcher(":memory:")
    _suspend_on_timer(disp, "t-good", fire_at=1_000.0)
    _suspend_on_timer(disp, "t-corrupt", fire_at=1_000.0)
    # Corrupt the second task's stored wake blob directly (not canonical bytes).
    disp._conn.execute(
        "UPDATE dispatcher_tasks SET wake_on_canonical = ? WHERE task_id = ?",
        (b"\x00 not-canonical", "t-corrupt"),
    )
    disp._conn.commit()

    fired = disp.fire_due_timers(now=1_000.0)

    assert fired == ["t-good"]  # good one fired; corrupt row skipped, no raise
    # The good task is ready/leasable; the corrupt row is left suspended.
    assert disp.lease(worker_id="w", task_id="t-good") is not None
    assert disp.lease(worker_id="w", task_id="t-corrupt") is None


def test_migration_7_backfills_fire_at_for_legacy_timer_suspend(
    tmp_path, monkeypatch
) -> None:
    """sqlite-only: a pre-migration-7 DB (no ``fire_at`` column) upgrades in
    place, and migration 7's backfill decodes each suspended timer's blob so
    the indexed sweep still fires it — an in-flight ``wait_timer`` suspend is
    never stranded across the upgrade. A non-timer suspend backfills to NULL
    (it must never surface on the timer index)."""
    from noeta.protocols.canonical import to_canonical_bytes
    from noeta.storage.sqlite import migrations as migrations_module
    from noeta.storage.sqlite.migrations import MIGRATIONS

    db = tmp_path / "timer_backfill.sqlite"
    # Build a v6 DB (reclaim_count present, fire_at absent) and hand-write the
    # rows an OLD dispatcher would have left — a raw INSERT, since the current
    # release path already writes the not-yet-existent fire_at column.
    v6_only = [m for m in MIGRATIONS if m.version <= 6]
    monkeypatch.setattr(migrations_module, "MIGRATIONS", v6_only)
    disp = SqliteDispatcher(str(db))
    disp._conn.execute(
        "INSERT INTO dispatcher_tasks "
        "(task_id, status, wake_on_canonical, suspend_reason) "
        "VALUES (?, 'suspended', ?, 'waiting_timer')",
        ("legacy-timer", to_canonical_bytes(TimerFired(fire_at=1_000.0))),
    )
    disp._conn.execute(
        "INSERT INTO dispatcher_tasks "
        "(task_id, status, wake_on_canonical, suspend_reason) "
        "VALUES (?, 'suspended', ?, 'waiting_human')",
        ("legacy-human", to_canonical_bytes(HumanResponseReceived(handle="h"))),
    )
    disp._conn.commit()
    disp.close()

    monkeypatch.undo()
    disp = SqliteDispatcher(str(db))  # migration 7 runs: ADD COLUMN + backfill
    try:
        timer_fire_at = disp._conn.execute(
            "SELECT fire_at FROM dispatcher_tasks WHERE task_id = ?",
            ("legacy-timer",),
        ).fetchone()["fire_at"]
        assert timer_fire_at == 1_000.0
        human_fire_at = disp._conn.execute(
            "SELECT fire_at FROM dispatcher_tasks WHERE task_id = ?",
            ("legacy-human",),
        ).fetchone()["fire_at"]
        assert human_fire_at is None
        # The backfilled timer still fires off the indexed sweep; the human
        # suspend never surfaces on it.
        assert disp.fire_due_timers(now=1_000.0) == ["legacy-timer"]
    finally:
        disp.close()


def test_fire_due_timers_idle_poll_skips_write_transaction(monkeypatch) -> None:
    """sqlite-only perf invariant: a poll with nothing due returns off the
    read-only probe WITHOUT opening ``BEGIN IMMEDIATE`` — the ~1s idle poll no
    longer takes the write lock. A due poll opens exactly one."""
    from noeta.storage.sqlite import dispatcher as disp_mod

    calls = {"n": 0}
    real = disp_mod._begin_immediate_with_retry

    def _spy(conn: Any) -> None:
        calls["n"] += 1
        return real(conn)

    monkeypatch.setattr(disp_mod, "_begin_immediate_with_retry", _spy)
    disp = SqliteDispatcher(":memory:")
    _suspend_on_timer(disp, "t1", fire_at=1_000.0)
    calls["n"] = 0  # ignore the setup's transactions

    assert disp.fire_due_timers(now=999.0) == []
    assert calls["n"] == 0  # nothing due → no write transaction opened
    assert disp.fire_due_timers(now=1_000.0) == ["t1"]
    assert calls["n"] == 1  # due → exactly one


def test_fire_due_timers_ignores_non_timer_suspends_and_other_states(
    make_dispatcher,
) -> None:
    disp = make_dispatcher()
    # Suspended on a human handle — not a timer; must not fire.
    disp.enqueue("t-human")
    lease = disp.lease(worker_id="w", task_id="t-human")
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=HumanResponseReceived(handle="h1"),
    )
    # Ready task — must not be touched.
    disp.enqueue("t-ready")
    # Terminal task — must not be touched.
    disp.enqueue("t-done")
    lease = disp.lease(worker_id="w", task_id="t-done")
    disp.release(lease.lease_id, next_state="terminal")

    assert disp.fire_due_timers(now=10_000.0) == []
    assert disp.lease(worker_id="w", task_id="t-human") is None


def test_fire_due_timers_is_idempotent_until_consumed(make_dispatcher) -> None:
    """A fired timer moves to ready (leased next), so a second poll must
    not double-fire; the matched wake follows the normal H2 consume
    discipline (survives the lease, cleared by a consuming release)."""
    disp = make_dispatcher()
    _suspend_on_timer(disp, "t1", fire_at=100.0)
    assert disp.fire_due_timers(now=100.0) == ["t1"]
    assert disp.fire_due_timers(now=100.0) == []
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease.wake_event == TimerFired(fire_at=100.0)
    # Still nothing to fire while leased.
    assert disp.fire_due_timers(now=100.0) == []
    # Consuming release clears the matched wake for good.
    disp.release(
        lease.lease_id,
        next_state="terminal",
        consumed_wake_event=TimerFired(fire_at=100.0),
    )
    assert disp.fire_due_timers(now=100.0) == []


# ---------------------------------------------------------------------------
# WorkerLoop.maybe_poll_timers — interval gate + wall-clock seam
# ---------------------------------------------------------------------------


class _RT:
    def __init__(self, dispatcher: Any) -> None:
        self.dispatcher = dispatcher
        self.engine = None
        self.event_log = None
        self.content_store = None


def test_maybe_poll_timers_runs_on_interval_with_injected_clocks() -> None:
    """Cadence uses the monotonic ``clock``; the due-check uses the
    injected wall-clock ``now_fn`` (mirrors
    test_maybe_sweep_runs_on_interval_with_injected_clock)."""

    class _TimerDispatcher:
        def __init__(self) -> None:
            self.polls: list[float] = []

        def fire_due_timers(self, *, now: float) -> list[str]:
            self.polls.append(now)
            return []

    cadence = {"t": 100.0}
    wall = {"t": 1_000_000.0}
    disp = _TimerDispatcher()
    loop = WorkerLoop(
        _RT(disp),
        worker_id="w",
        timer_poll_interval=1.0,
        clock=lambda: cadence["t"],
        now_fn=lambda: wall["t"],
        heartbeat_interval=0,
    )
    # Not enough cadence time elapsed yet.
    assert loop.maybe_poll_timers() is False
    assert disp.polls == []
    # Jump past the interval — the poll passes the WALL clock through.
    cadence["t"] = 101.5
    assert loop.maybe_poll_timers() is True
    assert disp.polls == [1_000_000.0]
    # Immediately after, the window resets.
    assert loop.maybe_poll_timers() is False
    assert disp.polls == [1_000_000.0]


def test_maybe_poll_timers_fires_due_task_and_emits_reliability() -> None:
    events: list[Any] = []
    disp = InMemoryDispatcher()
    _suspend_on_timer(disp, "t1", fire_at=500.0)
    cadence = {"t": 100.0}
    loop = WorkerLoop(
        _RT(disp),
        worker_id="w",
        timer_poll_interval=1.0,
        clock=lambda: cadence["t"],
        now_fn=lambda: 500.0,
        heartbeat_interval=0,
        reliability_sink=events.append,
    )
    cadence["t"] = 102.0
    assert loop.maybe_poll_timers() is True
    assert [e.kind for e in events] == ["timers_fired"]
    assert events[0].detail == {"count": 1, "task_ids": ["t1"]}
    assert disp.lease(worker_id="w", task_id="t1") is not None


def test_maybe_poll_timers_tolerates_dispatcher_without_the_method() -> None:
    """A pre-timer external Dispatcher adapter degrades to a no-op poll
    instead of crashing the loop."""

    class _LegacyDispatcher:
        pass

    cadence = {"t": 100.0}
    loop = WorkerLoop(
        _RT(_LegacyDispatcher()),
        worker_id="w",
        timer_poll_interval=1.0,
        clock=lambda: cadence["t"],
        heartbeat_interval=0,
    )
    cadence["t"] = 102.0
    assert loop.maybe_poll_timers() is True  # poll ran; nothing to call


def test_timer_poll_interval_zero_disables_the_poll() -> None:
    class _BoomDispatcher:
        def fire_due_timers(self, *, now: float) -> list[str]:
            raise AssertionError("must not be called when disabled")

    loop = WorkerLoop(
        _RT(_BoomDispatcher()),
        worker_id="w",
        timer_poll_interval=0,
        clock=lambda: 1e9,
        heartbeat_interval=0,
    )
    assert loop.maybe_poll_timers() is False


# ---------------------------------------------------------------------------
# End-to-end: wait_timer suspend → poll fires → resume finishes
# ---------------------------------------------------------------------------


def _wire_engine(policy: Any, dispatcher: Any, clock: Any) -> tuple[Any, Any]:
    store = InMemoryContentStore()
    log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(log, dispatcher)
    engine = Engine(
        event_log=log,
        content_store=store,
        composer=trivial_three_segment(store),
        policy=policy,
        clock=clock,
    )
    return engine, log


def test_wait_timer_full_cycle_resumes_and_finishes(make_dispatcher) -> None:
    """The liveness proof the review demanded: a ``wait_timer`` suspend
    is woken by the poll (no external wake producer involved) and the
    resumed turn runs to terminal."""
    disp = make_dispatcher()
    policy = StubScriptedPolicy(
        [WaitTimerDecision(seconds=30), FinishDecision(answer="done")]
    )
    engine, log = _wire_engine(policy, disp, clock=lambda: 1_000.0)
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w", task_id=task.task_id)
    result = engine.run_one_step(task, lease_id=lease.lease_id)
    assert result.status == "suspended"
    assert result.wake_on == TimerFired(fire_at=1_030.0)
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=result.wake_on,
        suspend_reason="waiting_timer",
    )

    assert disp.fire_due_timers(now=1_029.0) == []
    assert disp.fire_due_timers(now=1_030.0) == [task.task_id]

    lease = disp.lease(worker_id="w", task_id=task.task_id)
    assert lease.wake_event == TimerFired(fire_at=1_030.0)
    woken = engine.note_woken(
        result, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    finished = engine.run_one_step(woken, lease_id=lease.lease_id)
    assert finished.status == "terminal"
    disp.release(
        lease.lease_id,
        next_state="terminal",
        consumed_wake_event=TimerFired(fire_at=1_030.0),
    )
    types = [e.type for e in log.read(task.task_id)]
    assert "TaskWoken" in types
    assert types[-1] == "TaskCompleted"


# ---------------------------------------------------------------------------
# wait_external — the un-stubbed exit
# ---------------------------------------------------------------------------


def test_engine_wait_external_decision_suspends_with_external_wake() -> None:
    policy = StubScriptedPolicy(
        [WaitExternalDecision(event_kind="webhook:payment")]
    )
    disp = InMemoryDispatcher()
    engine, log = _wire_engine(policy, disp, clock=lambda: 1_000.0)
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w", task_id=task.task_id)
    result = engine.run_one_step(task, lease_id=lease.lease_id)
    assert result.status == "suspended"
    assert result.wake_on == ExternalEvent(event_kind="webhook:payment")
    types = [e.type for e in log.read(task.task_id)]
    suspend_idx = types.index("TaskSuspended")
    assert types[suspend_idx - 1] == "TaskSnapshot"
    payload = log.read(task.task_id)[suspend_idx].payload
    assert payload.reason == "waiting_external"


def test_external_wake_delivery_resumes_suspended_task(
    make_dispatcher,
) -> None:
    """An external ingress calling ``dispatcher.wake`` with the declared
    ``event_kind`` requeues the task; a mismatched kind buffers."""
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w", task_id="t1")
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=ExternalEvent(event_kind="bus:orders"),
        suspend_reason="waiting_external",
    )
    assert disp.wake("t1", ExternalEvent(event_kind="bus:other")) is False
    assert disp.lease(worker_id="w", task_id="t1") is None
    assert disp.wake("t1", ExternalEvent(event_kind="bus:orders")) is True
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease is not None
    assert lease.wake_event == ExternalEvent(event_kind="bus:orders")
