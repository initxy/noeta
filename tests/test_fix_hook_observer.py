"""Regression: ``HookObserver._call_names`` must not grow unbounded.

The dict only ever holds in-flight tool calls: a ``ToolCallStarted``
records ``call_id -> tool_name``, and the matching ``ToolResultRecorded``
**evicts** that entry (under ``_names_lock``). Without eviction the dict
retained one entry per tool call for the whole session lifetime (and all
its subtasks) — a steady, unbounded memory leak.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from noeta.observers.hook import HookObserver, PostToolUseRule


class _FakeLog:
    def __init__(self) -> None:
        self.cb: Any = None

    def subscribe(self, callback: Any) -> Any:
        self.cb = callback
        return lambda: None


class _ImmediateHandle:
    def wait(self) -> None:
        return None

    def cancel(self) -> None:
        return None


def _runner(_argv: tuple[str, ...]) -> Any:
    return _ImmediateHandle()


def _env(type_: str, **payload: Any) -> Any:
    return SimpleNamespace(type=type_, payload=SimpleNamespace(**payload))


def _observer() -> tuple[HookObserver, _FakeLog]:
    log = _FakeLog()
    obs = HookObserver(
        event_log=log,
        post_tool_use=(PostToolUseRule(match_tool="*"),),
        notification=(),
        runner=_runner,
    )
    return obs, log


def test_call_names_evicted_on_result() -> None:
    obs, log = _observer()
    try:
        for i in range(50):
            cid = f"call-{i}"
            log.cb(_env("ToolCallStarted", call_id=cid, tool_name="read"))
            # In-flight: the entry is present until the result arrives.
            assert cid in obs._call_names
            log.cb(_env("ToolResultRecorded", call_id=cid))
        # Every finished call is evicted — nothing accumulates.
        assert obs._call_names == {}
    finally:
        obs.stop()


def test_result_without_start_is_noop() -> None:
    obs, log = _observer()
    try:
        # A stray result for an unknown call_id must not raise or leak.
        log.cb(_env("ToolResultRecorded", call_id="never-started"))
        assert obs._call_names == {}
    finally:
        obs.stop()


def test_names_lock_present_and_guards_dict() -> None:
    # The contract fix: _call_names is guarded by its own lock, like the
    # sibling Audit/Metrics observers.
    obs, _ = _observer()
    try:
        # A real lock acquired/released as a context manager.
        assert obs._names_lock is not None
        with obs._names_lock:
            pass
    finally:
        obs.stop()
