"""External trace export (T1) — a live-only observer that ships the
EventLog as a trace to an external sink (v1: newline-JSON to a file).

Reuses :class:`noeta.observers.audit.AuditObserver`'s projection of every
``EventEnvelope`` into an :class:`AuditRecord` (an **allowlist
projection** — it does NOT carry the raw goal, tool arguments, or
message / LLM bodies; it DOES carry operational metadata: task_id,
tool_name, model, content-hash refs, summary/reason, possibly paths — so
a trace file is sensitive operational data, not a scrubbed artifact).

Three pieces, all `noeta.protocols` + stdlib only (``observers-only-
protocols`` holds; no ``httpx`` / OTel — OTLP/Langfuse are a documented
follow-on ``inner`` adapter, not built here):

* :class:`JsonlTraceSink` — the inner exporter: one canonical-JSON line
  per record appended to a real file. IO failure is logged + dropped.
* :class:`AsyncTraceSink` — wraps an inner ``AuditSink`` so the EventLog
  emit path never blocks: the hot-path ``__call__`` only ``put_nowait``s
  onto a bounded queue (full → drop + log); a background worker runs the
  inner. ``stop()`` drains the pre-stop backlog within a bounded timeout,
  dropping only an overrunning (stuck/too-slow) remainder.
* :class:`TraceExportObserver` — the **lifecycle owner**. Because
  ``AuditObserver.stop()`` only unsubscribes (it does not stop a sink),
  this composes the subscription + the async sink + the file and stops
  them in a fixed order. It is what the runtime's observer list owns.

Live-only: it is wired only at the live construction point
(``noeta.execution.builder``) and never participates in fold / resume /
state reconstruction, so a rebuilt state is identical with or without a
trace export. No L0 / fold change.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any, Optional

from noeta.observers.audit import AuditObserver, AuditRecord, AuditSink
from noeta.protocols.event_log import EventLogSubscriber


__all__ = [
    "AsyncTraceSink",
    "JsonlTraceSink",
    "TraceExportObserver",
]


_log = logging.getLogger(__name__)

_DEFAULT_QUEUE_MAX = 1024
_WORKER_POLL_S = 0.1
#: On stop, the worker is given this long to drain records already queued
#: BEFORE stop; only if it overruns (a stuck/too-slow inner) is the
#: remainder dropped and the daemon worker abandoned.
_STOP_DRAIN_TIMEOUT_S = 2.0


class JsonlTraceSink:
    """Append one canonical-JSON line per :class:`AuditRecord` to a file.

    IO errors are logged + dropped (an export hiccup must never break the
    run). Single-writer by construction (the :class:`AsyncTraceSink`
    worker calls it serially)."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._fh: Optional[Any] = None
        try:
            self._fh = open(self._path, "a", encoding="utf-8")
        except OSError as exc:
            _log.warning("trace export: cannot open %s: %s; disabled", self._path, exc)

    def __call__(self, record: AuditRecord) -> None:
        if self._fh is None:
            return
        try:
            line = json.dumps(
                dataclasses.asdict(record),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            self._fh.write(line + "\n")
            self._fh.flush()
        except (OSError, ValueError) as exc:  # noqa: BLE001 — never break the run
            _log.warning("trace export: write to %s failed: %s", self._path, exc)

    def close(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None


class AsyncTraceSink:
    """Non-blocking wrapper: the hot-path ``__call__`` only enqueues; a
    background worker runs ``inner``. The EventLog emit path is never
    blocked on export (a slow/stuck collector applies back-pressure via
    drop, never a stall)."""

    def __init__(
        self,
        inner: AuditSink,
        *,
        max_queue: int = _DEFAULT_QUEUE_MAX,
    ) -> None:
        self._inner = inner
        self._q: "queue.Queue[AuditRecord]" = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._drain, name="noeta-trace-export", daemon=True
        )
        self._worker.start()

    def __call__(self, record: AuditRecord) -> None:
        # Pure hot-path: stopped → drop; else non-blocking enqueue. NO
        # file IO, no blocking — a full queue drops + logs.
        if self._stop.is_set():
            return
        try:
            self._q.put_nowait(record)
        except queue.Full:
            _log.warning("trace export: queue full; dropping a trace record")

    def _drain(self) -> None:
        # Drain until the queue is empty AND stop was requested. This means
        # `stop()` does NOT discard records already queued before it — the
        # worker keeps writing them; it only exits once the backlog is gone.
        while True:
            try:
                record = self._q.get(timeout=_WORKER_POLL_S)
            except queue.Empty:
                if self._stop.is_set():
                    return  # stop requested and the backlog is fully drained
                continue
            try:
                self._inner(record)
            except Exception:  # noqa: BLE001 — an export must never crash the worker
                _log.warning("trace export: inner sink raised; continuing", exc_info=True)

    def stop(self) -> None:
        """Bounded graceful shutdown: stop accepting new records, then let
        the worker drain the records already queued BEFORE stop within a
        bounded timeout. On a clean (non-stuck) inner this returns with the
        worker dead and every pre-stop record written. Only if the inner is
        stuck / too slow to finish within the timeout is the remaining
        backlog dropped and the daemon worker abandoned (Python cannot
        safely cancel a thread)."""
        self._stop.set()
        # New records are already dropped by __call__; the worker keeps
        # draining the pre-stop backlog. Give it a bounded window.
        self._worker.join(timeout=_STOP_DRAIN_TIMEOUT_S)
        if self._worker.is_alive():
            # Stuck / too-slow inner: drop the remaining backlog so the
            # abandoned daemon worker reaches an empty queue and exits
            # quickly (it writes at most its one in-flight record more).
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break


class TraceExportObserver:
    """Lifecycle owner: subscribes an :class:`AuditObserver` whose sink is
    an :class:`AsyncTraceSink` over ``inner_sink``, and stops all three in
    the right order (``AuditObserver.stop()`` alone only unsubscribes — it
    does not stop a sink)."""

    def __init__(
        self,
        *,
        event_log: EventLogSubscriber,
        inner_sink: JsonlTraceSink,
        max_queue: int = _DEFAULT_QUEUE_MAX,
    ) -> None:
        self._inner = inner_sink
        self._async = AsyncTraceSink(inner_sink, max_queue=max_queue)
        self._observer = AuditObserver(event_log=event_log, sink=self._async)
        self._stopped = False

    def stop(self) -> None:
        """Fixed order, idempotent: (1) unsubscribe so no new records
        arrive, (2) stop the async sink (drain/join; an overrun drops the
        remainder), (3) close the file."""
        if self._stopped:
            return
        self._stopped = True
        with contextlib.suppress(Exception):
            self._observer.stop()
        with contextlib.suppress(Exception):
            self._async.stop()
        with contextlib.suppress(Exception):
            self._inner.close()


def make_jsonl_trace_observer(
    *, event_log: EventLogSubscriber, path: Path
) -> TraceExportObserver:
    """Build a JSONL trace export observer for the given file path."""
    return TraceExportObserver(event_log=event_log, inner_sink=JsonlTraceSink(path))
