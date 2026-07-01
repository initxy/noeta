"""Storage-backend-neutral ContentStore contract.

Issue 16 introduces the second ContentStore adapter (`SqliteContentStore`)
on top of the existing `InMemoryContentStore`. This module runs the
behavioural contract — content-addressed put, hash-only dedup,
``media_type`` returned from caller, ``get`` hash-only lookup,
``ContentNotFound`` — against **both** backends.

Existing `test_content_store.py` keeps its InMemory-specific case
coverage; this suite adds the behavioural contract that every adapter
satisfies.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from noeta.protocols.errors import ContentNotFound
from noeta.protocols.values import ContentRef
from noeta.storage.memory import InMemoryContentStore
from noeta.storage.sqlite.contentstore import SqliteContentStore


def _make_in_memory():
    return InMemoryContentStore()


def _make_sqlite():
    return SqliteContentStore(":memory:")


@pytest.fixture(params=["memory", "sqlite"])
def make_store(request):
    if request.param == "memory":
        builder = _make_in_memory
    else:
        builder = _make_sqlite

    instances: list[object] = []

    def _factory():
        store = builder()
        instances.append(store)
        return store

    yield _factory

    for store in instances:
        close = getattr(store, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------------
# Content-addressed put
# ---------------------------------------------------------------------------


def test_put_returns_sha256_hash_and_correct_size(make_store) -> None:
    store = make_store()
    body = b"hello world"
    ref = store.put(body, media_type="text/plain")
    assert ref.hash == hashlib.sha256(body).hexdigest()
    assert ref.size == len(body)
    assert ref.media_type == "text/plain"


def test_put_then_get_round_trip_preserves_byte_for_byte(make_store) -> None:
    store = make_store()
    body = b"\x00\x01\xff binary \xfe content"
    ref = store.put(body, media_type="application/octet-stream")
    assert store.get(ref) == body


def test_get_unknown_hash_raises_content_not_found(make_store) -> None:
    store = make_store()
    bogus = ContentRef(hash="0" * 64, size=0, media_type="text/plain")
    with pytest.raises(ContentNotFound):
        store.get(bogus)


# ---------------------------------------------------------------------------
# Hash-only dedup
# ---------------------------------------------------------------------------


def test_put_same_bytes_twice_dedupes_to_same_hash(make_store) -> None:
    store = make_store()
    body = b"identical"
    ref1 = store.put(body, media_type="application/json")
    ref2 = store.put(body, media_type="application/json")
    assert ref1 == ref2
    # InMemory exposes len(); SqliteContentStore does not. The contract
    # we can assert across both is "same bytes round-trip identically".
    assert store.get(ref1) == store.get(ref2) == body


def test_put_same_bytes_different_media_type_returns_caller_media_type(
    make_store,
) -> None:
    """Architecturally-pinned (issue 16 §11): dedup keys on hash only;
    the returned :class:`ContentRef` always carries the caller's
    ``media_type`` even though storage stores the first put's value.
    """
    store = make_store()
    body = b"X"
    ref1 = store.put(body, media_type="text/plain")
    ref2 = store.put(body, media_type="image/png")

    assert ref1.hash == ref2.hash
    assert ref1.media_type == "text/plain"
    assert ref2.media_type == "image/png"
    assert ref1 != ref2  # dataclass equality includes media_type
    assert store.get(ref1) == store.get(ref2) == body


# ---------------------------------------------------------------------------
# ``get`` is hash-only — caller-supplied size / media_type are ignored
# ---------------------------------------------------------------------------


def test_get_ignores_caller_supplied_size(make_store) -> None:
    store = make_store()
    ref = store.put(b"hello", media_type="text/plain")
    spoofed = ContentRef(hash=ref.hash, size=99999, media_type="text/plain")
    assert store.get(spoofed) == b"hello"


def test_get_ignores_caller_supplied_media_type(make_store) -> None:
    store = make_store()
    ref = store.put(b"hello", media_type="text/plain")
    spoofed = ContentRef(hash=ref.hash, size=ref.size, media_type="image/png")
    assert store.get(spoofed) == b"hello"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_put_zero_bytes_works(make_store) -> None:
    store = make_store()
    ref = store.put(b"", media_type="text/plain")
    assert ref.size == 0
    assert ref.hash == hashlib.sha256(b"").hexdigest()
    assert store.get(ref) == b""


def test_put_large_body_works(make_store) -> None:
    """ContentStore must accept bodies larger than the EventLog 4 KB
    cap — that cap is event-payload-only. 1 MB is enough
    to demonstrate "no surprise application-layer cap" without
    burning CI time on the GB boundary."""
    store = make_store()
    body = b"x" * (1024 * 1024)
    ref = store.put(body, media_type="application/octet-stream")
    assert ref.size == len(body)
    assert store.get(ref) == body


# ---------------------------------------------------------------------------
# Sqlite-specific: content table CHECK constraints catch bypass writes
# ---------------------------------------------------------------------------


def test_sqlite_content_table_rejects_short_hash() -> None:
    store = SqliteContentStore(":memory:")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO content (hash, size, media_type, body) "
                "VALUES (?, ?, ?, ?)",
                ("abc", 3, "text/plain", b"foo"),
            )
    finally:
        store.close()


def test_sqlite_content_table_rejects_negative_size() -> None:
    store = SqliteContentStore(":memory:")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO content (hash, size, media_type, body) "
                "VALUES (?, ?, ?, ?)",
                ("a" * 64, -1, "text/plain", b""),
            )
    finally:
        store.close()


def test_sqlite_content_table_rejects_size_body_mismatch() -> None:
    store = SqliteContentStore(":memory:")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO content (hash, size, media_type, body) "
                "VALUES (?, ?, ?, ?)",
                ("a" * 64, 99, "text/plain", b"only-3-bytes"),
            )
    finally:
        store.close()


def test_sqlite_stored_row_records_first_put_media_type() -> None:
    """When the same body is put twice with different ``media_type``,
    the stored ``content`` row keeps the **first** put's value while
    each returned :class:`ContentRef` carries the caller's argument.

    Sqlite-specific because we look at the row directly; the
    cross-backend contract that the returned ref equals the caller's
    media_type is covered by
    ``test_put_same_bytes_different_media_type_returns_caller_media_type``.
    """
    store = SqliteContentStore(":memory:")
    try:
        body = b"shared body"
        ref1 = store.put(body, media_type="text/plain")
        ref2 = store.put(body, media_type="image/png")

        # Returned refs carry caller media_types.
        assert ref1.media_type == "text/plain"
        assert ref2.media_type == "image/png"

        # Exactly one row in the table, recorded media_type is first put's.
        rows = store._conn.execute(
            "SELECT hash, media_type FROM content WHERE hash = ?",
            (ref1.hash,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["media_type"] == "text/plain"
    finally:
        store.close()
