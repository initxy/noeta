"""The lease heartbeat survives storage hiccups, and its cap bounds one step
attempt — not the turn — and ends in recovery rather than a failed or
stranded task.
"""

from __future__ import annotations

import functools
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from noeta.protocols.errors import InvalidLease
from noeta.protocols.messages import ToolUseBlock
from noeta.runtime import worker as worker_mod
from noeta.runtime.worker import _HeartbeatRunner, keep_lease_alive, lease_lost
from noeta.sdk import Client, HostConfig, LLMResponse, Options, TextBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider


class _Dispatcher:
    def __init__(self, fail_on: tuple[int, ...] = (), invalid_on: int = 0) -> None:
        self.calls = 0
        self.yielded: list[str] = []
        self._fail_on = fail_on
        self._invalid_on = invalid_on

    def heartbeat(self, lease_id: str, *, lease_seconds: float) -> float:
        self.calls += 1
        if self.calls == self._invalid_on:
            raise InvalidLease(lease_id)
        if self.calls in self._fail_on:
            raise sqlite3.OperationalError("database is locked")
        return 0.0

    def release_yield(self, lease_id: str) -> None:
        self.yielded.append(lease_id)


class _Log:
    """An event log reduced to the subscription the runner listens on."""

    def __init__(self) -> None:
        self.callbacks: list[Callable[[Any], None]] = []

    def subscribe(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        self.callbacks.append(callback)
        return lambda: self.callbacks.remove(callback)

    def plan(self, task_id: str) -> None:
        for cb in list(self.callbacks):
            cb(SimpleNamespace(type="ContextPlanComposed", task_id=task_id))


def _lease(lease_id: str = "L1") -> Any:
    return SimpleNamespace(lease_id=lease_id, task_id="t1")


def _scripted(n: int, on_tick: Callable[[int], None] = lambda _i: None) -> Any:
    ticks = {"i": 0}

    def wait(_delay: float) -> bool:
        ticks["i"] += 1
        if ticks["i"] > n:
            return True
        on_tick(ticks["i"])
        return False

    return wait


# -- A3: transient errors ----------------------------------------------------


def test_heartbeat_keeps_going_after_a_transient_error() -> None:
    disp = _Dispatcher(fail_on=(2,))
    runner = _HeartbeatRunner(
        disp, _lease(), interval=0.05, lease_seconds=600, attempt_heartbeat_max=None
    )
    runner.start()
    deadline = time.monotonic() + 5
    while disp.calls < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    alive = runner._thread is not None and runner._thread.is_alive()
    runner.stop()
    assert disp.calls >= 5
    assert alive


def test_invalid_lease_marks_the_lease_lost_until_the_runner_stops() -> None:
    disp = _Dispatcher(invalid_on=2)
    runner = _HeartbeatRunner(
        disp, _lease("L-lost"), interval=1.0, lease_seconds=600,
        wait=_scripted(5),
    )
    runner._loop()
    assert disp.calls == 2
    assert lease_lost("L-lost")
    runner.stop()
    assert not lease_lost("L-lost")


def test_the_step_poll_trips_on_a_lost_lease() -> None:
    rt = SimpleNamespace(is_cancelled=lambda _tid: False)
    poll = worker_mod._cancel_predicate(rt, "t1", lease_id="L-poll")
    assert poll is not None and poll() is False
    worker_mod._LOST_LEASES.add("L-poll")
    try:
        assert poll() is True
    finally:
        worker_mod._LOST_LEASES.discard("L-poll")


# -- A4: the cap counts one attempt ------------------------------------------


def test_cap_restarts_with_every_model_call() -> None:
    disp = _Dispatcher()
    log = _Log()
    # A new attempt starts every second heartbeat: a long, healthy turn.
    runner = _HeartbeatRunner(
        disp, _lease(), interval=1.0, lease_seconds=600, event_log=log,
        attempt_heartbeat_max=3,
        wait=_scripted(20, lambda i: log.plan("t1") if i % 2 == 0 else None),
    )
    runner.start()
    runner._thread.join(5)  # type: ignore[union-attr]
    runner.stop()
    assert disp.calls == 20
    assert disp.yielded == []


def test_cap_hands_the_lease_back_and_stops_the_step() -> None:
    disp = _Dispatcher()
    runner = _HeartbeatRunner(
        disp, _lease("L-cap"), interval=1.0, lease_seconds=600, event_log=_Log(),
        attempt_heartbeat_max=3, wait=_scripted(20),
    )
    runner._loop()
    try:
        assert disp.calls == 3
        assert disp.yielded == ["L-cap"]
        assert lease_lost("L-cap")
    finally:
        runner.stop()


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_host_config_rejects_a_bad_cap(bad: Any) -> None:
    with pytest.raises(ValueError, match="attempt_heartbeat_max"):
        HostConfig(attempt_heartbeat_max=bad)


def test_host_config_accepts_no_cap() -> None:
    assert HostConfig(attempt_heartbeat_max=None).attempt_heartbeat_max is None


def test_capped_attempt_is_recovered_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-request turn whose single attempt outruns the cap: the lease goes
    back, the step stops, and the drive recovers the attempt like a crash —
    the task ends parked for the next goal with no ``TaskFailed``, and the
    dispatcher row agrees with the fold."""
    monkeypatch.setattr(
        "noeta.execution.driver.keep_lease_alive",
        functools.partial(keep_lease_alive, interval=0.05),
    )
    calls: list[int] = []
    lock = threading.Lock()

    def respond(_req: Any) -> LLMResponse:
        with lock:
            calls.append(1)
            n = len(calls)
        if n == 1:
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="slow", tool_name="Bash",
                        arguments={"command": "sleep 1"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
                raw={"id": "1"},
            )
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
            raw={"id": str(n)},
        )

    client = Client(
        Options(system_prompt="x", permission_mode="bypassPermissions"),
        provider=FakeLLMProvider(responder=respond),
        workspace_dir=tmp_path,
        model="sonnet",
        host_config=HostConfig(
            storage_path=str(tmp_path / "s.sqlite"), attempt_heartbeat_max=3
        ),
    )
    try:
        out = client.start(goal="go")
        types = [e.type for e in client.events(out.task_id)]
        assert "TaskFailed" not in types
        # Recovery keeps the recorded batch: the capped call is not re-run.
        started = [
            e for e in client.events(out.task_id) if e.type == "ToolCallStarted"
        ]
        assert len(started) == 1
        assert out.status == "suspended"
        disp = client._host.dispatcher  # noqa: SLF001 - row vs fold is the point
        assert disp.task_status(out.task_id) == "suspended"
    finally:
        client.shutdown()
