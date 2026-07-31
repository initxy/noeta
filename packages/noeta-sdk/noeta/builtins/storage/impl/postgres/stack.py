"""``build_stack`` — the Postgres backend's stack factory.

Every storage backend ships one ``build_stack(**config)`` returning the
``(EventLogFull, ContentStore, Dispatcher)`` triple and wires the triple's one
internal invariant itself: the event log takes the dispatcher as its
``lease_validator``.
"""

from __future__ import annotations

from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull

from noeta.builtins.storage.impl.postgres.contentstore import PostgresContentStore
from noeta.builtins.storage.impl.postgres.dispatcher import PostgresDispatcher
from noeta.builtins.storage.impl.postgres.eventlog import PostgresEventLog


__all__ = ["build_stack"]


def build_stack(*, dsn: str) -> tuple[EventLogFull, ContentStore, Dispatcher]:
    """Build the three Postgres adapters over one database at ``dsn``."""
    dispatcher = PostgresDispatcher(dsn)
    event_log = PostgresEventLog(dsn, lease_validator=dispatcher)
    content_store = PostgresContentStore(dsn)
    return event_log, content_store, dispatcher
