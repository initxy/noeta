"""``noeta.sdk.storage`` — the durable storage adapters a host wires, re-exported.

A product backend that opts into durable storage constructs the three L0
adapters — an event log, a dispatcher, and a content store — over one sqlite
file and passes them to the engine (``Client`` / ``HostConfig``). The concrete
adapters live in ``noeta.storage.sqlite`` (noeta-runtime); routing the import
through ``noeta.sdk`` keeps the encapsulation weld intact — the backend reaches
the engine only through ``noeta.sdk``, and ``noeta.sdk`` sits above
``noeta.storage`` in the layer order.

The three adapters share one sqlite file and one migration sequence; construct
them against the same path::

    from noeta.sdk.storage import (
        SqliteContentStore,
        SqliteDispatcher,
        SqliteEventLog,
    )

    dispatcher = SqliteDispatcher(db_path)
    event_log = SqliteEventLog(db_path, lease_validator=dispatcher)
    content_store = SqliteContentStore(db_path)

Kept in this submodule (not the ``noeta.sdk`` root) so importing the SDK stays
light and its identity-plane surface (``Options`` / ``Client`` / the extension
protocols) is not conflated with host-plane storage wiring: only a host that
builds a durable backend pulls the adapters in. This is the ``noeta.storage``
gap the repo-split public-surface audit closes by re-export, never by blessing
the internal path (see ``docs/implementation-specs/2026-07-26-sdk-only-repo-split.md``,
decision D4).
"""

from __future__ import annotations

from noeta.storage.sqlite import (
    SqliteContentStore,
    SqliteDispatcher,
    SqliteEventLog,
)


__all__ = [
    "SqliteContentStore",
    "SqliteDispatcher",
    "SqliteEventLog",
]
