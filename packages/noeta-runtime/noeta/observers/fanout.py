"""Transport-neutral envelope fan-out primitives + Observer.

These primitives are **transport-neutral**: they only know
:class:`EventEnvelope`. They do not know HTTP, sockets, SSE framing, or
NDJSON. SSE (``noeta.agent.backend``) and any future stdio-NDJSON surface are
*consumers* of the fan-out, not part of it.

Three layers with hard boundaries:

1. :class:`EventFanout` — subscribes to an ``EventLogSubscriber`` and
   forwards each :class:`EventEnvelope` to an :class:`EnvelopeBroadcaster`.
   Knows EventEnvelope; **does not** know HTTP, sockets, JSON, or
   client identity.
2. :class:`EnvelopeBroadcaster` — fans envelopes out to per-subscriber
   bounded queues. Each consumer (typically an HTTP request thread, but
   equally a stdio-NDJSON writer) gets a :class:`FanoutSubscription` it
   ``get()``s from. Slow consumers that fill their queue are closed; the
   broadcaster never has its own worker thread, so a stuck transport write
   cannot block the publisher.
3. The transport adapter (``noeta.agent.backend``, owns SSE framing and
   socket writes) calls ``broadcaster.subscribe()`` and iterates the
   subscription; broadcaster has no idea HTTP exists.

The subscription-owns-queue model (rev2 B4) is the deliberate fix for
the rev1 "broadcaster worker thread calls callbacks" shape, which would
have let a slow socket write stall the publisher.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Iterator, Optional

from noeta.protocols.event_log import EventLogSubscriber, subscribe_with_stop
from noeta.protocols.events import EventEnvelope


__all__ = ["EnvelopeBroadcaster", "EventFanout", "FanoutSubscription"]


_log = logging.getLogger(__name__)

_DEFAULT_MAX_QUEUE_SIZE = 256


class _CloseSentinel:
    """Sentinel posted to a subscription's queue when it is closed so
    a blocked :meth:`FanoutSubscription.get` returns promptly."""


_SENTINEL = _CloseSentinel()


class FanoutSubscription:
    """One subscriber's view of the broadcast stream.

    The consumer (typically an HTTP request thread, equally a
    stdio-NDJSON writer) iterates ``get()`` or ``__iter__`` to receive
    envelopes; the broadcaster only ``put_nowait``s into the
    subscription's bounded queue. If the queue fills (slow consumer),
    the broadcaster calls :meth:`close` and drops the subscription on
    its next iteration — events stop arriving, ``get()`` returns
    ``None``, the iterator exits.
    """

    def __init__(self, *, max_queue_size: int) -> None:
        self._queue: "queue.Queue[EventEnvelope | _CloseSentinel]" = queue.Queue(
            maxsize=max_queue_size
        )
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def get(self, *, timeout: Optional[float] = None) -> Optional[EventEnvelope]:
        """Block up to ``timeout`` seconds for the next envelope.

        Returns ``None`` if the subscription was closed (sentinel
        observed) or the timeout elapsed without an envelope.
        """
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(item, _CloseSentinel):
            return None
        return item

    def __iter__(self) -> Iterator[EventEnvelope]:
        """Yield envelopes until :meth:`close` is called.

        Transport adapter typical use::

            for env in subscription:
                socket.write(sse_frame(env))
        """
        while not self._closed:
            env = self.get(timeout=1.0)
            if env is None:
                if self._closed:
                    return
                continue
            yield env

    def close(self) -> None:
        """Idempotent. Enqueues a sentinel so a blocked ``get()``
        returns promptly."""
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass


class EnvelopeBroadcaster:
    """Bounded fan-out from one publisher to N subscriptions.

    No internal worker thread (rev2 B4): :meth:`publish` synchronously
    walks the subscription list and ``put_nowait``s into each. Slow
    subscriptions whose queue is full are closed and dropped from the
    list on the same pass — publish never blocks the EventLog writer.

    Thread-safe: subscribe / publish / close all serialise on a single
    mutex. The mutex is held briefly (no blocking IO under it).
    """

    def __init__(self, *, max_queue_size: int = _DEFAULT_MAX_QUEUE_SIZE) -> None:
        self._max_queue_size = max_queue_size
        self._subscriptions: list[FanoutSubscription] = []
        self._lock = threading.Lock()
        self._closed = False

    def subscribe(self) -> FanoutSubscription:
        """Register a new subscriber.

        Raises :class:`RuntimeError` if the broadcaster has been
        closed — subscribers attached after server shutdown would
        never receive events.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("EnvelopeBroadcaster is closed")
            sub = FanoutSubscription(max_queue_size=self._max_queue_size)
            self._subscriptions.append(sub)
            return sub

    def publish(self, env: EventEnvelope) -> None:
        """Fan ``env`` out to every live subscription.

        Subscriptions whose queue is full or already closed are
        dropped on this pass. Never raises; never blocks longer than
        ``put_nowait``.
        """
        dropped: list[FanoutSubscription] = []
        with self._lock:
            for sub in self._subscriptions:
                if sub.closed:
                    dropped.append(sub)
                    continue
                try:
                    sub._queue.put_nowait(env)
                except queue.Full:
                    _log.warning(
                        "EnvelopeBroadcaster: subscription queue full; closing "
                        "slow consumer (envelope id=%s task=%s)",
                        env.id,
                        env.task_id,
                    )
                    sub.close()
                    dropped.append(sub)
            for sub in dropped:
                try:
                    self._subscriptions.remove(sub)
                except ValueError:
                    pass

    def close(self) -> None:
        """Close every subscription and drop them. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for sub in self._subscriptions:
                sub.close()
            self._subscriptions.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._subscriptions)


class EventFanout:
    """Subscribes to an EventLog and forwards envelopes to an
    :class:`EnvelopeBroadcaster`.

    Same shape as :class:`noeta.observers.audit.AuditObserver` /
    :class:`noeta.observers.metrics.MetricsObserver`: self-subscribes
    on construction, ``stop()`` unsubscribes. Failures inside
    ``broadcaster.publish`` are swallowed at WARNING — Observer
    callbacks fire post-COMMIT outside the EventLog writer lock and
    must never raise back into the writer.
    """

    name = "fanout"

    def __init__(
        self,
        *,
        event_log: EventLogSubscriber,
        broadcaster: EnvelopeBroadcaster,
    ) -> None:
        self._broadcaster = broadcaster
        self._handle = subscribe_with_stop(event_log, self._on_event)

    def stop(self) -> None:
        self._handle.stop()

    def _on_event(self, env: EventEnvelope) -> None:
        try:
            self._broadcaster.publish(env)
        except Exception as exc:  # noqa: BLE001 — observer must not raise
            _log.warning(
                "EventFanout: broadcaster.publish raised %s; "
                "envelope dropped (id=%s task=%s type=%s)",
                exc,
                env.id,
                env.task_id,
                env.type,
            )
