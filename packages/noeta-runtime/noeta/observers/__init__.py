"""Built-in Observers.

AuditObserver + MetricsObserver are the in-tree Observers. The
transport-neutral EventFanout lives with the noeta-cli/server
slice instead, since its first consumer, SSE, is fundamentally an HTTP
protocol and the single-host runtime has no HTTP server.

Both Observers subscribe through ``EventLogSubscriber.subscribe`` —
no formal ``Observer`` Protocol is introduced; the existing pattern
(``ChildLifecycleObserver`` style: self-subscribe + ``stop()``) is
reused.

Subscriber callbacks fire post-COMMIT and
**outside** the EventLog writer lock.
Multiple writer threads may invoke an Observer's callback concurrently;
each Observer guards its own state with a ``threading.Lock``.
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
