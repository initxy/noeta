"""Ship the EventLog to an external trace sink, live only.

Records are :class:`~noeta.observers.audit.AuditRecord` projections, so a trace
carries operational metadata (task ids, tool and model names, content hashes,
summaries, possibly paths) but no goal, tool arguments, or message / LLM bodies
— sensitive operational data, not a scrubbed artifact. Export is non-blocking by
construction: the emit path only enqueues, a daemon worker runs the inner sink,
and overflow or IO failure is dropped with a warning rather than allowed to
stall or break a run. Trace export never participates in fold or state
reconstruction, so a rebuilt state is identical with or without it; transport
dependencies stay out of this layer, so an OTLP exporter is supplied as an
``inner_sink`` by the host.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any, Optional, Protocol

from noeta.observers.audit import AuditObserver, AuditRecord, AuditSink
from noeta.protocols.event_log import EventLogSubscriber


__all__ = [
    "AsyncTraceSink",
    "JsonlTraceSink",
    "TraceExportObserver",
    "TraceSink",
]


class TraceSink(Protocol):
    """An inner exporter: a serially-invoked :data:`AuditSink` plus a
    ``close()`` that releases its transport (file handle, final HTTP flush)."""

    def __call__(self, record: AuditRecord) -> None: ...

    def close(self) -> None: ...


_log = logging.getLogger(__name__)

_DEFAULT_QUEUE_MAX = 1024
_WORKER_POLL_S = 0.1
#: How long ``stop()`` waits for the pre-stop backlog to drain before giving up
#: on a stuck or too-slow inner sink and abandoning the daemon worker.
_STOP_DRAIN_TIMEOUT_S = 2.0


class JsonlTraceSink:
    """Append one canonical-JSON line per :class:`AuditRecord` to a file.

    An unopenable path or a failed write is logged and dropped — an export
    hiccup must never break the run. Not thread-safe, and does not need to be:
    the :class:`AsyncTraceSink` worker is its only caller."""

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
    """Non-blocking wrapper: ``__call__`` only enqueues, a background worker
    runs ``inner``. A slow or stuck collector therefore costs dropped records,
    never a stalled EventLog emit path."""

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
        # Runs on the EventLog emit path: no IO, nothing blocking.
        if self._stop.is_set():
            return
        try:
            self._q.put_nowait(record)
        except queue.Full:
            _log.warning("trace export: queue full; dropping a trace record")

    def _drain(self) -> None:
        # Exit requires an empty queue AND a stop request, so ``stop()`` does
        # not discard records queued before it.
        while True:
            try:
                record = self._q.get(timeout=_WORKER_POLL_S)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                self._inner(record)
            except Exception:  # noqa: BLE001 — an export must never crash the worker
                _log.warning("trace export: inner sink raised; continuing", exc_info=True)

    def stop(self) -> None:
        """Bounded graceful shutdown: against a healthy inner sink this returns
        with the worker dead and every record queued before the stop written.
        A stuck or too-slow inner costs the remaining backlog and leaves the
        daemon worker abandoned, because Python cannot safely cancel a
        thread."""
        self._stop.set()
        self._worker.join(timeout=_STOP_DRAIN_TIMEOUT_S)
        if self._worker.is_alive():
            # Emptying the queue lets the abandoned worker finish its in-flight
            # record and exit instead of writing the whole backlog behind us.
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break


class TraceExportObserver:
    """Lifecycle owner for the subscription, the async worker and the sink.

    This is what an observer list holds: ``AuditObserver.stop()`` alone only
    unsubscribes, leaving a worker thread and an open transport behind."""

    def __init__(
        self,
        *,
        event_log: EventLogSubscriber,
        inner_sink: TraceSink,
        max_queue: int = _DEFAULT_QUEUE_MAX,
    ) -> None:
        self._inner = inner_sink
        self._async = AsyncTraceSink(inner_sink, max_queue=max_queue)
        self._observer = AuditObserver(event_log=event_log, sink=self._async)
        self._stopped = False

    def stop(self) -> None:
        """Idempotent, and the order is load-bearing: unsubscribe first so no
        new records arrive, drain the worker second, release the transport
        last."""
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
    return TraceExportObserver(event_log=event_log, inner_sink=JsonlTraceSink(path))
