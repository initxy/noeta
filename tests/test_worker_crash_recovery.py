"""H1 — daemon hardening: bounded process-shutdown, crash recovery, and
process-local reliability events.

Part 1 (deterministic fakes + monkeypatched ``fold``) exercises the new
worker control paths without an Engine: abandon-after-grace
process-shutdown (and that NO further lease is taken), and the four
symptom `ReliabilityEvent`s — all process-local, none touching the
EventLog.

Part 2 (real `build_runtime` bundle, InMemory **and** Sqlite) proves the
recoverable crash class: a **lease-only / before-first-durable-step-
event** crash → `requeue_stale` → a fresh worker drives to completion,
and the recovered recording replays the **same event sequence** as
a clean no-crash run; plus a poison task → bounded retry → **terminal
asserted from the dispatcher final state** (not a worker event).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from noeta.protocols.errors import InvalidLease
from noeta.runtime import worker as worker_mod
from noeta.runtime.worker import ReliabilityEvent, WorkerLoop, _HeartbeatRunner


# ---------------------------------------------------------------------------
# Part 1 — deterministic fakes
# ---------------------------------------------------------------------------


@dataclass
class _Lease:
    task_id: str
    lease_id: str
    wake_event: Any = None


class _FakeDispatcher:
    def __init__(
        self, leases: list[Optional[_Lease]], requeue: Optional[list[str]] = None
    ) -> None:
        self._leases = list(leases)
        self._requeue = list(requeue or [])
        self.calls: list[Any] = []

    def lease(self, *, worker_id: str, lease_seconds: float, task_id: Any = None) -> Any:
        self.calls.append("lease")
        return self._leases.pop(0) if self._leases else None

    def release(self, lease_id: str, *, next_state: str, wake_on: Any = None) -> None:
        self.calls.append(("release", next_state))

    def fail(self, lease_id: str, *, retryable: bool = False, reason: str = "") -> None:
        self.calls.append(("fail", retryable))

    def heartbeat(self, lease_id: str, *, lease_seconds: float) -> None:
        self.calls.append("heartbeat")

    def requeue_stale(self) -> list[str]:
        self.calls.append("requeue_stale")
        return list(self._requeue)

    def wake(self, task_id: str, wake_event: Any) -> bool:
        return True


class _FakeEngine:
    def __init__(self, run_one_step: Any) -> None:
        self.run_one_step = run_one_step

    def note_woken(self, task: Any, *, lease_id: str, wake_event: Any) -> Any:
        return task


def _rt(engine: Any, dispatcher: Any) -> Any:
    # ``event_log.read`` returns an empty stream so the drained path's
    # interrupted-attempt scan (step-attempt-recovery) finds nothing and
    # runs the bare step — the pre-recovery Part-1 control flow.
    return SimpleNamespace(
        engine=engine,
        event_log=SimpleNamespace(read=lambda task_id, **kw: []),
        content_store=None,
        dispatcher=dispatcher,
    )


class _CaptureSink:
    def __init__(self) -> None:
        self.events: list[ReliabilityEvent] = []

    def __call__(self, e: ReliabilityEvent) -> None:
        self.events.append(e)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


def _task(status: str = "running", wake_on: Any = None) -> Any:
    return SimpleNamespace(status=status, wake_on=wake_on, task_id="t1")


def test_abandon_after_grace_is_process_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step stuck past the grace → run_forever returns, loop.abandoned,
    NO further lease taken, and `shutdown_abandoned` emitted."""
    started = threading.Event()
    release_step = threading.Event()

    def _blocking_step(task: Any, *, lease_id: str, cancelled: Any = None) -> Any:
        # ``cancelled`` is the cooperative-stop poll ``run_leased_task`` now
        # threads into every ``run_one_step``; this blocking fake ignores it.
        started.set()
        release_step.wait(5.0)
        return _task("terminal")

    monkeypatch.setattr(worker_mod, "fold", lambda *a, **k: _task("running"))
    disp = _FakeDispatcher(leases=[_Lease("t1", "L1"), _Lease("t2", "L2")])
    sink = _CaptureSink()
    # A monotonically jumping clock: each read advances 100s, so once the
    # grace deadline is set (read1 + grace) the very next read exceeds it
    # → abandon, with no real wall-clock wait.
    import itertools

    ticks = itertools.count(0.0, 100.0)
    loop = WorkerLoop(
        _rt(_FakeEngine(_blocking_step), disp),
        heartbeat_interval=0,
        stale_sweep_interval=0,
        shutdown_grace_s=10.0,
        clock=lambda: next(ticks),
        sleep=lambda _s: None,
        reliability_sink=sink,
        step_poll_s=0.01,
    )
    t = threading.Thread(target=loop.run_forever)
    t.start()
    try:
        assert started.wait(3.0), "step never started"
        loop.stop()
        t.join(timeout=3.0)
        assert not t.is_alive(), "run_forever did not return after abandon"
        assert loop.abandoned is True
        assert disp.calls.count("lease") == 1  # no new lease after abandon
        assert "shutdown_abandoned" in sink.kinds()
    finally:
        release_step.set()
        t.join(timeout=3.0)


def test_suspended_without_wake_symptom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_mod, "fold", lambda *a, **k: _task("suspended", wake_on="W")
    )
    disp = _FakeDispatcher(leases=[_Lease("t1", "L1", wake_event=None)])
    sink = _CaptureSink()
    loop = WorkerLoop(
        _rt(_FakeEngine(lambda *a, **k: _task("running")), disp),
        heartbeat_interval=0,
        reliability_sink=sink,
        step_poll_s=0.01,
    )
    assert loop.tick() is True
    assert ("release", "suspended") in disp.calls
    assert "suspended_without_wake" in sink.kinds()


def test_step_failed_retryable_symptom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_mod, "fold", lambda *a, **k: _task("running"))

    def _boom(task: Any, *, lease_id: str) -> Any:
        raise RuntimeError("step blew up")

    disp = _FakeDispatcher(leases=[_Lease("t1", "L1")])
    sink = _CaptureSink()
    loop = WorkerLoop(
        _rt(_FakeEngine(_boom), disp),
        heartbeat_interval=0,
        reliability_sink=sink,
        step_poll_s=0.01,
    )
    assert loop.tick() is True
    assert ("fail", True) in disp.calls
    assert "step_failed_retryable" in sink.kinds()


def test_stale_requeued_symptom() -> None:
    disp = _FakeDispatcher(leases=[], requeue=["t1", "t2"])
    sink = _CaptureSink()
    clock_state = {"t": 0.0}  # last_sweep is set to 0 at construction
    loop = WorkerLoop(
        _rt(_FakeEngine(lambda *a, **k: _task()), disp),
        heartbeat_interval=0,
        stale_sweep_interval=1.0,
        clock=lambda: clock_state["t"],
        reliability_sink=sink,
    )
    clock_state["t"] = 100.0  # advance well past the sweep interval
    assert loop.maybe_sweep() is True
    ev = [e for e in sink.events if e.kind == "stale_requeued"]
    assert ev and ev[0].detail["count"] == 2


def test_heartbeat_invalid_lease_symptom() -> None:
    class _D:
        def heartbeat(self, lease_id: str, *, lease_seconds: float) -> None:
            raise InvalidLease("cap or expired or requeued — cause unknowable")

    sink = _CaptureSink()
    # wait() returns False once (→ one heartbeat attempt), then True (stop).
    calls = {"n": 0}

    def _wait(_interval: float) -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    hb = _HeartbeatRunner(
        _D(), _Lease("t1", "L1"), interval=0.0, lease_seconds=1.0,
        wait=_wait, reliability_sink=sink,
    )
    hb.start()
    hb.stop()
    assert "heartbeat_invalid_lease" in sink.kinds()


def test_reliability_events_are_process_local_not_eventlog() -> None:
    # The sink is just a callable; events never reach an EventLog. A guard
    # against accidentally turning ReliabilityEvent into an L0 type.
    from noeta.protocols import events as l0_events

    assert not hasattr(l0_events, "ReliabilityEvent")


# ---------------------------------------------------------------------------
# Part 2 — real bundle: recoverable crash replays same sequence + poison → terminal
# ---------------------------------------------------------------------------


def _end_turn() -> Any:
    from noeta.protocols.messages import LLMResponse, TextBlock, Usage

    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="done")],
        usage=Usage(uncached=1, output=1),
    )


def _bundle(sqlite_path: str, provider: Any) -> Any:
    from noeta.testing.profile import (
        build_runtime,
        build_tools,
        default_budget,
        default_permission_policy,
    )

    return build_runtime(
        provider=provider,
        model="m",
        system_prompt="p",
        tools=build_tools(),
        sqlite_path=sqlite_path,
        sse_broadcaster=None,
        max_steps=5,
        permission_policy=default_permission_policy(),
        budget=default_budget(),
    )


class _OneShotProvider:
    """Returns end_turn once per request (deterministic across runs)."""

    def complete(self, request: Any) -> Any:
        return _end_turn()


def _event_types(event_log: Any, task_id: str) -> list[str]:
    """The durable event-type sequence for a task — a structural recovery
    check: the recovered run must replay the same steps as a clean run."""
    return [env.type for env in event_log.read(task_id)]


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_lease_only_crash_recovers_byte_equal(kind: str, tmp_path: Any) -> None:
    path_clean = ":memory:" if kind == "memory" else str(tmp_path / "clean.sqlite")
    path_rec = ":memory:" if kind == "memory" else str(tmp_path / "rec.sqlite")

    # Clean run — no crash.
    b1 = _bundle(path_clean, _OneShotProvider())
    try:
        t1 = b1.engine.create_task(goal="g", policy_name="react")
        b1.dispatcher.enqueue(t1.task_id)
        loop1 = WorkerLoop(b1, heartbeat_interval=0, stale_sweep_interval=0)
        assert loop1.tick() is True
        clean = _event_types(b1.event_log, t1.task_id)
    finally:
        b1.shutdown()

    # Recovered run — worker A leases then "crashes" before any durable
    # step event; requeue_stale reclaims; worker B drives to completion.
    b2 = _bundle(path_rec, _OneShotProvider())
    try:
        t2 = b2.engine.create_task(goal="g", policy_name="react")
        b2.dispatcher.enqueue(t2.task_id)
        lease_a = b2.dispatcher.lease(worker_id="A", lease_seconds=0.001)
        assert lease_a is not None and lease_a.task_id == t2.task_id
        time.sleep(0.03)  # let the tiny lease deadline pass
        reclaimed = b2.dispatcher.requeue_stale()
        assert t2.task_id in reclaimed  # A's lease reclaimed (A wrote nothing)
        loop2 = WorkerLoop(b2, heartbeat_interval=0, stale_sweep_interval=0)
        assert loop2.tick() is True
        recovered = _event_types(b2.event_log, t2.task_id)
    finally:
        b2.shutdown()

    assert recovered == clean  # recovered run replays the same event sequence


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_poison_task_bounded_retry_then_terminal(
    kind: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step whose ``run_one_step`` RAISES (an uncaught policy/engine
    bug — provider errors are instead Engine-handled into TaskFailed) is
    `fail(retryable=True)`-ed by the worker. Driven against the REAL
    dispatcher, bounded retry caps at ``max_fail_attempts`` → the task is
    terminal (no longer leasable). Terminal is asserted from the
    **dispatcher**, the symptom only from the sink."""
    from noeta.testing.profile import open_storage_stack

    path = ":memory:" if kind == "memory" else str(tmp_path / "poison.sqlite")
    event_log, content_store, dispatcher = open_storage_stack(path)
    # fold is monkeypatched (we never write events); the step always raises.
    monkeypatch.setattr(worker_mod, "fold", lambda *a, **k: _task("running"))

    def _boom(task: Any, *, lease_id: str) -> Any:
        raise RuntimeError("poison step")

    rt = SimpleNamespace(
        engine=_FakeEngine(_boom),
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
    )
    sink = _CaptureSink()
    dispatcher.enqueue("poison-task")
    loop = WorkerLoop(
        rt, heartbeat_interval=0, stale_sweep_interval=0, reliability_sink=sink
    )
    attempts = 0
    while loop.tick():
        attempts += 1
        if attempts > 50:
            pytest.fail("poison task never stopped being leasable")
    assert any(e.kind == "step_failed_retryable" for e in sink.events)
    # terminal asserted from the dispatcher: the task is no longer leasable.
    assert dispatcher.lease(worker_id="x", lease_seconds=1.0) is None
