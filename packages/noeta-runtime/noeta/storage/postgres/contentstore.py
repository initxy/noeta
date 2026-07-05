"""``PostgresContentStore`` — psycopg-backed adapter for the L0 ContentStore.

Behaviour pinned by :class:`noeta.storage.memory.InMemoryContentStore`
(same contract as the sqlite adapter): content-addressed via SHA-256,
immutable, **hash-only** dedup; the ``media_type`` carried on the
returned :class:`ContentRef` is the value passed to the current ``put``
call, not whatever was recorded on the stored row.

Single psycopg connection + :class:`threading.Lock`, mirroring the
EventLog adapter's concurrency model. Each ``put`` is a single
``INSERT ... ON CONFLICT (hash) DO NOTHING`` (atomic first-write-wins
dedup, no explicit transaction needed under autocommit). Each ``get``
is a single SELECT under the same lock.
"""

from __future__ import annotations

import hashlib
import threading
from types import TracebackType
from typing import Optional

from noeta.protocols.errors import ContentNotFound
from noeta.protocols.values import ContentRef
from noeta.storage.postgres._connection import _open_connection
from noeta.storage.postgres.migrations import apply_migrations


__all__ = ["PostgresContentStore"]


class PostgresContentStore:
    """psycopg implementation of the ``ContentStore`` L0 Protocol.

    Public surface is exactly the Protocol (``put`` + ``get``) plus
    lifecycle helpers (``close`` + context manager) that the L0
    contract does not enumerate.
    """

    def __init__(self, dsn: str) -> None:
        self._conn = _open_connection(dsn)
        apply_migrations(self._conn)
        self._lock = threading.Lock()
        self._closed = False

    # -- ContentStore Protocol ------------------------------------------

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        digest = hashlib.sha256(body).hexdigest()
        size = len(body)
        with self._lock:
            # ``ON CONFLICT DO NOTHING`` keeps the first-write-wins
            # semantics the InMemory adapter has (``setdefault``-style):
            # if a row for this hash already exists, the body and
            # recorded media_type are left untouched. The returned
            # ContentRef always carries the caller's ``media_type``
            # (hash-only storage identity, descriptive metadata).
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

    # -- lifecycle (adapter-only, not on Protocol) ----------------------

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
