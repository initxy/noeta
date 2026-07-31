"""``MetricsObserver`` — process-local, in-memory event counters.

The surface is deliberately narrow: integer counters only, no histograms, no
quantiles, no cross-process aggregation. Exporting to a real metrics backend is
an application concern, fed by ``snapshot()``, which hands back a defensive copy
so a reader never races the writer. A lock guards every counter update because
subscriber callbacks fire post-COMMIT outside the EventLog writer lock and
several writer threads can enter ``_on_event`` at once.
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

    ``by_task_type`` uses a compound ``(task_id, event_type)`` key rather than a
    nested dict so that a snapshot stays flat and a shallow copy is genuinely a
    copy.
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
        with self._lock:
            self._by_type[env.type] = self._by_type.get(env.type, 0) + 1
            key = (env.task_id, env.type)
            self._by_task_type[key] = self._by_task_type.get(key, 0) + 1
            self._total += 1

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            return MetricsSnapshot(
                by_type=dict(self._by_type),
                by_task_type=dict(self._by_task_type),
                total_events=self._total,
            )
