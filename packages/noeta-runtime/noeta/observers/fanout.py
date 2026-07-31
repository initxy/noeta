"""Fan every ``EventEnvelope`` out to per-subscriber bounded queues.

Nothing here knows HTTP, sockets, SSE framing, or NDJSON — a transport adapter
subscribes and iterates its own subscription, and that is the only place a
wire format exists. Each subscription owns its queue and the broadcaster runs
no worker thread of its own, so publishing can never block on a consumer's
write; a subscriber that lets its queue fill is closed and dropped instead.
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
    """Posted to a subscription's queue on close so a blocked
    :meth:`FanoutSubscription.get` returns promptly."""


_SENTINEL = _CloseSentinel()


class FanoutSubscription:
    """One subscriber's view of the broadcast stream.

    The consumer pulls; the broadcaster only ``put_nowait``s. A consumer too
    slow to keep its bounded queue from filling is closed by the broadcaster
    rather than waited on: events stop arriving, ``get()`` returns ``None``,
    and the iterator exits.
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
        """``None`` means either "closed" or "timed out" — check
        :attr:`closed` to tell them apart."""
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(item, _CloseSentinel):
            return None
        return item

    def __iter__(self) -> Iterator[EventEnvelope]:
        """Yield envelopes until :meth:`close` is called."""
        while not self._closed:
            env = self.get(timeout=1.0)
            if env is None:
                if self._closed:
                    return
                continue
            yield env

    def close(self) -> None:
        """Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass


class EnvelopeBroadcaster:
    """Bounded fan-out from one publisher to N subscriptions.

    :meth:`publish` runs on the caller's thread and only ``put_nowait``s, so it
    never blocks the EventLog writer. Thread-safe: subscribe / publish / close
    serialise on one mutex, which is only ever held across non-blocking work.
    """

    def __init__(self, *, max_queue_size: int = _DEFAULT_MAX_QUEUE_SIZE) -> None:
        self._max_queue_size = max_queue_size
        self._subscriptions: list[FanoutSubscription] = []
        self._lock = threading.Lock()
        self._closed = False

    def subscribe(self) -> FanoutSubscription:
        """Raises :class:`RuntimeError` once closed: a subscriber attached
        after shutdown would wait forever for events that cannot come."""
        with self._lock:
            if self._closed:
                raise RuntimeError("EnvelopeBroadcaster is closed")
            sub = FanoutSubscription(max_queue_size=self._max_queue_size)
            self._subscriptions.append(sub)
            return sub

    def publish(self, env: EventEnvelope) -> None:
        """Fan ``env`` out to every live subscription; never raises, and never
        blocks longer than a ``put_nowait``."""
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
        """Idempotent."""
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

    A failure inside ``broadcaster.publish`` is swallowed at WARNING: an
    Observer callback must never raise back into the EventLog writer.
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
