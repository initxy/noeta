"""Storage-backend-neutral ContentStore contract.

Every adapter — in-memory, sqlite, postgres — must agree on the same
observable behaviour: content-addressed put, hash-only dedup, the caller's
``media_type`` on the returned ref, hash-only ``get``, ``ContentNotFound``, and
the batch semantics of ``get_many``. Running one parametrized suite over all
three is what keeps a backend from quietly diverging; adapter-private details
(sqlite CHECK constraints, the stored row) get their own cases at the end.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from noeta.protocols.errors import ContentNotFound
from noeta.protocols.values import ContentRef
from noeta.storage.memory import InMemoryContentStore
from noeta.sdk.storage import SqliteContentStore
from tests._pg import isolated_schema_dsn, postgres_param


def _make_in_memory():
    return InMemoryContentStore()


def _make_sqlite():
    return SqliteContentStore(":memory:")


@pytest.fixture(params=["memory", "sqlite", postgres_param()])
def make_store(request):
    # Postgres: every factory call gets its own fresh schema on the
    # configured server so it is as isolated and empty as a fresh
    # InMemory / sqlite ``:memory:`` instance.
    from contextlib import ExitStack

    stack = ExitStack()

    if request.param == "memory":
        builder = _make_in_memory
    elif request.param == "sqlite":
        builder = _make_sqlite
    else:

        def builder():
            from noeta.sdk.storage import PostgresContentStore

            dsn = stack.enter_context(isolated_schema_dsn())
            return PostgresContentStore(dsn)

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
    stack.close()


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
    """Dedup keys on hash only, so storage keeps the first put's
    ``media_type`` — but the returned :class:`ContentRef` always carries the
    caller's, otherwise a second caller would silently get someone else's
    content type.
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
# Batch read (``get_many``)
# ---------------------------------------------------------------------------


def test_get_many_returns_every_body_keyed_by_hash(make_store) -> None:
    store = make_store()
    refs = [store.put(f"body-{i}".encode(), media_type="text/plain") for i in range(5)]
    assert store.get_many(refs) == {r.hash: store.get(r) for r in refs}


def test_get_many_omits_missing_hashes_instead_of_raising(make_store) -> None:
    """The one deliberate divergence from ``get``: a body outside the GC
    window must not abort the rest of the batch."""
    store = make_store()
    present = store.put(b"present", media_type="text/plain")
    absent = ContentRef(hash="0" * 64, size=0, media_type="text/plain")

    result = store.get_many([present, absent])

    assert result == {present.hash: b"present"}
    assert absent.hash not in result


def test_get_many_with_no_refs_returns_empty_without_touching_storage(
    make_store,
) -> None:
    store = make_store()
    store.put(b"unrelated", media_type="text/plain")
    assert store.get_many([]) == {}


def test_get_many_collapses_duplicate_refs(make_store) -> None:
    store = make_store()
    ref = store.put(b"once", media_type="text/plain")
    assert store.get_many([ref, ref, ref]) == {ref.hash: b"once"}


def test_get_many_is_hash_only_like_get(make_store) -> None:
    """``size`` / ``media_type`` are not cross-checked, matching ``get``."""
    store = make_store()
    ref = store.put(b"hello", media_type="text/plain")
    spoofed = ContentRef(hash=ref.hash, size=99999, media_type="image/png")
    assert store.get_many([spoofed]) == {ref.hash: b"hello"}


def test_get_many_agrees_with_get_over_a_mixed_batch(make_store) -> None:
    """The property the prefetch path relies on: batching changes which
    query runs, never which bytes come back."""
    store = make_store()
    bodies = [b"", b"a", b"\x00\xff binary", b"x" * 5000]
    refs = [store.put(b, media_type="application/octet-stream") for b in bodies]

    batched = store.get_many(refs)

    assert batched == {ref.hash: store.get(ref) for ref in refs}


def test_get_many_handles_more_refs_than_one_query_can_bind(make_store) -> None:
    """sqlite caps host parameters per statement, so its ``get_many``
    chunks; 1000 refs crosses that boundary and must still come back whole."""
    store = make_store()
    refs = [store.put(f"chunk-{i}".encode(), media_type="text/plain") for i in range(1000)]

    result = store.get_many(refs)

    assert len(result) == len(refs)
    assert result[refs[0].hash] == b"chunk-0"
    assert result[refs[-1].hash] == b"chunk-999"


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


# ---------------------------------------------------------------------------
# ``sweep`` — the maintenance affordance every built-in adapter carries
# ---------------------------------------------------------------------------


def _age_every_row(store) -> None:
    """Push every row's last-put time to the epoch so a grace of any length
    treats it as old — the backend's private clock, not the wall clock."""
    if isinstance(store, InMemoryContentStore):
        for hash_ in store._touched:
            store._touched[hash_] = 0.0
    else:
        store._conn.execute("UPDATE content SET touched_at = 0")


def test_content_ref_for_mints_the_ref_put_returns(make_store) -> None:
    from noeta.protocols.content_store import content_ref_for

    store = make_store()
    body = b"identity without storage"
    assert store.put(body, media_type="text/plain") == content_ref_for(
        body, media_type="text/plain"
    )


def test_sweep_reclaims_only_unreferenced_rows_older_than_the_grace(
    make_store,
) -> None:
    store = make_store()
    live = store.put(b"still referenced", media_type="text/plain")
    dead = store.put(b"orphaned long ago", media_type="text/plain")
    _age_every_row(store)

    outcome = store.sweep({live.hash}, grace_seconds=3600.0)

    assert (outcome.rows, outcome.bytes) == (1, dead.size)
    assert store.get(live) == b"still referenced"
    with pytest.raises(ContentNotFound):
        store.get(dead)
    assert store.get_many([live, dead]) == {live.hash: b"still referenced"}


def test_sweep_keeps_an_unreferenced_row_inside_the_grace(make_store) -> None:
    store = make_store()
    fresh = store.put(b"just written by a running turn", media_type="text/plain")

    outcome = store.sweep(set(), grace_seconds=3600.0)

    assert (outcome.rows, outcome.bytes) == (0, 0)
    assert store.get(fresh) == b"just written by a running turn"


def test_sweep_treats_a_dedup_put_as_a_fresh_write(make_store) -> None:
    """A body re-``put`` after it aged — the dedup hit a live turn makes on a
    row an earlier task left behind — is as young as that put."""
    store = make_store()
    ref = store.put(b"shared across tasks", media_type="text/plain")
    _age_every_row(store)
    assert store.put(b"shared across tasks", media_type="text/plain") == ref

    outcome = store.sweep(set(), grace_seconds=3600.0)

    assert outcome.rows == 0
    assert store.get(ref) == b"shared across tasks"


def test_sweep_with_nothing_to_do_reports_zero(make_store) -> None:
    store = make_store()
    outcome = store.sweep(set(), grace_seconds=0.0)
    assert (outcome.rows, outcome.bytes) == (0, 0)


def test_sweep_vacuum_flag_reports_what_the_backend_did(make_store) -> None:
    store = make_store()
    outcome = store.sweep(set(), grace_seconds=0.0, vacuum=True)
    # The in-memory store has no file to compact; the durable ones do.
    assert outcome.vacuumed is not isinstance(store, InMemoryContentStore)


def test_sqlite_sweep_reevaluates_the_age_under_its_own_lock() -> None:
    """The DELETE re-checks ``touched_at`` — a row the candidate listing saw
    as old but a ``put`` refreshed before the DELETE ran is kept."""
    clock = [1_000.0]
    store = SqliteContentStore(":memory:", clock=lambda: clock[0])
    try:
        ref = store.put(b"raced", media_type="text/plain")
        clock[0] = 5_000.0
        real = store._conn

        class _RacingConnection:
            """The adapter's connection, with a turn's re-put wedged in right
            after the sweep lists its candidates."""

            def execute(self, sql, *args):
                cursor = real.execute(sql, *args)
                if sql.startswith("SELECT hash, size FROM content"):
                    rows = cursor.fetchall()
                    real.execute(
                        "UPDATE content SET touched_at = ? WHERE hash = ?",
                        (clock[0], ref.hash),
                    )

                    class _Listed:
                        def fetchall(self_inner):
                            return rows

                    return _Listed()
                return cursor

            def close(self):
                real.close()

        store._conn = _RacingConnection()  # type: ignore[assignment]
        outcome = store.sweep(set(), grace_seconds=100.0)
        assert (outcome.rows, outcome.bytes) == (0, 0)
        store._conn = real
        assert store.get(ref) == b"raced"
    finally:
        store.close()
