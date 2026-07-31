"""Built-in Observers: audit projection, event counters, envelope fan-out.

There is no formal ``Observer`` Protocol — an Observer self-subscribes to an
``EventLogSubscriber`` on construction and detaches through ``stop()``.
Subscriber callbacks fire post-COMMIT and **outside** the EventLog writer lock,
so several writer threads may enter one concurrently and every Observer must
guard its own state.
"""

from __future__ import annotations

from noeta.observers.audit import (
    AuditObserver,
    AuditRecord,
    AuditSink,
)
from noeta.observers.metrics import MetricsObserver, MetricsSnapshot
from noeta.observers.fanout import (
    EnvelopeBroadcaster,
    EventFanout,
    FanoutSubscription,
)


__all__ = [
    "AuditObserver",
    "AuditRecord",
    "AuditSink",
    "MetricsObserver",
    "MetricsSnapshot",
    "EnvelopeBroadcaster",
    "EventFanout",
    "FanoutSubscription",
]
