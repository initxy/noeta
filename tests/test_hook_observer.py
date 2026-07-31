"""HookObserver runs user commands off the emit path, exactly once per result.

A user hook is arbitrary code of unbounded duration, so it must never be able to
stall an EventLog write, wedge shutdown, or outlive the session: the queue drops
under pressure rather than applying backpressure, a raising runner is swallowed,
and ``stop()`` is bounded and cancels the in-flight command. The injectable
runner here returns cancellable fake handles, keeping every case free of real
subprocesses and real sleeps.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from noeta.builtins.governance.impl.hook_observer import HookObserver, NotificationRule, PostToolUseRule


class _FakeLog:
    """Captures the subscriber callback `subscribe_with_stop` registers."""

    def __init__(self) -> None:
        self.cb: Any = None

    def subscribe(self, callback: Any) -> Any:
        self.cb = callback
        return lambda: None  # StopHandle wraps this; .stop() calls it


class _ImmediateHandle:
    def __init__(self) -> None:
        self.cancelled = threading.Event()

    def wait(self) -> None:
        return None

    def cancel(self) -> None:
        self.cancelled.set()


class _BlockingHandle:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.started = threading.Event()

    def wait(self) -> None:
        self.started.set()
        self.cancelled.wait(10.0)  # block until cancel() (or safety timeout)

    def cancel(self) -> None:
        self.cancelled.set()


class _Runner:
    """Records argvs + the handles it created."""

    def __init__(self, factory: Any = _ImmediateHandle) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.handles: list[Any] = []
        self._factory = factory

    def __call__(self, argv: tuple[str, ...]) -> Any:
        self.calls.append(argv)
        h = self._factory()
        self.handles.append(h)
        return h


def _env(type_: str, **payload: Any) -> Any:
    return SimpleNamespace(type=type_, payload=SimpleNamespace(**payload))


def _observer(
    post: tuple[PostToolUseRule, ...] = (),
    notif: tuple[NotificationRule, ...] = (),
    runner: Any = None,
    max_queue: int = 256,
) -> tuple[HookObserver, _FakeLog]:
    log = _FakeLog()
    obs = HookObserver(
        event_log=log,
        post_tool_use=post,
        notification=notif,
        runner=runner if runner is not None else _Runner(),
        max_queue=max_queue,
    )
    return obs, log


def _wait_until(pred: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


def test_callback_does_not_block_on_slow_command() -> None:
    runner = _Runner(_BlockingHandle)
    obs, log = _observer(
        post=(PostToolUseRule(match_tool="*", command=("notify",)),), runner=runner
    )
    try:
        log.cb(_env("ToolCallStarted", call_id="c1", tool_name="write"))
        t0 = time.monotonic()
        log.cb(_env("ToolResultRecorded", call_id="c1", success=True))
        # The emit-path callback returned immediately despite the slow runner.
        assert time.monotonic() - t0 < 0.3
        assert _wait_until(lambda: runner.calls == [("notify",)])
    finally:
        obs.stop()


def test_post_tool_use_fires_once_on_result_not_finished() -> None:
    runner = _Runner()
    obs, log = _observer(
        post=(PostToolUseRule(match_tool="write", command=("n",)),), runner=runner
    )
    try:
        log.cb(_env("ToolCallStarted", call_id="c1", tool_name="write"))
        log.cb(_env("ToolResultRecorded", call_id="c1", success=True))
        # ToolCallFinished follows the result for the same call; firing on both
        # would run the user's hook twice per tool call.
        log.cb(_env("ToolCallFinished", call_id="c1"))
        assert _wait_until(lambda: len(runner.calls) == 1)
        time.sleep(0.1)
        assert runner.calls == [("n",)]  # still exactly once
    finally:
        obs.stop()


def test_post_tool_use_unmatched_tool_no_fire() -> None:
    runner = _Runner()
    obs, log = _observer(
        post=(PostToolUseRule(match_tool="read_file", command=("n",)),), runner=runner
    )
    try:
        log.cb(_env("ToolCallStarted", call_id="c1", tool_name="write"))
        log.cb(_env("ToolResultRecorded", call_id="c1", success=True))
        time.sleep(0.1)
        assert runner.calls == []
    finally:
        obs.stop()


def test_notification_fires_on_approval_only() -> None:
    runner = _Runner()
    obs, log = _observer(
        notif=(NotificationRule(on="approval", command=("notify",)),), runner=runner
    )
    try:
        log.cb(_env("ToolCallApprovalRequested", call_id="c1", tool_name="write"))
        assert _wait_until(lambda: runner.calls == [("notify",)])
        # a TaskSuspended must NOT double-fire the notification.
        log.cb(_env("TaskSuspended", reason="waiting_human"))
        time.sleep(0.1)
        assert runner.calls == [("notify",)]
    finally:
        obs.stop()


def test_full_queue_drops_without_blocking() -> None:
    runner = _Runner(_BlockingHandle)
    obs, log = _observer(
        post=(PostToolUseRule(match_tool="*", command=("n",)),),
        runner=runner,
        max_queue=1,
    )
    try:
        log.cb(_env("ToolCallStarted", call_id="c1", tool_name="t"))
        # Fire several; worker takes one (blocks), one queues, rest drop —
        # none of these callbacks should raise or block.
        t0 = time.monotonic()
        for _ in range(5):
            log.cb(_env("ToolResultRecorded", call_id="c1", success=True))
        assert time.monotonic() - t0 < 0.5
    finally:
        obs.stop()


def test_failing_runner_is_swallowed() -> None:
    def boom(argv: tuple[str, ...]) -> Any:
        raise RuntimeError("notify blew up")

    obs, log = _observer(
        post=(PostToolUseRule(match_tool="*", command=("n",)),), runner=boom
    )
    try:
        log.cb(_env("ToolCallStarted", call_id="c1", tool_name="t"))
        log.cb(_env("ToolResultRecorded", call_id="c1", success=True))
        time.sleep(0.1)  # worker raised internally; must not crash the test
        log.cb(_env("ToolResultRecorded", call_id="c1", success=True))
        time.sleep(0.05)
    finally:
        obs.stop()


def test_stop_cancels_in_flight_and_skips_queued() -> None:
    """``stop()`` cancels the in-flight command so no user hook outlives the
    session, and drains without running anything still queued."""
    runner = _Runner(_BlockingHandle)
    obs, log = _observer(
        post=(PostToolUseRule(match_tool="*", command=("n",)),),
        runner=runner,
        max_queue=8,
    )
    log.cb(_env("ToolCallStarted", call_id="c1", tool_name="t"))
    log.cb(_env("ToolResultRecorded", call_id="c1", success=True))  # job 1 → runs+blocks
    log.cb(_env("ToolResultRecorded", call_id="c1", success=True))  # job 2 → queued
    # wait until the worker has started the first (blocking) command
    assert _wait_until(lambda: runner.handles and runner.handles[0].started.is_set())
    t0 = time.monotonic()
    obs.stop()
    assert time.monotonic() - t0 < 3.0  # bounded
    # the in-flight handle was cancelled (not left running)
    assert runner.handles[0].cancelled.is_set()
    # the queued job 2 was NOT run after stop
    time.sleep(0.2)
    assert len(runner.calls) == 1
    assert not obs._worker.is_alive()  # worker thread stopped


def test_stop_is_bounded_with_blocking_runner() -> None:
    runner = _Runner(_BlockingHandle)
    obs, log = _observer(
        post=(PostToolUseRule(match_tool="*", command=("n",)),), runner=runner
    )
    log.cb(_env("ToolCallStarted", call_id="c1", tool_name="t"))
    log.cb(_env("ToolResultRecorded", call_id="c1", success=True))
    assert _wait_until(lambda: runner.handles and runner.handles[0].started.is_set())
    t0 = time.monotonic()
    obs.stop()  # must return bounded even though the runner is stuck
    assert time.monotonic() - t0 < 3.0


def test_log_only_rule_no_command() -> None:
    logs: list[str] = []
    runner = _Runner()
    obs, log = _observer(
        post=(PostToolUseRule(match_tool="*", log=True),), runner=runner
    )
    obs._log_sink = lambda msg: logs.append(msg)
    try:
        log.cb(_env("ToolCallStarted", call_id="c1", tool_name="t"))
        log.cb(_env("ToolResultRecorded", call_id="c1", success=True))
        time.sleep(0.1)
        assert any("post_tool_use" in m for m in logs)
        assert runner.calls == []  # no command ⇒ runner never called
    finally:
        obs.stop()
