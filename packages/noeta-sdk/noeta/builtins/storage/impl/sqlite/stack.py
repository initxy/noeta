"""``build_stack`` — the sqlite backend's stack factory.

Every storage backend ships one ``build_stack(**config)`` returning the
``(EventLogFull, ContentStore, Dispatcher)`` triple, wiring the triple's
one internal invariant itself: the event log takes the dispatcher as
``lease_validator``. The host injects the result through ``HostConfig``.
"""

from __future__ import annotations

from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull

from noeta.builtins.storage.impl.sqlite.contentstore import SqliteContentStore
from noeta.builtins.storage.impl.sqlite.dispatcher import SqliteDispatcher
from noeta.builtins.storage.impl.sqlite.eventlog import SqliteEventLog


__all__ = ["build_stack"]


def build_stack(*, path: str) -> tuple[EventLogFull, ContentStore, Dispatcher]:
    """Build the three sqlite adapters over one database file at ``path``."""
    dispatcher = SqliteDispatcher(path)
    event_log = SqliteEventLog(path, lease_validator=dispatcher)
    content_store = SqliteContentStore(path)
    return event_log, content_store, dispatcher
