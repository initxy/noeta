"""The Postgres store's connections: the pool, opening the store on first
use (schema setup, then the change feed's ``LISTEN`` connection) and running
one transaction with the retry rules.

Deadlocks and serialization failures abort a transaction whole; it is
re-run (up to :data:`DEADLOCK_RETRIES` times), and its checks run again.

Connection loss: a connection that drops before ``COMMIT`` is sent leaves
nothing behind, so the transaction is re-run once on a fresh connection. A
drop while ``COMMIT`` is in flight raises ``CommitUnknown`` (a ``Conflict``)
and is never re-run; the pool replaces the broken connection for the next
call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from noeta.backends.stores.postgres_feed import ChangeFeed
from noeta.core.errors import CommitUnknown, StoreUnavailable

T = TypeVar("T")
Conn = psycopg.AsyncConnection[Any]

RETRY_SQLSTATES = frozenset({"40001", "40P01"})  # serialization failure, deadlock
LOST_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})
DEADLOCK_RETRIES = 5


def lost(conn: Conn | None, exc: BaseException) -> bool:
    """Whether ``exc`` means the connection is gone (not a query error)."""
    if not isinstance(exc, psycopg.OperationalError | psycopg.InterfaceError):
        return False
    if conn is not None and (conn.closed or conn.broken):
        return True
    state = getattr(exc, "sqlstate", None) or ""
    return state.startswith("08") or state in LOST_SQLSTATES


class Connections:
    """A store's connection pool. ``setup`` runs once, in a connection of
    its own, when the store is first used; ``feed`` gets every pooled
    connection (its own notifications are told apart) and its ``LISTEN``
    connection is opened right after ``setup``."""

    def __init__(self, dsn: str, *, name: str, pool_size: int, connect_timeout: float,
                 feed: ChangeFeed, setup: Callable[[Conn], Awaitable[None]]) -> None:
        self.connect_timeout = connect_timeout
        self._feed = feed
        self._setup = setup
        self._pool = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=max(pool_size, 1),
            open=False,
            kwargs={"autocommit": True},
            configure=self._configure,
            check=AsyncConnectionPool.check_connection,
            timeout=connect_timeout,
            name=name,
        )
        self._open_lock = asyncio.Lock()
        self._opened = False
        self._closed = False

    async def _configure(self, conn: Conn) -> None:
        self._feed.add_own(conn)

    async def open(self) -> None:
        if self._opened:
            return
        async with self._open_lock:
            if self._opened:
                return
            if self._closed:
                raise StoreUnavailable("the store is closed")
            try:
                await self._pool.open(wait=True, timeout=self.connect_timeout)
                async with self._pool.connection() as conn:
                    await self._setup(conn)
                listen = await self._feed.connect()
            except (PoolTimeout, psycopg.OperationalError) as e:
                raise StoreUnavailable(f"cannot open the postgres store: {e}") from e
            self._feed.start(listen)
            self._opened = True

    async def tx(self, body: Callable[[Conn], Awaitable[T]], *, write: bool = True) -> T:
        """Run ``body`` in one transaction (or, for reads, one autocommit
        statement batch), with the retry rules in the module docstring."""
        await self.open()
        dropped = deadlocks = 0
        while True:
            conn: Conn | None = None
            committing = False
            try:
                async with self._pool.connection() as conn:
                    if not write:
                        return await body(conn)
                    async with conn.transaction():
                        result = await body(conn)
                        committing = True
                    return result
            except PoolTimeout as e:
                raise StoreUnavailable(f"no postgres connection: {e}") from e
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                if lost(conn, e):
                    if committing:
                        raise CommitUnknown(f"connection lost during COMMIT: {e}") from e
                    if dropped:
                        raise StoreUnavailable(f"postgres connection lost: {e}") from e
                    dropped += 1
                    continue
                if getattr(e, "sqlstate", None) in RETRY_SQLSTATES and \
                        deadlocks < DEADLOCK_RETRIES:
                    deadlocks += 1
                    await asyncio.sleep(0.005 * deadlocks)
                    continue
                # Any other operational failure (a cancelled statement, a full
                # disk): nothing committed; the caller may try again later.
                raise StoreUnavailable(f"postgres: {e}") from e

    async def close(self) -> None:
        self._closed = True
        await self._feed.close()
        await self._pool.close()
        self._opened = False
