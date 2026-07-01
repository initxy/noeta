"""storage — durable (sqlite) storage triple for the host's ``HostConfig``.

The SDK's ``HostConfig`` accepts an
external ``(event_log, content_store, dispatcher)`` triple (all-or-none) so a
product backend can opt into durable storage while still driving the engine only
through ``noeta.sdk``. This module is the host-side **material** that builds that
triple from a sqlite file — product wiring the new backend reuses (sibling to
:mod:`noeta.agent.host.preview_gateway` / :mod:`noeta.agent.host.mcp_registry`),
not part of the SDK or the engine.

The three are constructed together so the event log already holds the dispatcher
as its ``lease_validator`` (the invariant ``HostConfig.storage_triple`` documents).
A single sqlite file backs all three adapters; ``close`` releases them in reverse
construction order. In-memory is the SDK's own default (an omitted triple), so
this helper only ever builds the durable case.
"""

from __future__ import annotations

import contextlib
from typing import Callable, Tuple

from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull


StorageTriple = Tuple[EventLogFull, ContentStore, Dispatcher]


def open_sqlite_storage(sqlite_path: str) -> tuple[StorageTriple, Callable[[], None]]:
    """Build the durable ``(event_log, content_store, dispatcher)`` over a sqlite
    file, plus a ``close`` callable.

    The dispatcher is constructed first and handed to the event log as its
    ``lease_validator`` (the all-or-none triple's wiring invariant). ``close``
    closes each adapter that owns a connection, in reverse construction order;
    it suppresses per-adapter close errors so a partial close never masks the
    real shutdown path.
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

    def close() -> None:
        for adapter in (content_store, event_log, dispatcher):
            closer = getattr(adapter, "close", None)
            if closer is None:
                continue
            with contextlib.suppress(Exception):
                closer()

    return (event_log, content_store, dispatcher), close
