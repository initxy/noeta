"""``PostgresContentStore`` — psycopg-backed adapter for the L0 ContentStore.

Content-addressed via SHA-256, immutable, **hash-only** dedup: a stored row
is never updated, and the ``media_type`` on a returned :class:`ContentRef` is
the value passed to that ``put`` call rather than whatever the row recorded.
A single connection guarded by a :class:`threading.Lock` carries every
statement, so ``put`` is one ``INSERT ... ON CONFLICT (hash) DO NOTHING`` —
first-write-wins dedup stays atomic without an explicit transaction.
"""

from __future__ import annotations

import hashlib
import threading
from types import TracebackType
from typing import Iterable, Optional

from noeta.protocols.errors import ContentNotFound
from noeta.protocols.values import ContentRef
from noeta.builtins.storage.impl.postgres._connection import _open_connection
from noeta.builtins.storage.impl.postgres.migrations import apply_migrations


__all__ = ["PostgresContentStore"]


class PostgresContentStore:
    """psycopg implementation of the ``ContentStore`` L0 Protocol.

    Beyond the Protocol it exposes only lifecycle helpers (``close`` and the
    context manager), which the L0 contract does not enumerate.
    """

    def __init__(self, dsn: str) -> None:
        self._conn = _open_connection(dsn)
        apply_migrations(self._conn)
        self._lock = threading.Lock()
        self._closed = False

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        digest = hashlib.sha256(body).hexdigest()
        size = len(body)
        with self._lock:
            self._conn.execute(
                "INSERT INTO content ("
                " hash, size, media_type, body"
                ") VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (hash) DO NOTHING",
                (digest, size, media_type, body),
            )
        return ContentRef(hash=digest, size=size, media_type=media_type)

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
