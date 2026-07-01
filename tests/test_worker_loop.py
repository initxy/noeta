"""3A-I1 — L2 worker loop + 3-state machine sink + exception policy.

Covers `noeta.runtime.worker`: `run_leased_task` (the 3-state machine
moved down from CLI resume), `WorkerLoop.tick` / `run_forever`, and the
worker exception policy (D7) — a daemon must never crash on a poisoned
task.

CLI byte-identical reuse is covered by the existing resume tests
(test_cli_commands / test_cli_resume_targeted), which now import
`run_leased_task` from L2.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.testing.profile import (
    build_runtime,
    build_tools,
    default_budget,
    default_permission_policy,
)
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, SpawnSubtaskDecision
from noeta.protocols.errors import InvalidLease
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.runtime.worker import WorkerLoop, run_leased_task


SYSTEM_PROMPT = "You are a helpful assistant."


class _StubProvider:
    def complete(self, request: LLMRequest) -> LLMResponse:
        has_tool_result = any(
            isinstance(b, ToolResultBlock)
            for msg in request.messages
            for b in (msg.content or [])
        )
        if not has_tool_result:
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="c-1", tool_name="echo",
                        arguments={"text": "hello"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
            )
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
        )


def _build_bundle(sqlite_path: str = ":memory:") -> Any:
    return build_runtime(
        provider=_StubProvider(),
        model="stub-model",
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        sqlite_path=sqlite_path,
        sse_broadcaster=None,
        max_steps=5,
        permission_policy=default_permission_policy(),
        budget=default_budget(),
    )


# ---------------------------------------------------------------------------
# WorkerRuntime structural Protocol
# ---------------------------------------------------------------------------


def test_runtime_bundle_satisfies_worker_runtime_protocol() -> None:
    """RuntimeBundle (L4) must satisfy the L2 WorkerRuntime Protocol
    structurally — without L2 importing the bundle type."""
    bundle = _build_bundle()
    try:
        for attr in ("engine", "event_log", "content_store", "dispatcher"):
            assert hasattr(bundle, attr), f"bundle missing {attr}"
    finally:
        bundle.shutdown()


# ---------------------------------------------------------------------------
# run_leased_task — the 3-state machine
# ---------------------------------------------------------------------------


def test_run_leased_task_drains_pending_task() -> None:
    bundle = _build_bundle()
    try:
        task = bundle.engine.create_task(goal="x", policy_name="react")
        bundle.dispatcher.enqueue(task.task_id)
        lease = bundle.dispatcher.lease(worker_id="w", lease_seconds=60.0)
        assert lease is not None
        outcome = run_leased_task(bundle, lease)
        assert outcome == "drained"
    finally:
        bundle.shutdown()


def test_run_leased_task_skips_suspended_without_wake() -> None:
    from noeta.guards.permission import PermissionPolicy

    perm = PermissionPolicy(
        allowed_tools=frozenset({"echo"}),
        denied_tools=frozenset(),
        max_risk_level=None,
        allowed_subtask_agents=frozenset({"helper"}),
    )
    bundle = build_runtime(
        provider=_StubProvider(),
        model="stub-model",
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        sqlite_path=":memory:",
        sse_broadcaster=None,
        max_steps=5,
        permission_policy=perm,
        budget=default_budget(),
    )
    try:
        bundle.engine._policy = StubScriptedPolicy(
            [SpawnSubtaskDecision(agent_name="helper", goal="sub"),
             FinishDecision(answer="done")]
        )
        task = bundle.engine.create_task(goal="parent", policy_name="stub")
        bundle.dispatcher.enqueue(task.task_id)
        lease = bundle.dispatcher.lease(worker_id="w", lease_seconds=60.0)
        assert lease is not None
        result = bundle.engine.run_one_step(task, lease_id=lease.lease_id)
        bundle.dispatcher.release(
            lease.lease_id, next_state=result.status, wake_on=result.wake_on
        )
        assert result.status == "suspended"
        # Force back onto ready WITHOUT a wake_event (lost-wake shape).
        # Lease the PARENT specifically — the child the parent spawned is
        # also on the ready queue, and an untargeted lease would pick it.
        bundle.dispatcher.enqueue(task.task_id)
        lease2 = bundle.dispatcher.lease(
            worker_id="w", lease_seconds=60.0, task_id=task.task_id
        )
        assert lease2 is not None and lease2.wake_event is None
        outcome = run_leased_task(bundle, lease2)
        assert outcome == "skipped"
    finally:
        bundle.shutdown()


# ---------------------------------------------------------------------------
# WorkerLoop — continuous drain
# ---------------------------------------------------------------------------


def test_worker_loop_tick_returns_false_on_empty_queue() -> None:
    bundle = _build_bundle()
    try:
        loop = WorkerLoop(bundle, worker_id="w")
        assert loop.tick() is False  # nothing enqueued
    finally:
        bundle.shutdown()


def test_worker_loop_drains_multiple_tasks_continuously() -> None:
    bundle = _build_bundle()
    try:
        ids = []
        for i in range(3):
            t = bundle.engine.create_task(goal=f"g{i}", policy_name="react")
            bundle.dispatcher.enqueue(t.task_id)
            ids.append(t.task_id)
        loop = WorkerLoop(bundle, worker_id="w")
        processed = 0
        while loop.tick():
            processed += 1
        assert processed == 3
        # All three reached terminal.
        for tid in ids:
            folded = __import__(
                "noeta.core.fold", fromlist=["fold"]
            ).fold(bundle.event_log, bundle.content_store, tid)
            assert folded.status == "terminal"
    finally:
        bundle.shutdown()


def test_worker_loop_run_forever_stops_via_injected_sleep() -> None:
    """run_forever loops until stop(); an injected sleep flips the flag
    so the test never wall-clock waits or loops forever."""
    bundle = _build_bundle()
    try:
        t = bundle.engine.create_task(goal="g", policy_name="react")
        bundle.dispatcher.enqueue(t.task_id)

        loop = WorkerLoop(bundle, worker_id="w", poll_interval=0.01)

        calls = {"n": 0}

        def fake_sleep(_seconds: float) -> None:
            # Called when the queue is empty; stop after the first idle.
            calls["n"] += 1
            loop.stop()

        loop._sleep = fake_sleep
        loop.run_forever()
        # The task was drained before the first idle-sleep stop.
        assert calls["n"] == 1
        folded = __import__(
            "noeta.core.fold", fromlist=["fold"]
        ).fold(bundle.event_log, bundle.content_store, t.task_id)
        assert folded.status == "terminal"
    finally:
        bundle.shutdown()


# ---------------------------------------------------------------------------
# Worker exception policy (D7)
# ---------------------------------------------------------------------------


class _FakeLease:
    def __init__(self, task_id: str = "t-fake", lease_id: str = "L-fake") -> None:
        self.task_id = task_id
        self.lease_id = lease_id
        self.wake_event = None


class _RaisingDispatcher:
    """Dispatcher stub: lease hands out one fake lease, then None;
    records fail() calls; can be told to raise on fail()."""

    def __init__(self, *, fail_raises: bool = False) -> None:
        self._handed = False
        self.fail_calls: list[dict[str, Any]] = []
        self._fail_raises = fail_raises

    def lease(self, *, worker_id: str, lease_seconds: float = 30.0,
              task_id: Any = None) -> Any:
        if self._handed:
            return None
        self._handed = True
        return _FakeLease()

    def fail(self, lease_id: str, *, retryable: bool = False,
             reason: Any = None) -> None:
        self.fail_calls.append(
            {"lease_id": lease_id, "retryable": retryable, "reason": reason}
        )
        if self._fail_raises:
            raise RuntimeError("fail() itself blew up")


class _BoomRuntime:
    """WorkerRuntime whose step raises a chosen exception."""

    def __init__(self, dispatcher: Any, exc: Exception) -> None:
        self.dispatcher = dispatcher
        self._exc = exc
        self.event_log = self  # fold is never reached — engine raises first
        self.content_store = self

        class _Engine:
            def __init__(self, exc: Exception) -> None:
                self._exc = exc

            def run_one_step(self, *a: Any, **k: Any) -> Any:
                raise self._exc

            def note_woken(self, *a: Any, **k: Any) -> Any:
                raise self._exc

        self.engine = _Engine(exc)

    # Minimal fold inputs — fold reads event_log.read(task_id) +
    # find_latest_snapshot; return a pending task so run_leased_task
    # proceeds to run_one_step (which booms).
    def read(self, task_id: str, *, after_seq: Any = None) -> list[Any]:
        from noeta.protocols.events import EventEnvelope, TaskCreatedPayload

        env = EventEnvelope.build(
            task_id=task_id, type="TaskCreated",
            payload=TaskCreatedPayload(goal="x", policy_name="react"),
            id="evt-1", actor="test", trace_id=None, causation_id=None,
            schema_version=1, occurred_at=0.0, origin="engine",
        ).with_seq(0)
        return [env]

    def find_latest_snapshot(self, task_id: str) -> Any:
        return None


def test_exception_policy_unexpected_error_fails_retryable_and_continues() -> None:
    disp = _RaisingDispatcher()
    rt = _BoomRuntime(disp, ValueError("policy blew up"))
    loop = WorkerLoop(rt, worker_id="w", heartbeat_interval=0)

    # tick() must NOT propagate the exception; it fails the lease retryable.
    assert loop.tick() is True
    assert len(disp.fail_calls) == 1
    assert disp.fail_calls[0]["retryable"] is True
    assert "policy blew up" in str(disp.fail_calls[0]["reason"])
    # Queue now empty → next tick False, loop survived.
    assert loop.tick() is False


def test_exception_policy_invalid_lease_does_not_fail_or_release() -> None:
    disp = _RaisingDispatcher()
    rt = _BoomRuntime(disp, InvalidLease("lease gone"))
    loop = WorkerLoop(rt, worker_id="w", heartbeat_interval=0)

    # InvalidLease → log + continue; NO fail() / release() attempt.
    assert loop.tick() is True
    assert disp.fail_calls == []  # did not try to fail an un-owned lease
    assert loop.tick() is False  # loop survived


def test_exception_policy_fail_itself_raising_is_swallowed() -> None:
    disp = _RaisingDispatcher(fail_raises=True)
    rt = _BoomRuntime(disp, ValueError("boom"))
    loop = WorkerLoop(rt, worker_id="w", heartbeat_interval=0)

    # fail() raises, but the loop still must not crash.
    assert loop.tick() is True
    assert len(disp.fail_calls) == 1
    assert loop.tick() is False


# ---------------------------------------------------------------------------
# 3A-I2 — heartbeat side-thread + stale-sweep timer (injected clock)
# ---------------------------------------------------------------------------


class _CountingHeartbeatDispatcher:
    """Records heartbeat calls; optionally raises InvalidLease."""

    def __init__(self, *, raise_invalid: bool = False) -> None:
        self.heartbeats: list[str] = []
        self._raise = raise_invalid

    def heartbeat(self, lease_id: str, *, lease_seconds: float = 30.0) -> float:
        self.heartbeats.append(lease_id)
        if self._raise:
            raise InvalidLease(lease_id)
        return 0.0


def _make_lease(task_id: str = "t1", lease_id: str = "L1") -> Any:
    class _L:
        pass

    lease = _L()
    lease.task_id = task_id  # type: ignore[attr-defined]
    lease.lease_id = lease_id  # type: ignore[attr-defined]
    lease.wake_event = None  # type: ignore[attr-defined]
    return lease


def test_heartbeat_runner_beats_then_stops_with_injected_wait() -> None:
    """Injected `wait` scripts exactly one heartbeat then stop — no real
    sleep, no thread timing dependence."""
    from noeta.runtime.worker import _HeartbeatRunner

    disp = _CountingHeartbeatDispatcher()
    waits = iter([False, True])  # one timeout (→beat), then stop

    runner = _HeartbeatRunner(
        disp, _make_lease(), interval=1.0, lease_seconds=30.0,
        wait=lambda _i: next(waits),
    )
    runner._loop()  # drive synchronously
    assert disp.heartbeats == ["L1"]


def test_heartbeat_runner_stops_on_invalid_lease() -> None:
    """When heartbeat raises InvalidLease the runner stops (logs) and
    does not loop forever / crash."""
    from noeta.runtime.worker import _HeartbeatRunner

    disp = _CountingHeartbeatDispatcher(raise_invalid=True)
    waits = iter([False, False, True])  # would beat twice, but 1st raises

    runner = _HeartbeatRunner(
        disp, _make_lease(), interval=1.0, lease_seconds=30.0,
        wait=lambda _i: next(waits),
    )
    runner._loop()
    assert disp.heartbeats == ["L1"]  # stopped after the InvalidLease


def test_heartbeat_thread_starts_and_joins_cleanly() -> None:
    """start()/stop() over a real (short) thread leaves nothing leaked."""
    import threading as _t
    from noeta.runtime.worker import _HeartbeatRunner

    disp = _CountingHeartbeatDispatcher()
    runner = _HeartbeatRunner(
        disp, _make_lease(), interval=30.0, lease_seconds=30.0
    )
    before = _t.active_count()
    runner.start()
    runner.stop()  # Event interrupts the 30s wait immediately
    assert _t.active_count() == before  # joined, no leak


def test_maybe_sweep_runs_on_interval_with_injected_clock() -> None:
    """requeue_stale fires only after stale_sweep_interval elapses, by
    the injected clock."""

    class _SweepDispatcher:
        def __init__(self) -> None:
            self.sweeps = 0

        def requeue_stale(self) -> list[str]:
            self.sweeps += 1
            return []

    class _RT:
        def __init__(self, dispatcher: Any) -> None:
            self.dispatcher = dispatcher
            self.engine = None
            self.event_log = None
            self.content_store = None

    now = {"t": 100.0}
    disp = _SweepDispatcher()
    loop = WorkerLoop(
        _RT(disp),
        worker_id="w",
        stale_sweep_interval=10.0,
        clock=lambda: now["t"],
        heartbeat_interval=0,
    )
    # Not enough time elapsed yet.
    assert loop.maybe_sweep() is False
    assert disp.sweeps == 0
    # Jump past the interval.
    now["t"] = 111.0
    assert loop.maybe_sweep() is True
    assert disp.sweeps == 1
    # Immediately after, the window resets.
    assert loop.maybe_sweep() is False
    assert disp.sweeps == 1


def test_worker_loop_heartbeat_keeps_slow_step_lease_valid() -> None:
    """Integration: a step slower than lease_seconds keeps its lease via
    the heartbeat side-thread, so its EventLog writes stay valid."""
    bundle = _build_bundle()
    try:
        # Short lease so a real heartbeat is needed; the in-memory
        # dispatcher's heartbeat extends it. Inject a heartbeat wait that
        # beats once quickly then idles until stop.
        import threading as _t

        first = _t.Event()

        def hb_wait(_interval: float) -> bool:
            if not first.is_set():
                first.set()
                return False  # timeout → one heartbeat
            return True  # then stop

        t = bundle.engine.create_task(goal="g", policy_name="react")
        bundle.dispatcher.enqueue(t.task_id)
        loop = WorkerLoop(
            bundle, worker_id="w", lease_seconds=60.0,
            heartbeat_interval=0.01, heartbeat_wait=hb_wait,
        )
        assert loop.tick() is True
        folded = __import__(
            "noeta.core.fold", fromlist=["fold"]
        ).fold(bundle.event_log, bundle.content_store, t.task_id)
        assert folded.status == "terminal"
    finally:
        bundle.shutdown()
