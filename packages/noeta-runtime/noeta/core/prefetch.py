"""Traversal-scoped content prefetch: batch the reads, keep the handlers.

``fold`` and the ``as_messages`` projection both walk an event stream and
dereference bodies as they go. Both *can* read in one round-trip, because
nothing they deref is discovered by reading: every ``ContentRef`` sits in an
event payload that is already in memory, and no body's content determines
another body's ref. So each scans its stream for the refs it is about to need,
pulls them in one ``ContentStore.get_many``, and walks the stream against the
view built here.

What the view buys is that the per-event handlers do not change. fold routes
through several dozen of them, each taking a ``ContentStore`` and calling
``get``; rewriting them to accept a body map would spread the batching across
the whole reducer table. Instead they keep calling ``get`` on a store that
already holds the answer.

Lives in ``noeta.core`` rather than ``noeta.protocols`` so the L0 boundary
stays what it is — Protocols, dataclasses and errors, no behaviour — and
rather than in ``noeta.storage`` because ``noeta.core`` may not import it
(``storage-adapters-isolated``). Both consumers reach it from here: fold is a
sibling, and ``noeta.client.messages`` already sits above ``noeta.core``.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from noeta.protocols.content_store import ContentStore
from noeta.protocols.values import ContentRef


__all__ = ["PrefetchedContentStore", "prefetched"]


class PrefetchedContentStore:
    """A ContentStore view that answers ``get`` from an already-fetched map.

    A ref the scan did not predict is not an error — it falls through to the
    inner store, which answers it exactly as before, ``ContentNotFound``
    included. That is what makes the ref scanners safe to evolve: one that
    misses an event type costs a round-trip, never correctness.

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

    The one-line form of the pattern. Returns ``store`` unchanged when there is
    nothing to fetch, so a traversal with no content-bearing events pays
    neither a query nor a wrapper.
    """
    if not refs:
        return store
    return PrefetchedContentStore(store, store.get_many(refs))
