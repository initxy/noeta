"""Durable backend implementations for the ``storage`` built-in.

Each backend sub-package (``sqlite`` / ``postgres``) implements the L0 storage
Protocols against one database, routes the shared domain rules through
:mod:`noeta.storage.spi`, and ships a ``stack.build_stack(**config)`` factory
returning the ``(EventLogFull, ContentStore, Dispatcher)`` triple. This
``__init__`` re-exports nothing on purpose: importing the package must not drag
in one backend's driver (``psycopg``) for a host that chose the other.
"""

from __future__ import annotations

__all__: list[str] = []
