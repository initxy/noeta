"""ContentStore Protocol — the typed boundary for content-addressed blobs.

ContentStore is one half of Noeta's source of truth: EventLog holds decisions
and refs, ContentStore holds the large bodies they point at (LLM responses,
tool outputs, snapshot bodies, provider documents). The surface stays at
``put`` + ``get`` + ``get_many`` — deletion is a GC concern that belongs to the
adapter, existence is implicit in ``get`` raising ``ContentNotFound``, and
listing is a debug helper.
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
      and ``ContentRef.hash`` is hex-encoded SHA-256.
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

        Same hash-only lookup as ``get``, and duplicate hashes in ``refs``
        collapse to one entry.

        **Missing hashes are omitted, not raised** — the one deliberate
        divergence from ``get``: a body that fell outside the GC window must
        not abort the other N-1 reads. A caller that wants the raising
        behaviour re-``get``\\ s the ref it did not find.

        Required, not optional: the traversals that dominate read cost know
        every ref they will dereference before they touch a body, so one
        round-trip is always available — but only a backend can express it
        (``WHERE hash IN (...)``). An adapter answering this with a loop over
        ``get`` would silently cost N round-trips at the call sites that chose
        the batch API for the opposite reason.
        """
        ...
