"""delta_hub — thread-safe fan-out for ephemeral token deltas (product layer).

The token-streaming projection (ADR ``token-streaming-projection.md``): while a
streaming-capable provider call is in flight, the runtime pushes ``StreamDelta``
fragments through the host-config ``delta_sink``; this hub fans them out to the
live SSE connections. Deltas are **ephemeral** — never persisted, never folded,
never replayed — so the hub is deliberately the whole delta channel: no cursor,
no history, no queueing. A subscriber that is not listening at publish time
simply misses the delta; the final ``MessagesAppended`` envelope repaints the
truth by the normal path.

Mirrors the fanout layer's discipline (``noeta.observers.fanout``): this module
is **transport-blind** — it knows neither HTTP nor sockets nor SSE framing, and
it never inspects the delta payload. It is also NOT ``EnvelopeBroadcaster``:
deltas are not envelopes and must not ride the envelope fan-out (that layer is
AST-guarded to know only ``EventEnvelope``).
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from noeta.sdk import StepContext


__all__ = ["DeltaHub"]


#: A subscriber receives ``(task_id, call_id, delta)``. ``delta`` is the
#: runtime's ``StreamDelta`` (``kind`` / ``text`` / ``index``) — annotated
#: structurally because the backend reaches the engine only through
#: ``noeta.sdk`` (backend-only-sdk contract) and the hub never looks inside it.
DeltaCallback = Callable[[str, str, Any], None]


class DeltaHub:
    """One publisher (the per-turn LLM drive thread) to N subscribers.

    Thread-safe: subscribe / unsubscribe / publish serialise on a single
    mutex held only to snapshot or mutate the subscriber map — callbacks run
    OUTSIDE the lock so a slow subscriber cannot serialise publishers, and an
    unsubscribe from inside a callback cannot deadlock. A subscriber exception
    never propagates to the publisher: the publisher is the LLM drive thread,
    and a delta consumer must never fail an LLM call (same stance as the
    runtime's sink-exception swallow).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[int, DeltaCallback] = {}
        self._next_token = 0

    def publish(self, task_id: str, call_id: str, delta: Any) -> None:
        """Fan one delta out to every current subscriber.

        Iterates a snapshot taken under the lock, so a concurrent
        subscribe/unsubscribe never mutates the iteration; exceptions are
        swallowed (observational channel — the delta is simply lost to that
        subscriber).
        """
        with self._lock:
            callbacks = list(self._subscribers.values())
        for callback in callbacks:
            try:
                callback(task_id, call_id, delta)
            except Exception:  # noqa: BLE001 — must never reach the drive thread
                pass

    def subscribe(self, callback: DeltaCallback) -> Callable[[], None]:
        """Register ``callback``; returns an idempotent unsubscribe."""
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._subscribers[token] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(token, None)

        return unsubscribe

    def sink(self, ctx: StepContext, call_id: str, delta: Any) -> None:
        """The ``HostConfig.delta_sink`` adapter: extract the task identity.

        Bound method shaped ``(ctx, call_id, delta)`` — exactly what the
        runtime's streaming seam calls — so ``EngineRoom`` wires
        ``hub.sink`` straight into the host config.
        """
        self.publish(ctx.task_id, call_id, delta)
