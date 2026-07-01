"""``MetricsObserver`` — process-local event counters.

Issue 19. Subscribes to an ``EventLogSubscriber`` and maintains
per-event-type and per-(task_id, event_type) counters in memory.
``snapshot()`` returns a defensive copy of the current view so
callers (tests, debug tooling, future Phase 2 metrics-backend
adapters) can read without racing the writer.

Phase 1 keeps the surface intentionally narrow: integer counters,
no histograms, no quantiles, no cross-process aggregation. Real
metrics backend wiring (Prometheus / OTel / DataDog) is an
application concern that lands in Phase 2.

Thread-safety: subscriber callbacks fire post-COMMIT and outside the
EventLog writer lock (issues 15/16/17). Multiple writer threads can
enter ``_on_event`` concurrently; the internal ``threading.Lock``
serialises counter increments and snapshot reads.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from noeta.protocols.event_log import EventLogSubscriber, subscribe_with_stop
from noeta.protocols.events import EventEnvelope


__all__ = ["MetricsObserver", "MetricsSnapshot"]


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """Read-only view of :class:`MetricsObserver` state at a moment.

    ``by_type`` is a ``event_type → count`` mapping aggregated across
    every task this observer has seen. ``by_task_type`` keys on
    ``(task_id, event_type)``; the compound key keeps snapshot copies
    flat and avoids nested-dict ownership questions when callers
    mutate (callers can't — the snapshot is defensively copied).
    """

    by_type: dict[str, int]
    by_task_type: dict[tuple[str, str], int]
    total_events: int


class MetricsObserver:
    """Subscribes to an EventLog and counts envelopes per type / task."""

    name = "metrics"

    def __init__(self, *, event_log: EventLogSubscriber) -> None:
        self._by_type: dict[str, int] = {}
        self._by_task_type: dict[tuple[str, str], int] = {}
        self._total = 0
        self._lock = threading.Lock()
        self._handle = subscribe_with_stop(event_log, self._on_event)

    def stop(self) -> None:
        self._handle.stop()

    def _on_event(self, env: EventEnvelope) -> None:
        # Concurrent subscriber callbacks from multiple writer threads
        # (issue 19 B1) require the lock around every counter update.
        with self._lock:
            self._by_type[env.type] = self._by_type.get(env.type, 0) + 1
            key = (env.task_id, env.type)
            self._by_task_type[key] = self._by_task_type.get(key, 0) + 1
            self._total += 1

    def snapshot(self) -> MetricsSnapshot:
        """Return a defensive copy of the counters."""
        with self._lock:
            return MetricsSnapshot(
                by_type=dict(self._by_type),
                by_task_type=dict(self._by_task_type),
                total_events=self._total,
            )
