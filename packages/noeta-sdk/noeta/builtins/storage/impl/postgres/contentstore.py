"""``PostgresContentStore`` — psycopg-backed adapter for the L0 ContentStore.

Content-addressed via SHA-256, immutable, **hash-only** dedup: a stored row
is never updated, and the ``media_type`` on a returned :class:`ContentRef` is
the value passed to that ``put`` call rather than whatever the row recorded.
A single connection guarded by a :class:`threading.Lock` carries every
statement, so ``put`` is one ``INSERT ... ON CONFLICT (hash) DO NOTHING`` —
first-write-wins dedup stays atomic without an explicit transaction.
"""

from __future__ import annotations

import threading
from types import TracebackType
from typing import Collection, Iterable, Optional

from noeta.protocols.content_store import SweepOutcome, content_ref_for
from noeta.protocols.errors import ContentNotFound
from noeta.protocols.values import ContentRef
from noeta.builtins.storage.impl.postgres._connection import (
    _DB_NOW_SQL,
    _open_connection,
)
from noeta.builtins.storage.impl.postgres.migrations import apply_migrations


__all__ = ["PostgresContentStore"]


#: The age predicate a sweep candidate must satisfy, against the database
#: clock (several hosts share one store, so no host clock is the reference).
#: Evaluated once to list candidates and again inside each DELETE, so a row
#: an upsert refreshed in between is kept. NULL = written before the column
#: existed = as old as it gets.
_STALE_SQL = f"(touched_at IS NULL OR touched_at < {_DB_NOW_SQL} - %s)"

#: Hashes per DELETE statement; bounds the size of one ``= ANY`` parameter and
#: of one statement's row-lock footprint.
_DELETE_CHUNK = 900


class PostgresContentStore:
    """psycopg implementation of the ``ContentStore`` L0 Protocol.

    Beyond the Protocol it exposes the ``sweep`` maintenance affordance
    (:mod:`noeta.storage.gc`) and the lifecycle helpers (``close`` and the
    context manager), which the L0 contract does not enumerate.
    """

    def __init__(self, dsn: str) -> None:
        self._conn = _open_connection(dsn)
        apply_migrations(self._conn)
        self._lock = threading.Lock()
        self._closed = False

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        ref = content_ref_for(body, media_type=media_type)
        with self._lock:
            # First-write-wins on body and media_type; only ``touched_at``
            # moves on a dedup hit, stamped from the database clock so every
            # host sharing the store measures a row's age the same way.
            self._conn.execute(
                "INSERT INTO content ("
                " hash, size, media_type, body, touched_at"
                f") VALUES (%s, %s, %s, %s, {_DB_NOW_SQL}) "
                "ON CONFLICT (hash) DO UPDATE SET touched_at = EXCLUDED.touched_at",
                (ref.hash, ref.size, media_type, body),
            )
        return ref

    def sweep(
        self,
        live: Collection[str],
        *,
        grace_seconds: float,
        vacuum: bool = False,
    ) -> SweepOutcome:
        """Delete every row not in ``live`` whose last ``put`` is older than
        ``grace_seconds`` by the database clock; ``vacuum=True`` then runs a
        plain ``VACUUM content`` so the space is reusable (returning it to
        the OS is ``VACUUM FULL``, an operator's call — it locks the table).

        Each DELETE re-evaluates the age predicate: under ``READ COMMITTED``
        a row a concurrent upsert just refreshed is re-checked after that
        upsert commits and kept, and an upsert that arrives after the DELETE
        committed re-inserts the body. Either order keeps the running turn's
        body.
        """
        swept = 0
        freed = 0
        with self._lock:
            candidates = [
                (str(row["hash"]), int(row["size"]))
                for row in self._conn.execute(
                    f"SELECT hash, size FROM content WHERE {_STALE_SQL}",
                    (grace_seconds,),
                ).fetchall()
            ]
            dead = [h for h, _ in candidates if h not in live]
            for start in range(0, len(dead), _DELETE_CHUNK):
                chunk = dead[start : start + _DELETE_CHUNK]
                rows = self._conn.execute(
                    f"DELETE FROM content WHERE hash = ANY(%s) AND {_STALE_SQL} "
                    "RETURNING size",
                    (chunk, grace_seconds),
                ).fetchall()
                swept += len(rows)
                freed += sum(int(row["size"]) for row in rows)
            vacuumed = False
            if vacuum:
                # The connection is autocommit, which VACUUM requires.
                self._conn.execute("VACUUM content")
                vacuumed = True
        return SweepOutcome(rows=swept, bytes=freed, vacuumed=vacuumed)

    def get(self, ref: ContentRef) -> bytes:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM content WHERE hash = %s", (ref.hash,)
            ).fetchone()
        if row is None:
            raise ContentNotFound(ref.hash)
        return bytes(row["body"])

    def get_many(self, refs: Iterable[ContentRef]) -> dict[str, bytes]:
        """One ``hash = ANY(...)`` round-trip instead of one SELECT per ref.

        Each single ``get`` costs a network round-trip *and* an acquisition of
        the adapter's one connection lock, so a caller reading a whole fold
        tail holds that lock once here instead of N times. ``= ANY(array)``
        rather than ``IN (...)``: the hash list binds as a single array
        parameter, so no host-parameter ceiling has to be chunked around and
        every batch size shares one statement shape. Missing hashes are
        omitted per the Protocol contract.
        """
        hashes = list(dict.fromkeys(ref.hash for ref in refs))
        if not hashes:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT hash, body FROM content WHERE hash = ANY(%s)",
                (hashes,),
            ).fetchall()
        return {str(row["hash"]): bytes(row["body"]) for row in rows}

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._conn.close()
        finally:
            self._closed = True

    def __enter__(self) -> "PostgresContentStore":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()
