"""``Monitor``: a script's stdout lines become notifications, as in Claude Code.

The event stream (batching, line and batch caps, pacing, flood stop) is pinned
on :class:`MonitorStream` with a fake clock; the registry runs a real script
and hands its notices on; the tool gates the script like ``Bash`` and answers
with Claude Code's launch text; and an event reaches a running turn between
tool calls like any background notice.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from noeta.builtins.fs.impl import MonitorTool, ShellRunTool
from noeta.builtins.fs.impl.shell import _monitor_timeout_ms
from noeta.client.host import _make_shell_approval_predicate
from noeta.protocols.tool import ToolContext
from noeta.runtime.background_shell import ProcessRegistry
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.task_notification import (
    BATCH_MAX_CHARS,
    LINE_MAX_CHARS,
    MonitorStream,
    format_duration,
    monitor_end_summary,
    monitor_event,
    monitor_expired_note,
)
from noeta.runtime.workspace import WorkspaceRoot
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


# -- the event stream ------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _stream(clock: Any = None) -> tuple[MonitorStream, list[tuple[str, bool]], list[int]]:
    sunk: list[tuple[str, bool]] = []
    stops: list[int] = []
    stream = MonitorStream(
        sink=lambda text, note: sunk.append((text, note)),
        stop=lambda: stops.append(1),
        clock=clock or _Clock(),
    )
    return stream, sunk, stops


def test_lines_within_the_window_share_one_event() -> None:
    stream, sunk, _ = _stream()
    stream.feed("first\n\n  second  \nthird")
    time.sleep(0.4)
    assert sunk == [("first\nsecond", False)]
    # The unfinished line rides the end of the stream.
    assert stream.drain() == "third"
    assert stream.delivered == 2


def test_long_lines_and_batches_are_cut_like_claude_code() -> None:
    stream, sunk, _ = _stream()
    stream.feed("x" * (LINE_MAX_CHARS + 10) + "\n")
    stream.flush()
    assert sunk[-1][0] == "x" * LINE_MAX_CHARS + "...(truncated)"
    stream.feed("".join(f"{n:04d}" + "y" * 400 + "\n" for n in range(10)))
    stream.flush()
    batch = sunk[-1][0]
    assert batch.endswith("\n...(truncated)")
    assert len(batch) == BATCH_MAX_CHARS + len("\n...(truncated)")


def test_a_flood_is_paced_then_reported_then_stopped() -> None:
    clock = _Clock()
    stream, sunk, stops = _stream(clock)
    for n in range(10):  # the bucket's ten tokens
        stream.feed(f"e{n}\n")
        stream.flush()
    stream.feed("over\n")
    stream.flush()
    assert [t for t, _ in sunk] == [f"e{n}" for n in range(10)]
    clock.now = 2.0  # one token back
    stream.feed("back\n")
    stream.flush()
    assert sunk[-2] == (
        "[1 events suppressed — output rate too high. Consider using KillShell "
        "to restart this monitor with a more selective filter.]",
        True,
    )
    assert sunk[-1] == ("back", False)
    # Suppressed without a break for over 30 s: the monitor is stopped.
    stream.feed("more\n")
    stream.flush()
    clock.now = 33.0
    stream._tokens = 0  # noqa: SLF001 — keep the bucket dry at the deadline
    stream._refilled_at = clock.now  # noqa: SLF001
    stream.feed("flood\n")
    stream.flush()
    assert sunk[-1][1] is True
    assert sunk[-1][0].startswith("[Monitor stopped — too much output (2 events suppressed over ")
    assert sunk[-1][0].endswith("s). Restart with a more selective source.]")
    assert stops == [1]
    stream.feed("after\n")
    stream.flush()
    assert sunk[-1][0] != "after"


def test_claude_code_texts() -> None:
    assert format_duration(300_000) == "5m"
    assert format_duration(90_000) == "1m 30s"
    assert format_duration(1_800_000) == "30m"
    assert format_duration(3_600_000) == "1h"
    assert format_duration(5_000) == "5s"
    assert monitor_expired_note(300_000, 0) == (
        "[Monitor expired after 5m with no events delivered. Re-arm it if you "
        "still need the watch — and widen the filter if silence was unexpected.]"
    )
    assert monitor_expired_note(60_000, 1) == (
        "[Monitor expired after 1m with 1 event delivered. Re-arm it if you "
        "still need the watch.]"
    )
    assert monitor_end_summary("d", "completed", 0, produced_output=True) == (
        'Monitor "d" stream ended'
    )
    assert monitor_end_summary("d", "completed", 0, produced_output=False) == (
        'Monitor "d" ended without producing output (exit 0)'
    )
    assert monitor_end_summary("d", "failed", 2, produced_output=True) == (
        'Monitor "d" script failed (exit 2)'
    )
    assert monitor_end_summary("d", "killed", None, produced_output=True) == (
        'Monitor "d" stopped'
    )
    assert monitor_event(task_id="bg-1", description="errors", text="a<b") == (
        "<task-notification>\n<task-id>bg-1</task-id>\n"
        '<summary>Monitor event: "errors"</summary>\n'
        "<event>a&lt;b</event>\n</task-notification>"
    )


# -- the registry runs a real script ------------------------------------------


def _registry() -> tuple[ProcessRegistry, list[str]]:
    notices: list[str] = []
    registry = ProcessRegistry(
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
        on_notice=lambda _root, _job, notice: notices.append(notice),
    )
    return registry, notices


def _spawn(registry: ProcessRegistry, tmp_path: Path, code: str, timeout_s: float) -> str:
    out = registry.spawn(
        argv=[sys.executable, "-u", "-c", code],
        cwd=tmp_path,
        env={},
        command="python watch",
        spawned_by_task_id="t",
        trace_id="tr",
        description="test events",
        tool_use_id="toolu_m",
        monitor_timeout_s=timeout_s,
    )
    return str(out["job_id"])


def _until(predicate: Any, timeout_s: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_each_stdout_batch_is_an_event_and_exit_ends_the_watch(tmp_path: Path) -> None:
    registry, notices = _registry()
    code = (
        "import sys, time\n"
        "print('ready'); print('port 8080')\n"
        "time.sleep(0.5)\n"
        "print('ERROR boom')\n"
        "sys.stderr.write('trace line\\n')\n"
        "sys.exit(3)\n"
    )
    job = _spawn(registry, tmp_path, code, 30)
    _until(lambda: any("<status>" in n for n in notices))
    events = [n for n in notices if "<status>" not in n]
    assert events[0] == monitor_event(
        task_id=job, description="test events", text="ready\nport 8080"
    )
    end = notices[-1]
    assert f"<task-id>{job}</task-id>\n<tool-use-id>toolu_m</tool-use-id>" in end
    assert "<status>failed</status>" in end
    assert '<summary>Monitor "test events" script failed (exit 3)</summary>' in end
    # The last event either rode its own batch or the end notice; stderr's
    # tail rides only the end notice.
    assert "ERROR boom" in "".join(notices)
    assert "trace line" in end
    assert all("trace line" not in n for n in events)


def test_expiry_sends_one_notice_with_the_event_count(tmp_path: Path) -> None:
    registry, notices = _registry()
    code = "import time\nprint('tick')\nwhile True: time.sleep(0.1)\n"
    job = _spawn(registry, tmp_path, code, 1.0)
    _until(lambda: registry.poll(job)["status"] == "killed")
    time.sleep(0.2)
    assert len(notices) == 2
    assert "<event>tick</event>" in notices[0]
    assert "<event>[Monitor expired after 1s with 1 event delivered. Re-arm it if you still need the watch.]</event>" in notices[1]


def test_kill_ends_the_watch_with_a_stopped_notice(tmp_path: Path) -> None:
    registry, notices = _registry()
    code = "import time\nwhile True: time.sleep(0.1)\n"
    job = _spawn(registry, tmp_path, code, 30)
    registry.kill(job)
    _until(lambda: bool(notices))
    assert '<status>killed</status>\n<summary>Monitor "test events" stopped</summary>' in notices[-1]


# -- the tool ---------------------------------------------------------------------


def _ctx(registry: Any) -> ToolContext:
    return ToolContext(
        artifact_store=InMemoryContentStore(),
        background_runner=registry,
        metadata={"task_id": "t", "tool_use_id": "toolu_x"},
    )


def _monitor(tmp_path: Path, **kwargs: Any) -> MonitorTool:
    mode = kwargs.pop("mode", ShellMode.ARBITRARY)
    return MonitorTool(
        shell=ShellRunTool(
            workspace=WorkspaceRoot.from_path(tmp_path), mode=mode, **kwargs
        )
    )


def test_the_launch_answers_like_claude_code(tmp_path: Path) -> None:
    registry, _ = _registry()
    result = _monitor(tmp_path).invoke(
        {"command": "sleep 5", "description": "nothing", "timeout_ms": 300000},
        _ctx(registry),
    )
    assert result.success
    job = str(result.output).split("task ")[1].split(",")[0]
    assert result.output == (
        f"Monitor started (task {job}, expires in 5m unless the source ends "
        "first; you get one notice at expiry — re-arm if you still need the "
        "watch). You will be notified on each event. Keep working — do not poll "
        "or sleep. Events may arrive while you are waiting for the user — an "
        "event is not their reply."
    )
    registry.kill_root_task("t")


def test_a_host_ending_jobs_with_the_turn_says_so(tmp_path: Path) -> None:
    registry, _ = _registry()
    result = _monitor(tmp_path, background_ends_with_turn=True).invoke(
        {"command": "sleep 5", "description": "nothing", "timeout_ms": 1000},
        _ctx(registry),
    )
    assert "expires in 1s" in str(result.output)
    assert str(result.output).endswith(
        "it is terminated when you give your final response, and nothing can "
        "follow that."
    )
    registry.kill_root_task("t")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 300_000), ("5", 300_000), (True, 300_000), (10, 1_000),
     (60_000, 60_000), (3_600_000, 1_800_000), (9_999_999, 1_800_000)],
)
def test_timeout_ms_is_defaulted_bounded_and_capped(raw: Any, expected: float) -> None:
    assert _monitor_timeout_ms(raw) == expected


def test_refusals(tmp_path: Path) -> None:
    registry, _ = _registry()
    tool = _monitor(tmp_path)
    assert not tool.invoke({"description": "d", "timeout_ms": 1000}, _ctx(registry)).success
    assert not tool.invoke({"command": "true", "timeout_ms": 1000}, _ctx(registry)).success
    assert "not available" in tool.invoke(
        {"command": "true", "description": "d", "timeout_ms": 1000}, _ctx(None)
    ).summary
    gated = _monitor(tmp_path, mode=ShellMode.ALLOWLIST).invoke(
        {"command": "tail -f x | grep y", "description": "d", "timeout_ms": 1000},
        _ctx(registry),
    )
    assert gated.summary.startswith("Monitor: shell metacharacters")
    off = _monitor(tmp_path, mode=ShellMode.OFF).invoke(
        {"command": "true", "description": "d", "timeout_ms": 1000}, _ctx(registry)
    )
    assert not off.success

    class _Sandbox:
        supports_background = False

    sandboxed = MonitorTool(
        shell=ShellRunTool(
            workspace=WorkspaceRoot.from_path(tmp_path),
            mode=ShellMode.ARBITRARY,
            exec_env=_Sandbox(),  # type: ignore[arg-type]
        )
    )
    assert "sandbox" in sandboxed.invoke(
        {"command": "true", "description": "d", "timeout_ms": 1000}, _ctx(registry)
    ).summary


def test_the_shell_allowlist_gates_monitor_like_bash() -> None:
    from noeta.builtins.fs.impl.shell_rules import DEFAULT_SHELL_RULES

    needs = _make_shell_approval_predicate(DEFAULT_SHELL_RULES)
    assert needs("Monitor", {"command": "git status"}) is False
    assert needs("Monitor", {"command": "tail -f app.log | grep ERROR"}) is True
    assert needs("Monitor", {}) is True
