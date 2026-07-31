"""ContentStore Protocol — L0 typed boundary for content-addressed blobs.

ContentStore is paired with EventLog as Noeta's dual source of
truth: EventLog stores decisions and refs, ContentStore stores the
large bodies (LLM responses, tool outputs, snapshot bodies, provider
documents). The Protocol is lifted to L0.

The Protocol is intentionally minimal — ``put`` + ``get`` + ``get_many``, no
``delete``, no ``exists``, no ``list``. Deletion is a GC concern
(refs stay reachable as long as a fold / resume needs them) and
lives on the adapter, not the Protocol. Existence is implicit in ``get`` raising
``ContentNotFound``. Listing is a debug helper at the adapter level.

``get_many`` is the batch read. It earns its place on the Protocol because the
two traversals that dominate read cost — ``fold`` over an event tail and the
``as_messages`` projection over a whole stream — know every ref they will
dereference *before* they process a single event (the refs sit in the event
payloads, and no body's content determines another body's ref). One round-trip
for the whole traversal is therefore always available, and only the backend
knows how to express it (``WHERE hash IN (...)`` / ``= ANY``); a loop over
``get`` in shared code could not.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from noeta.protocols.values import ContentRef


__all__ = ["ContentStore"]


class ContentStore(Protocol):
    """Content-addressed immutable blob store.

    Contracts:

    * **Content-addressed**: ``put(body, media_type=mt).hash`` is a
      deterministic function of ``body`` (and the hash algorithm), so
      identical bodies always produce equal refs.
    * **Hash-only dedup**: dedup keys on ``hash`` only. Putting the
      same bytes with a different ``media_type`` does **not** create
      a second row; the first put's ``media_type`` is recorded on the
      stored row, while each call returns a fresh :class:`ContentRef`
      carrying the caller's requested ``media_type``.
    * **Immutable**: a put with an existing ``hash`` is a no-op on the
      underlying storage; the originally-stored body is what
      subsequent ``get`` calls return, unchanged.
    * **``get`` is hash-only**: ``ContentRef.size`` and
      ``ContentRef.media_type`` are **not** validated against the
      stored row. ``get`` returns the body associated with
      ``ref.hash``; callers that need to verify ref consistency
      must do so themselves.
    * **Stable ContentRef**: ``ContentRef.size`` equals ``len(body)``
      and ``ContentRef.hash`` is hex-encoded SHA-256 in Phase 1.
    """

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        """Store ``body``; return a stable :class:`ContentRef`.

        Idempotent: putting the same ``body`` twice returns refs with
        equal ``hash`` / ``size`` (and the second put is a no-op on
        the underlying storage). The returned ref's ``media_type`` is
        always the caller's argument, not whatever was recorded on
        the existing row.
        """
        ...

    def get(self, ref: ContentRef) -> bytes:
        """Return the body for ``ref.hash``.

        Only ``ref.hash`` is consulted; ``ref.size`` and
        ``ref.media_type`` are not cross-checked against the stored
        row.

        Raises:
            noeta.protocols.errors.ContentNotFound — ``ref.hash`` is not
                in this store. Backends MAY garbage-collect refs that
                are outside the fold / resume window.
        """
        ...

    def get_many(self, refs: Iterable[ContentRef]) -> dict[str, bytes]:
        """Batch counterpart of ``get``: return ``{hash: body}``.

        Same hash-only lookup as ``get`` (``size`` / ``media_type`` are not
        consulted), and duplicate hashes in ``refs`` collapse to one entry.

        **Missing hashes are omitted, not raised.** This is the one place the
        batch read deliberately diverges from ``get``: a body that fell outside
        the GC window must not abort the other N-1 reads. A caller that needs
        the raising behaviour re-``get``\\ s the ref it did not find, so a
        partial batch degrades to the per-ref path with its error semantics
        intact.

        Backends implement this as a single query. It is a required member: an
        adapter that answered it with a Python loop over ``get`` would silently
        cost N round-trips at the call sites that chose the batch API for the
        opposite reason.
        """
        ...
