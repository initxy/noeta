"""File-on-disk durability smoke for ``SqliteContentStore`` (issue 16).

The contract suite runs an in-memory backend; this module covers the
disk-only behaviours: ``put`` survives close+reopen and the body
returned later is byte-for-byte identical.
"""

from __future__ import annotations

from noeta.storage.sqlite.contentstore import SqliteContentStore


def test_blobs_survive_close_and_reopen(tmp_path) -> None:
    db = tmp_path / "noeta.db"

    store = SqliteContentStore(db)
    refs = []
    try:
        for i in range(5):
            body = f"body number {i}".encode() + b"\x00\xff"
            refs.append(store.put(body, media_type="application/octet-stream"))
    finally:
        store.close()

    reopened = SqliteContentStore(db)
    try:
        for i, ref in enumerate(refs):
            expected = f"body number {i}".encode() + b"\x00\xff"
            assert reopened.get(ref) == expected
    finally:
        reopened.close()


def test_close_is_idempotent(tmp_path) -> None:
    store = SqliteContentStore(tmp_path / "noeta.db")
    store.close()
    store.close()


def test_context_manager_closes_on_exit(tmp_path) -> None:
    db = tmp_path / "noeta.db"
    with SqliteContentStore(db) as store:
        ref = store.put(b"hello", media_type="text/plain")
    assert store._closed
    # Reopen and assert the put landed.
    with SqliteContentStore(db) as reopened:
        assert reopened.get(ref) == b"hello"
