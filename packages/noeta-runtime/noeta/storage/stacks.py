"""Single source of truth for assembling the storage triple.

The ``(EventLogFull, ContentStore, Dispatcher)`` triple is wired here exactly
once and shared by :mod:`noeta.client` (the SDK host, production) and
:mod:`noeta.testing.profile` (test-support) so the two cannot drift. It lives in
``noeta.storage`` (L2 kernel-services) so both consumers may import it without
crossing the production/testing boundary (``noeta.client`` is above storage, and
``noeta.testing`` re-exports these names for the test suite).
"""

from __future__ import annotations

from typing import Optional

from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


__all__ = [
    "is_memory_path",
    "build_memory_stack",
    "build_sqlite_stack",
    "open_storage_stack",
]


def is_memory_path(sqlite_path: Optional[str]) -> bool:
    """Treat ``None`` / ``":memory:"`` as the InMemory adapter stack."""
    return sqlite_path is None or sqlite_path == ":memory:"


def open_storage_stack(
    sqlite_path: Optional[str],
) -> tuple[EventLogFull, ContentStore, Dispatcher]:
    """Single helper that picks InMemory vs Sqlite based on ``sqlite_path``.

    Collapses the ``if is_memory_path(...): ... else: ...`` branch its callers
    (the ``python -m noeta.agent`` runner and the test suite) would otherwise
    repeat. Return type uses the existing L0 Protocols
    (``EventLogFull / ContentStore / Dispatcher``) — no new "storage bundle"
    dataclass is introduced.
    """
    if is_memory_path(sqlite_path):
        return build_memory_stack()
    assert sqlite_path is not None  # narrowed by is_memory_path
    return build_sqlite_stack(sqlite_path)


def build_memory_stack() -> tuple[EventLogFull, ContentStore, Dispatcher]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    return event_log, content_store, dispatcher


def build_sqlite_stack(path: str) -> tuple[EventLogFull, ContentStore, Dispatcher]:
    # Local import keeps cold ``import noeta.storage.stacks`` cheap.
    from noeta.storage.sqlite import (
        SqliteContentStore,
        SqliteDispatcher,
        SqliteEventLog,
    )

    dispatcher = SqliteDispatcher(path)
    event_log = SqliteEventLog(path, lease_validator=dispatcher)
    content_store = SqliteContentStore(path)
    return event_log, content_store, dispatcher
