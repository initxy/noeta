"""storage — durable storage triple for the host's ``HostConfig``.

The SDK's ``HostConfig`` accepts an
external ``(event_log, content_store, dispatcher)`` triple (all-or-none) so a
product backend can opt into durable storage while still driving the engine only
through ``noeta.sdk``. This module is the host-side **material** that builds that
triple from a storage URL — product wiring the backend reuses (sibling to
:mod:`noeta.agent.host.preview_gateway` / :mod:`noeta.agent.host.mcp_registry`),
not part of the SDK or the engine.

Two durable backends: a sqlite file path (single file backs all three
adapters) and a ``postgresql://`` DSN (one database backs all three);
:func:`open_durable_storage` dispatches on the URL shape. The three are
constructed together so the event log already holds the dispatcher as its
``lease_validator`` (the invariant ``HostConfig.storage_triple`` documents).
``close`` releases them in reverse construction order. In-memory is the SDK's
own default (an omitted triple), so these helpers only ever build the durable
case.
"""

from __future__ import annotations

import contextlib
from typing import Callable, Tuple

from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull


StorageTriple = Tuple[EventLogFull, ContentStore, Dispatcher]


def open_durable_storage(
    storage_url: str,
) -> tuple[StorageTriple, Callable[[], None]]:
    """Build the durable triple for ``storage_url`` (sqlite path or Postgres DSN)."""
    if storage_url.startswith(("postgresql://", "postgres://")):
        return open_postgres_storage(storage_url)
    return open_sqlite_storage(storage_url)


def _close_in_reverse(*adapters: object) -> Callable[[], None]:
    """A ``close`` that releases each adapter owning a connection, in the
    given (reverse-construction) order; per-adapter close errors are
    suppressed so a partial close never masks the real shutdown path."""

    def close() -> None:
        for adapter in adapters:
            closer = getattr(adapter, "close", None)
            if closer is None:
                continue
            with contextlib.suppress(Exception):
                closer()

    return close


def open_sqlite_storage(sqlite_path: str) -> tuple[StorageTriple, Callable[[], None]]:
    """Build the durable ``(event_log, content_store, dispatcher)`` over a sqlite
    file, plus a ``close`` callable.

    The dispatcher is constructed first and handed to the event log as its
    ``lease_validator`` (the all-or-none triple's wiring invariant).
    """
    # Local import keeps a bare ``import noeta.agent`` cheap and confines the
    # noeta.storage dependency to this host-side material module.
    from noeta.storage.sqlite import (
        SqliteContentStore,
        SqliteDispatcher,
        SqliteEventLog,
    )

    dispatcher = SqliteDispatcher(sqlite_path)
    event_log = SqliteEventLog(sqlite_path, lease_validator=dispatcher)
    content_store = SqliteContentStore(sqlite_path)

    return (event_log, content_store, dispatcher), _close_in_reverse(
        content_store, event_log, dispatcher
    )


def open_postgres_storage(dsn: str) -> tuple[StorageTriple, Callable[[], None]]:
    """Build the durable triple over a Postgres DSN, plus a ``close`` callable.

    Same wiring invariant as the sqlite builder. psycopg is an optional
    dependency (``noeta-runtime[postgres]``); the local import surfaces a
    clear ImportError only when a Postgres URL was actually configured.
    """
    from noeta.storage.postgres import (
        PostgresContentStore,
        PostgresDispatcher,
        PostgresEventLog,
    )

    dispatcher = PostgresDispatcher(dsn)
    event_log = PostgresEventLog(dsn, lease_validator=dispatcher)
    content_store = PostgresContentStore(dsn)

    return (event_log, content_store, dispatcher), _close_in_reverse(
        content_store, event_log, dispatcher
    )
