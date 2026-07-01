"""Wire default Observers onto an EventLog.

Engine deliberately does not know about Dispatcher or Observer (see
CONTEXT.md: "Engine knows nothing about worker / dispatcher /
workflow"). The parent/child handoff that Phase 0 needs is delivered by
:class:`noeta.core.observers.ChildLifecycleObserver`, which subscribes
to the EventLog. Whoever owns the runtime (a Worker daemon in Phase 1,
the test fixtures today) is responsible for wiring those observers
once per ``(event_log, dispatcher)`` pair.

This helper is the single canonical wiring point so callers do not have
to import the observer class directly.
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
    """Install the Phase-0 default observer set; return an unsubscribe.

    Currently wires only :class:`ChildLifecycleObserver`. Calling the
    returned callable tears the wiring down — handy in tests so each
    case starts from a clean subscriber list.
    """
    observer = ChildLifecycleObserver(
        event_log=event_log, dispatcher=dispatcher
    )
    return observer.stop
