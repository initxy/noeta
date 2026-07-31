"""``storage`` built-in — the durable backend implementations (impl).

Each backend sub-package (``sqlite`` / ``postgres``) implements the L0
storage Protocols in ``noeta.protocols`` against one database, routes the
shared domain rules through :mod:`noeta.storage.spi`, and ships a
``stack.build_stack(**config)`` factory that wires the
``(EventLogFull, ContentStore, Dispatcher)`` triple. Hosts reach them only
through :mod:`noeta.sdk.storage`.

This ``__init__`` deliberately re-exports nothing so importing the package
does not pull in a backend's dependencies (``psycopg``) for callers that
only chose the other one.
"""

from __future__ import annotations

__all__: list[str] = []
