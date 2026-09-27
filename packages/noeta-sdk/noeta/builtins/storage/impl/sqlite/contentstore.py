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

import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Callable, Collection, Iterable, Optional, Union

from noeta.protocols.content_store import SweepOutcome, content_ref_for
from noeta.protocols.errors import ContentNotFound
from noeta.protocols.values import ContentRef
from noeta.builtins.storage.impl.sqlite._connection import _open_connection
from noeta.builtins.storage.impl.sqlite.migrations import apply_migrations


__all__ = ["SqliteContentStore"]


#: Host parameters per ``get_many`` statement. Kept below the 999 floor that
#: older sqlite builds impose so the batch read works against whatever library
#: the interpreter happens to bundle.
_IN_CHUNK = 900

#: The age predicate a sweep candidate must satisfy, evaluated twice: once to
#: list candidates, and again inside each DELETE under the adapter lock so a
#: ``put`` that refreshed a row in between keeps it. A legacy row (NULL,
#: written before migration 12) is as old as it gets.
_STALE_SQL = "(touched_at IS NULL OR touched_at < ?)"


class SqliteContentStore:
    """sqlite3 implementation of the ``ContentStore`` L0 Protocol.

    The public surface is the Protocol, the ``sweep`` maintenance affordance
    (:mod:`noeta.storage.gc`) and the lifecycle helpers (``close`` + context
    manager) the L0 contract does not enumerate; debug helpers stay
    underscore-private, since production code may only reach this class
    through the Protocol.
    """

    def __init__(
        self, path: Union[str, Path], *, clock: Callable[[], float] = time.time
    ) -> None:
        self._conn = _open_connection(path)
        apply_migrations(self._conn)
        self._clock = clock
        self._lock = threading.Lock()
        self._closed = False

    # -- ContentStore Protocol ------------------------------------------

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        ref = content_ref_for(body, media_type=media_type)
        now = self._clock()
        with self._lock:
            # First-write-wins on the body and the recorded media_type, as the
            # InMemory adapter has it; only ``touched_at`` moves on a dedup
            # hit, so a sweep can tell "re-used just now" from "orphaned for
            # weeks". The returned ContentRef still carries the caller's
            # ``media_type`` — storage identity is the hash alone.
            self._conn.execute(
                "INSERT INTO content ("
                " hash, size, media_type, body, touched_at"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (hash) DO UPDATE SET touched_at = excluded.touched_at",
                (ref.hash, ref.size, media_type, body, now),
            )
        return ref

    # -- maintenance (adapter-only, not on Protocol) --------------------

    def sweep(
        self,
        live: Collection[str],
        *,
        grace_seconds: float,
        vacuum: bool = False,
    ) -> SweepOutcome:
        """Delete every row not in ``live`` whose last ``put`` is older than
        ``grace_seconds``; ``vacuum=True`` then rewrites the file so the
        freed pages leave the disk.

        The candidate list and every DELETE evaluate the same age predicate,
        and the DELETE runs under the lock ``put`` takes, so a body a running
        turn re-``put`` after the candidates were read is refreshed before the
        DELETE sees it and survives. ``VACUUM`` holds the database's write
        lock for the whole rewrite — the sibling adapters' connections wait up
        to their ``busy_timeout`` and then fail — so it belongs in a quiet
        window, not in every sweep.
        """
        cutoff = self._clock() - grace_seconds
        swept = 0
        freed = 0
        with self._lock:
            candidates = [
                (str(row["hash"]), int(row["size"]))
                for row in self._conn.execute(
                    f"SELECT hash, size FROM content WHERE {_STALE_SQL}",
                    (cutoff,),
                ).fetchall()
            ]
            dead = [(h, size) for h, size in candidates if h not in live]
            for start in range(0, len(dead), _IN_CHUNK):
                chunk = dead[start : start + _IN_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                hashes = [h for h, _ in chunk]
                cursor = self._conn.execute(
                    f"DELETE FROM content WHERE hash IN ({placeholders}) "
                    f"AND {_STALE_SQL}",
                    [*hashes, cutoff],
                )
                swept += cursor.rowcount
                if cursor.rowcount == len(chunk):
                    freed += sum(size for _, size in chunk)
                    continue
                # A concurrent put refreshed some of these; only count the
                # ones that actually went.
                kept = {
                    str(row["hash"])
                    for row in self._conn.execute(
                        f"SELECT hash FROM content WHERE hash IN ({placeholders})",
                        hashes,
                    ).fetchall()
                }
                freed += sum(size for h, size in chunk if h not in kept)
            vacuumed = False
            if vacuum:
                # ``isolation_level=None`` means no transaction is open here,
                # which VACUUM requires.
                self._conn.execute("VACUUM")
                vacuumed = True
        return SweepOutcome(rows=swept, bytes=freed, vacuumed=vacuumed)

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
