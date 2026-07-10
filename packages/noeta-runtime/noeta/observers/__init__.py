"""Built-in Observers.

AuditObserver + MetricsObserver are the in-tree Observers. EventFanout
(``noeta.observers.fanout``) lives in this package too — it is
transport-neutral, knowing only :class:`EventEnvelope`; the HTTP/SSE
transport itself (``noeta.agent.backend``) is a *consumer* of the
fan-out, not part of it.

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
