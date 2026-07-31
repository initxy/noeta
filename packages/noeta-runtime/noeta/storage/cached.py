"""``CachedContentStore`` — a byte-bounded LRU in front of a durable backend.

The read amplification the batch API cannot reach. ``get_many`` collapses the
refs of *one* traversal into one query; this collapses the *same ref read by
different traversals*, which is where a running session actually spends its
round-trips:

* ``ContextComposer`` re-derefs every active resident's body on **every
  compose** (``_content_resolve``) — the same skill / instructions / memory
  hashes, once per model turn, for the life of the task.
* ``fold`` is called from a dozen sites per step (driver, worker, engine
  refold); each call re-reads the same snapshot baseline and the same message
  bodies from the same tail.

Correctness is free here: the store is content-addressed and immutable, so a
hash either maps to one body forever or is GC'd — there is no invalidation
rule to get wrong, and no staleness window. A hit on a body the backend has
since reclaimed is *more* available than the backend, never less consistent.

Bounded by **bytes, not entries**: entry counts say nothing about footprint
when one tool output can outweigh a thousand message bodies. Bodies above
``max_entry_bytes`` are not cached at all — a single large snapshot body would
otherwise evict the entire working set to hold something read once.

Wired by ``noeta.client.storage_resolve`` around the durable backends only.
The InMemory backend is already the cache; wrapping it would just hold every
body twice.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Iterable

from noeta.protocols.content_store import ContentStore
from noeta.protocols.values import ContentRef


__all__ = ["CachedContentStore"]


#: Total cached body bytes. Sized to hold a long session's message + resident
#: bodies without being a memory line item next to the process itself.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

#: Per-body ceiling. Above this a body is served straight through: big tool
#: outputs and snapshot bodies are read once by the traversal that asked for
#: them, so admitting them only costs the working set its residency.
DEFAULT_MAX_ENTRY_BYTES = 1024 * 1024


class CachedContentStore:
    """LRU read cache over any :class:`ContentStore`.

    Thread-safe: the adapters it wraps serialise on their own lock and are
    shared across worker threads, so the cache must be too. The lock covers
    only the bookkeeping — never the inner store's IO.
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

    # -- ContentStore Protocol ------------------------------------------

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        ref = self._inner.put(body, media_type=media_type)
        # The write path is a read predictor: the Engine snapshots then
        # refolds, and every message body it appends is read back by the next
        # fold of the same tail.
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

    # -- internals -------------------------------------------------------

    def _take(self, hash_: str) -> bytes | None:
        """Return a cached body and mark it most-recently-used."""
        with self._lock:
            body = self._cache.get(hash_)
            if body is not None:
                self._cache.move_to_end(hash_)
            return body

    def _admit(self, hash_: str, body: bytes) -> None:
        """Cache ``body`` unless it is too large, evicting LRU to fit."""
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
