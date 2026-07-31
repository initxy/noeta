"""Wire the default Observers onto an EventLog.

The Engine knows nothing about Dispatcher or Observer, so the parent/child
handoff is delivered by :class:`noeta.core.observers.ChildLifecycleObserver`
subscribing to the EventLog. This is the single wiring point, called once per
``(event_log, dispatcher)`` pair by whoever owns the runtime.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from noeta.core.observers import ChildLifecycleObserver
from noeta.protocols.event_log import EventLogFull


class _Dispatcher(Protocol):
    def enqueue(self, task_id: str) -> None: ...

    def wake(self, task_id: str, wake_event: Any) -> bool: ...


def wire_default_observers(
    event_log: EventLogFull, dispatcher: _Dispatcher
) -> Callable[[], None]:
    """Install the default observer set; the returned callable unsubscribes."""
    observer = ChildLifecycleObserver(
        event_log=event_log, dispatcher=dispatcher
    )
    return observer.stop
