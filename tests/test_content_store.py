"""InMemoryContentStore: content-addressed, immutable, dedup-by-hash."""

from __future__ import annotations

import hashlib

import pytest

from noeta.protocols.errors import ContentNotFound
from noeta.storage.memory import InMemoryContentStore


def test_put_returns_content_addressed_ref() -> None:
    store = InMemoryContentStore()
    body = b"hello world"
    ref = store.put(body, media_type="text/plain")

    assert ref.hash == hashlib.sha256(body).hexdigest()
    assert ref.size == len(body)
    assert ref.media_type == "text/plain"


def test_put_then_get_returns_original_bytes() -> None:
    store = InMemoryContentStore()
    body = b"some payload"
    ref = store.put(body, media_type="application/octet-stream")

    fetched = store.get(ref)

    assert fetched == body


def test_put_same_bytes_twice_dedupes_to_same_ref() -> None:
    store = InMemoryContentStore()
    body = b"identical"

    ref1 = store.put(body, media_type="application/json")
    ref2 = store.put(body, media_type="application/json")

    assert ref1 == ref2
    # Only one slot is occupied even though we wrote twice.
    assert len(store) == 1


def test_get_unknown_hash_raises_content_not_found() -> None:
    store = InMemoryContentStore()
    from noeta.protocols.values import ContentRef

    bogus = ContentRef(hash="0" * 64, size=0, media_type="text/plain")

    with pytest.raises(ContentNotFound):
        store.get(bogus)


def test_store_is_immutable_same_hash_must_match_existing_body() -> None:
    """A correctly hashed body cannot disagree with the existing body —
    sha256 collision is assumed impossible in tests. We assert that writing
    the same bytes is a no-op and does not corrupt the store."""
    store = InMemoryContentStore()
    body = b"X"
    ref1 = store.put(body, media_type="text/plain")
    ref2 = store.put(body, media_type="text/plain")
    assert store.get(ref1) == store.get(ref2) == body
