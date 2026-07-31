"""``CachedContentStore`` — a byte-bounded LRU in front of a durable backend.

It collapses the round-trips ``get_many`` cannot reach: the *same* ref read by
*different* traversals, which is where a running session actually spends its
reads — every compose re-derefs each active resident's body, and every fold
re-reads the same snapshot baseline and message tail. Correctness is free
because the store is content-addressed and immutable, so a hash maps to one
body forever and there is no invalidation rule to get wrong. The bound is on
bytes rather than entries, since one tool output can outweigh a thousand
message bodies, and a body over ``max_entry_bytes`` is never admitted at all
rather than evicting the whole working set to hold something read once. Only
the durable backends are wrapped — the InMemory backend already *is* the cache.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Iterable

from noeta.protocols.content_store import ContentStore
from noeta.protocols.values import ContentRef


__all__ = ["CachedContentStore"]


#: Sized to hold a long session's message and resident bodies without becoming
#: a memory line item next to the process itself.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

#: Bodies this large — big tool outputs, snapshot bodies — are read once by the
#: traversal that asked for them, so admitting them only costs residency.
DEFAULT_MAX_ENTRY_BYTES = 1024 * 1024


class CachedContentStore:
    """LRU read cache over any :class:`ContentStore`.

    The wrapped adapters are shared across worker threads, so the cache must be
    thread-safe too. Its lock covers only the bookkeeping — never the inner
    store's IO.
    """

    __slots__ = (
        "_bytes",
        "_cache",
        "_inner",
        "_lock",
        "_max_bytes",
        "_max_entry_bytes",
    )

    def __init__(
        self,
        inner: ContentStore,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
    ) -> None:
        self._inner = inner
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._bytes = 0
        self._max_bytes = max_bytes
        self._max_entry_bytes = max_entry_bytes
        self._lock = threading.Lock()

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        ref = self._inner.put(body, media_type=media_type)
        # The write path is a read predictor: the Engine snapshots then
        # refolds, so every body appended here is read back by the next fold
        # of the same tail.
        self._admit(ref.hash, body)
        return ref

    def get(self, ref: ContentRef) -> bytes:
        hit = self._take(ref.hash)
        if hit is not None:
            return hit
        body = self._inner.get(ref)
        self._admit(ref.hash, body)
        return body

    def get_many(self, refs: Iterable[ContentRef]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        missing: list[ContentRef] = []
        for ref in refs:
            hit = self._take(ref.hash)
            if hit is None:
                missing.append(ref)
            else:
                out[ref.hash] = hit
        if missing:
            fetched = self._inner.get_many(missing)
            for hash_, body in fetched.items():
                self._admit(hash_, body)
            out.update(fetched)
        return out

    def _take(self, hash_: str) -> bytes | None:
        with self._lock:
            body = self._cache.get(hash_)
            if body is not None:
                self._cache.move_to_end(hash_)
            return body

    def _admit(self, hash_: str, body: bytes) -> None:
        size = len(body)
        if size > self._max_entry_bytes:
            return
        with self._lock:
            previous = self._cache.pop(hash_, None)
            if previous is not None:
                self._bytes -= len(previous)
            self._cache[hash_] = body
            self._bytes += size
            while self._bytes > self._max_bytes and len(self._cache) > 1:
                _evicted_hash, evicted = self._cache.popitem(last=False)
                self._bytes -= len(evicted)
