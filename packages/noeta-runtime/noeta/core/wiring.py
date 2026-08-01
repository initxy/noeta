"""Wire the default Observers onto an EventLog.

The Engine knows nothing about Dispatcher or Observer, so the parent/child
handoff is delivered by :class:`noeta.core.observers.ChildLifecycleObserver`
subscribing to the EventLog. This is the single wiring point — and it is
**idempotent per event log instance**: N clients sharing one storage triple
get exactly one default observer, owned by the store's lifetime rather than
any one client's (ADR ``worker-queue-routing``).
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from noeta.core.observers import ChildLifecycleObserver
from noeta.protocols.event_log import EventLogFull


class _Dispatcher(Protocol):
    def enqueue(
        self, task_id: str, *, parent_task_id: Optional[str] = None
    ) -> None: ...

    def wake(self, task_id: str, wake_event: Any) -> bool: ...


#: Marker attribute set on the event log holding the wired observer's stop.
#: An instance attribute (not a registry) so the marker's lifetime is exactly
#: the store object's — a fresh stack starts unwired, always.
_WIRED_ATTR = "_noeta_default_observers_stop"


def wire_default_observers(
    event_log: EventLogFull, dispatcher: _Dispatcher
) -> Callable[[], None]:
    """Install the default observer set once per ``event_log`` instance.

    A repeat call over an already-wired log is a no-op returning the same
    stop callable, so a shared triple never accumulates duplicate observers.
    ``stop`` unsubscribes and clears the marker (test hygiene — a later wire
    call may install a fresh observer).
    """
    existing = getattr(event_log, _WIRED_ATTR, None)
    if existing is not None:
        stop_existing: Callable[[], None] = existing
        return stop_existing

    observer = ChildLifecycleObserver(
        event_log=event_log, dispatcher=dispatcher
    )

    def _stop() -> None:
        observer.stop()
        if getattr(event_log, _WIRED_ATTR, None) is _stop:
            delattr(event_log, _WIRED_ATTR)

    setattr(event_log, _WIRED_ATTR, _stop)
    return _stop
