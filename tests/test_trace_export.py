"""T1 — external trace export (JSONL sink + async worker + lifecycle owner).

Covers: the JSONL line shape (fixed AuditRecord fields), the
content-allowlist inheritance (no raw goal / tool arguments in the
export — driven through the real AuditObserver projection), the
non-blocking hot path + full-queue drop, the lifecycle-owner stop
(worker not alive after stop; bounded even with a stuck inner), and failure
swallowing.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from noeta.observers.audit import AuditRecord
from noeta.observers.trace_export import (
    AsyncTraceSink,
    JsonlTraceSink,
    TraceExportObserver,
    make_jsonl_trace_observer,
)
from noeta.storage.memory import InMemoryEventLog
from noeta.protocols.events import TaskCreatedPayload, ToolCallStartedPayload


def _record(**over: Any) -> AuditRecord:
    base: dict[str, Any] = dict(
        id="e1", task_id="t1", seq=0, type="TaskStarted", schema_version=1,
        occurred_at=1.0, actor="engine", trace_id="tr", correlation_id="c",
        causation_id=None, origin="engine", payload_summary={"x": 1},
    )
    base.update(over)
    return AuditRecord(**base)


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _wait_until(pred: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


# -- JsonlTraceSink ----------------------------------------------------------


def test_jsonl_sink_writes_fixed_record_fields(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    try:
        sink(_record(type="TaskStarted", payload_summary={"lease_id": "L1"}))
        sink(_record(seq=1, type="ToolCallFinished", payload_summary={"call_id": "c1"}))
    finally:
        sink.close()
    rows = _lines(path)
    assert len(rows) == 2
    assert set(rows[0]) == {
        "id", "task_id", "seq", "type", "schema_version", "occurred_at",
        "actor", "trace_id", "correlation_id", "causation_id", "origin",
        "payload_summary",
    }
    assert rows[0]["type"] == "TaskStarted"
    assert rows[1]["payload_summary"] == {"call_id": "c1"}


def test_jsonl_sink_bad_path_disabled_not_raising(tmp_path: Path) -> None:
    # an unwritable path → disabled, never raises (run must not break)
    sink = JsonlTraceSink(tmp_path / "nope" / "x.jsonl")  # parent missing
    sink(_record())  # no raise
    sink.close()


# -- content allowlist inheritance (real AuditObserver projection) ----------


def test_export_inherits_audit_allowlist_no_goal_no_args(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    log = InMemoryEventLog()
    obs = make_jsonl_trace_observer(event_log=log, path=path)
    try:
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="SECRET_GOAL_TEXT", policy_name="react"),
            trace_id="tr",
        )
        log.emit(
            task_id="t1",
            type="ToolCallStarted",
            payload=ToolCallStartedPayload(
                call_id="c1", tool_name="write",
                arguments={"path": "x", "content": "ARG_SECRET_BODY"},
            ),
        )
        assert _wait_until(lambda: len(_lines(path)) == 2)
        blob = path.read_text()
    finally:
        obs.stop()
    assert "SECRET_GOAL_TEXT" not in blob   # goal is banned from the projection
    assert "ARG_SECRET_BODY" not in blob    # tool arguments are banned
    rows = _lines(path)
    # operational metadata IS present (tool_name allowed).
    tcs = [r for r in rows if r["type"] == "ToolCallStarted"][0]
    assert tcs["payload_summary"]["tool_name"] == "write"
    assert "arguments" not in tcs["payload_summary"]


# -- AsyncTraceSink: non-blocking + drop + failure + bounded stop -----------


def test_async_call_does_not_block_on_slow_inner() -> None:
    gate = threading.Event()
    seen: list[AuditRecord] = []

    def slow(rec: AuditRecord) -> None:
        seen.append(rec)
        gate.wait(5.0)

    sink = AsyncTraceSink(slow, max_queue=8)
    try:
        t0 = time.monotonic()
        sink(_record())
        assert time.monotonic() - t0 < 0.3  # hot path returned immediately
        assert _wait_until(lambda: len(seen) == 1)
    finally:
        gate.set()
        sink.stop()


def test_async_full_queue_drops_without_blocking() -> None:
    gate = threading.Event()

    def blocking(rec: AuditRecord) -> None:
        gate.wait(5.0)

    sink = AsyncTraceSink(blocking, max_queue=1)
    try:
        t0 = time.monotonic()
        for _ in range(10):
            sink(_record())  # worker stuck on first; queue fills; rest drop
        assert time.monotonic() - t0 < 0.5  # never blocked
    finally:
        gate.set()
        sink.stop()


def test_async_inner_raise_is_swallowed() -> None:
    def boom(rec: AuditRecord) -> None:
        raise RuntimeError("export blew up")

    sink = AsyncTraceSink(boom)
    try:
        sink(_record())
        time.sleep(0.1)  # worker raised internally; must not crash
        sink(_record())  # still alive
        time.sleep(0.05)
    finally:
        sink.stop()


def test_async_stop_drops_new_records_and_worker_dies() -> None:
    seen: list[AuditRecord] = []
    sink = AsyncTraceSink(lambda r: seen.append(r))
    sink.stop()
    sink(_record())  # after stop → dropped
    time.sleep(0.05)
    assert seen == []
    assert not sink._worker.is_alive()


def test_async_stop_drains_pre_stop_backlog_then_worker_dies() -> None:
    """Graceful shutdown: records enqueued BEFORE stop are all written —
    stop() must not discard the queued tail (the JSONL completeness gate).
    """
    seen: list[AuditRecord] = []
    lock = threading.Lock()

    def fast(rec: AuditRecord) -> None:
        with lock:
            seen.append(rec)

    n = 200
    sink = AsyncTraceSink(fast, max_queue=n + 16)
    for i in range(n):
        sink(_record(seq=i))  # enqueue a burst
    sink.stop()  # immediately — must drain the whole backlog, not drop it
    assert not sink._worker.is_alive()
    with lock:
        assert len(seen) == n  # every pre-stop record was written


def test_async_stop_bounded_with_stuck_inner() -> None:
    gate = threading.Event()

    def stuck(rec: AuditRecord) -> None:
        gate.wait(30.0)

    sink = AsyncTraceSink(stuck)
    sink(_record())
    time.sleep(0.05)  # let the worker enter the stuck inner
    t0 = time.monotonic()
    sink.stop()  # bounded even though the inner is stuck
    assert time.monotonic() - t0 < 3.0
    gate.set()


# -- TraceExportObserver lifecycle owner ------------------------------------


def test_observer_stop_stops_worker_and_closes(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    log = InMemoryEventLog()
    obs = make_jsonl_trace_observer(event_log=log, path=path)
    log.emit(
        task_id="t1", type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="react"),
    )
    # Stop IMMEDIATELY (no wait): graceful drain must still flush the
    # pre-stop record through to the file before the worker exits.
    obs.stop()
    assert not obs._async._worker.is_alive()  # worker stopped
    assert len(_lines(path)) == 1  # the queued record was drained, not dropped
    obs.stop()  # idempotent — no raise


# -- trace-observer wiring (SDK host) ----------------------------------------


def test_code_session_trace_file_writes_jsonl(tmp_path: Path) -> None:
    """Wiring smoke: an SDK host wired with a JSONL trace observer writes a
    JSONL trace live, with the terminal tail flushed by graceful drain.

    The deleted ``CodeSessionConfig.trace_file`` knob is now an explicit
    observer (``make_jsonl_trace_observer(event_log=host.event_log, ...)``)
    constructed before driving — the observer self-subscribes on the host
    event log exactly like the product backend wires it."""
    from noeta.protocols.messages import LLMResponse, TextBlock, Usage
    from noeta.testing.fake_llm import FakeLLMProvider
    from noeta.tools.fs import FsWriteMode, ShellMode

    from tests._sdk_session import (
        make_driver,
        make_host,
        make_registry,
        runner_main_spec,
    )

    def _end() -> LLMResponse:
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
        )

    ws = tmp_path / "ws"
    ws.mkdir()
    trace = tmp_path / "trace.jsonl"
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=[_end()]),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    obs = make_jsonl_trace_observer(event_log=host.event_log, path=trace)
    try:
        out = make_driver(host).start(goal="g", agent="main")
        assert out.status == "terminal"
    finally:
        # No wait crutch: stop immediately after the turn. Graceful drain
        # must flush the terminal tail (TaskCompleted) before the trace
        # worker exits — this is the runtime completeness contract.
        obs.stop()
    rows = _lines(trace)
    assert any(r["type"] == "TaskCreated" for r in rows)
    assert any(r["type"] == "TaskCompleted" for r in rows)
