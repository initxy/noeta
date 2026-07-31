"""``SqliteContentStore`` — sqlite3-backed adapter for the L0 ContentStore.

Shares the sqlite file the sibling adapters open; migration 2 owns the
``content`` table. Behaviour is pinned by
:class:`noeta.storage.memory.InMemoryContentStore`: content-addressed via
SHA-256, immutable, **hash-only** dedup, and the ``media_type`` on the
returned :class:`ContentRef` is the value this ``put`` call passed, not
whatever the stored row recorded. One :class:`sqlite3.Connection` under one
:class:`threading.Lock`, mirroring the EventLog adapter; ``put`` needs no
explicit transaction because the ``hash`` PRIMARY KEY already makes
``INSERT OR IGNORE`` atomic dedup.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import TracebackType
from typing import Iterable, Optional, Union

from noeta.protocols.errors import ContentNotFound
from noeta.protocols.values import ContentRef
from noeta.builtins.storage.impl.sqlite._connection import _open_connection
from noeta.builtins.storage.impl.sqlite.migrations import apply_migrations


__all__ = ["SqliteContentStore"]


#: Host parameters per ``get_many`` statement. Kept below the 999 floor that
#: older sqlite builds impose so the batch read works against whatever library
#: the interpreter happens to bundle.
_IN_CHUNK = 900


class SqliteContentStore:
    """sqlite3 implementation of the ``ContentStore`` L0 Protocol.

    The public surface is exactly the Protocol plus the lifecycle helpers
    (``close`` + context manager) the L0 contract does not enumerate; debug
    helpers stay underscore-private, since production code may only reach this
    class through the Protocol.
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self._conn = _open_connection(path)
        apply_migrations(self._conn)
        self._lock = threading.Lock()
        self._closed = False

    # -- ContentStore Protocol ------------------------------------------

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        digest = hashlib.sha256(body).hexdigest()
        size = len(body)
        with self._lock:
            # ``INSERT OR IGNORE`` gives the first-write-wins semantics the
            # InMemory adapter has: an existing row for this hash keeps its
            # body and its recorded media_type. The returned ContentRef still
            # carries the caller's ``media_type`` — storage identity is the
            # hash alone, media_type is descriptive metadata.
            self._conn.execute(
                "INSERT OR IGNORE INTO content ("
                " hash, size, media_type, body"
                ") VALUES (?, ?, ?, ?)",
                (digest, size, media_type, body),
            )
        return ContentRef(hash=digest, size=size, media_type=media_type)

    def get(self, ref: ContentRef) -> bytes:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM content WHERE hash = ?", (ref.hash,)
            ).fetchone()
        if row is None:
            raise ContentNotFound(ref.hash)
        return bytes(row["body"])

    def get_many(self, refs: Iterable[ContentRef]) -> dict[str, bytes]:
        """One ``WHERE hash IN (...)`` per chunk instead of one SELECT per ref.

        Chunked at :data:`_IN_CHUNK` because sqlite caps host parameters per
        statement (``SQLITE_MAX_VARIABLE_NUMBER``). A fold tail or a message
        projection stays well under one chunk in practice; the loop is the
        correctness floor for the long-history case, not the expected path.

        Missing hashes are omitted per the Protocol contract.
        """
        hashes = list(dict.fromkeys(ref.hash for ref in refs))
        if not hashes:
            return {}
        out: dict[str, bytes] = {}
        with self._lock:
            for start in range(0, len(hashes), _IN_CHUNK):
                chunk = hashes[start : start + _IN_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT hash, body FROM content WHERE hash IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    out[str(row["hash"])] = bytes(row["body"])
        return out

    # -- lifecycle (adapter-only, not on Protocol) ----------------------

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._conn.close()
        finally:
            self._closed = True

    def __enter__(self) -> "SqliteContentStore":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()
