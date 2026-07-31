"""``SqliteContentStore`` — sqlite3-backed adapter for the L0 ContentStore.

Issue 16. Second persistent backend on the same sqlite file that
``SqliteEventLog`` opens (migration 2 adds the ``content`` table).
Behaviour is pinned by :class:`noeta.storage.memory.InMemoryContentStore`:
content-addressed via SHA-256, immutable, **hash-only** dedup; the
``media_type`` carried on the returned :class:`ContentRef` is the
value passed to the current ``put`` call, not whatever was recorded
on the stored row (see ``noeta.protocols.content_store.ContentStore``
docstring for the full contract).

Single :class:`sqlite3.Connection` + :class:`threading.Lock`, mirroring
the EventLog adapter's concurrency model. Each ``put`` is a single
``INSERT OR IGNORE`` (PRIMARY KEY ``hash`` provides atomic dedup, no
explicit transaction needed). Each ``get`` is a single SELECT under
the same lock.
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


#: Host parameters per ``get_many`` statement. Kept below the 999 floor of
#: pre-3.32 sqlite builds so the batch read works on every interpreter's
#: bundled library, not just a recent one.
_IN_CHUNK = 900


class SqliteContentStore:
    """sqlite3 implementation of the ``ContentStore`` L0 Protocol.

    Public surface is exactly the Protocol (``put`` + ``get``) plus
    lifecycle helpers (``close`` + context manager) that the L0
    contract does not enumerate. Any debug helpers stay underscore-
    private; the ``storage-adapters-isolated`` import-linter contract
    forbids production code from reaching across the Protocol anyway.
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
            # ``INSERT OR IGNORE`` keeps the first-write-wins semantics
            # the InMemory adapter has (``setdefault``-style): if a row
            # for this hash already exists, the body and recorded
            # media_type are left untouched. The returned ContentRef
            # always carries the caller's ``media_type`` (issue 16 §11
            # contract — hash-only storage identity, descriptive
            # metadata).
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
        statement (``SQLITE_MAX_VARIABLE_NUMBER``: 999 on builds predating
        3.32, 32766 after). A fold tail or a message projection stays well
        under one chunk in practice; the loop is the correctness floor for the
        long-history case, not the expected path.

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
