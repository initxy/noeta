"""Traversal-scoped content prefetch: batch the reads, keep the handlers.

An event-stream traversal can fetch every body it needs in one round-trip
because each ``ContentRef`` sits in a payload already in memory and no body's
content determines another body's ref. Wrapping the store — rather than
threading a body map through the reducer table — keeps the several dozen
per-event handlers calling ``get`` on a store that already holds the answer.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from noeta.protocols.content_store import ContentStore
from noeta.protocols.values import ContentRef


__all__ = ["PrefetchedContentStore", "prefetched"]


class PrefetchedContentStore:
    """A ContentStore view that answers ``get`` from an already-fetched map.

    A ref the scan did not predict falls through to the inner store,
    ``ContentNotFound`` included, so a ref scanner that misses an event type
    costs a round-trip and never correctness.

    **Not a cache**: the map is built by one caller for one traversal and dies
    with it. Reuse across traversals is
    :class:`noeta.storage.cached.CachedContentStore`.
    """

    __slots__ = ("_bodies", "_inner")

    def __init__(self, inner: ContentStore, bodies: dict[str, bytes]) -> None:
        self._inner = inner
        self._bodies = bodies

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        return self._inner.put(body, media_type=media_type)

    def get(self, ref: ContentRef) -> bytes:
        body = self._bodies.get(ref.hash)
        if body is None:
            return self._inner.get(ref)
        return body

    def get_many(self, refs: Iterable[ContentRef]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        missing: list[ContentRef] = []
        for ref in refs:
            body = self._bodies.get(ref.hash)
            if body is None:
                missing.append(ref)
            else:
                out[ref.hash] = body
        if missing:
            out.update(self._inner.get_many(missing))
        return out


def prefetched(store: ContentStore, refs: Sequence[ContentRef]) -> ContentStore:
    """Batch-read ``refs`` and return a store view that serves them locally.

    Returns ``store`` unchanged when there is nothing to fetch, so a traversal
    with no content-bearing events pays neither a query nor a wrapper.
    """
    if not refs:
        return store
    return PrefetchedContentStore(store, store.get_many(refs))
