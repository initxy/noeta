"""Tests for ``noeta.storage.sqlite.migrations`` (issue 15).

Pins the two behaviours the architect explicitly called out:

* re-running ``apply_migrations`` on an already-migrated database is
  a no-op (idempotent on reopen),
* a failing migration leaves ``PRAGMA user_version`` unchanged so the
  next init retries cleanly.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from noeta.storage.sqlite import migrations as migrations_module
from noeta.storage.sqlite._connection import _open_connection
from noeta.storage.sqlite.migrations import (
    MIGRATIONS,
    SCHEMA_VERSION,
    Migration,
    apply_migrations,
)


def _user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def test_apply_migrations_advances_user_version_to_schema_version(tmp_path):
    db = tmp_path / "noeta.db"
    conn = _open_connection(db)
    try:
        apply_migrations(conn)
        assert _user_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_apply_migrations_creates_expected_tables(tmp_path):
    db = tmp_path / "noeta.db"
    conn = _open_connection(db)
    try:
        apply_migrations(conn)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"events", "idempotency"}.issubset(names)
    finally:
        conn.close()


def test_apply_migrations_idempotent_on_reopen(tmp_path):
    db = tmp_path / "noeta.db"

    conn1 = _open_connection(db)
    try:
        apply_migrations(conn1)
    finally:
        conn1.close()

    conn2 = _open_connection(db)
    try:
        # Second init must not re-run DDL (which would fail with
        # ``table events already exists``).
        apply_migrations(conn2)
        assert _user_version(conn2) == SCHEMA_VERSION
    finally:
        conn2.close()


def test_failing_migration_leaves_user_version_unchanged(tmp_path, monkeypatch):
    db = tmp_path / "noeta.db"

    bad = Migration(
        version=SCHEMA_VERSION + 1,
        description="intentionally broken (syntax error)",
        statements=("CREATE TABLE !!! broken syntax",),
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS + [bad])

    conn = _open_connection(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(conn)
        # The good migration committed; the bad one rolled back, so
        # user_version sits at the last successful step.
        assert _user_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_partial_failure_rolls_back_within_a_single_migration(tmp_path, monkeypatch):
    """A migration that fails on its 2nd statement must not leave the 1st
    statement's effect behind.

    Without ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` framing, the
    first ``CREATE TABLE`` would commit before the second one's syntax
    error blew up, and a retry would crash with "table foo already
    exists".
    """
    db = tmp_path / "noeta.db"

    bad = Migration(
        version=SCHEMA_VERSION + 1,
        description="first statement OK, second blows up",
        statements=(
            "CREATE TABLE will_be_rolled_back (x INTEGER)",
            "CREATE TABLE !!! broken",
        ),
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS + [bad])

    conn = _open_connection(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(conn)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "will_be_rolled_back" not in names
        assert _user_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_2_creates_content_table(tmp_path):
    """Issue 16 migration 2 must add a ``content`` table to a fresh DB."""
    db = tmp_path / "noeta.db"
    conn = _open_connection(db)
    try:
        apply_migrations(conn)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "content" in names
        # user_version reaches whatever SCHEMA_VERSION is at runtime
        # — every issue that lands a new migration bumps it. The
        # invariant is that the content table exists, not that the
        # head version equals 2.
        assert _user_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_v1_db_upgrades_to_v2_preserving_data(tmp_path, monkeypatch):
    """A database initialised at v1 (events + idempotency only) must
    upgrade cleanly to v2 without losing the rows that already live on
    the v1 tables.

    Simulates the real-world upgrade story: issue 15 ships, a process
    runs, persists some events, exits. Issue 16 lands; on next start
    the v2 migration runs against the v1 file and the new ``content``
    table appears without touching any of the v1 data.
    """
    db = tmp_path / "noeta.db"

    # 1. Pin MIGRATIONS to v1-only so we end up with a v1 DB on disk.
    v1_only = [m for m in MIGRATIONS if m.version <= 1]
    monkeypatch.setattr(migrations_module, "MIGRATIONS", v1_only)
    conn = _open_connection(db)
    try:
        apply_migrations(conn)
        assert _user_version(conn) == 1
        conn.execute(
            "INSERT INTO events ("
            " task_id, seq, id, type, schema_version, occurred_at,"
            " actor, trace_id, correlation_id, causation_id, origin,"
            " payload_canonical"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "t1",
                0,
                "evt-existing",
                "TaskCreated",
                1,
                0.0,
                "engine",
                "trace-x",
                "t1",
                None,
                "engine",
                b"{}",
            ),
        )
        conn.execute(
            "INSERT INTO idempotency (task_id, lease_id, idempotency_key, seq) "
            "VALUES (?, ?, ?, ?)",
            ("t1", "lease-x", "op-x", 0),
        )
    finally:
        conn.close()

    # 2. Restore the real MIGRATIONS and reopen. The runner should add
    #    migration 2 without touching v1 data.
    monkeypatch.undo()
    conn2 = _open_connection(db)
    try:
        apply_migrations(conn2)
        assert _user_version(conn2) == SCHEMA_VERSION
        names = {
            row[0]
            for row in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"events", "idempotency", "content"}.issubset(names)
        # v1 rows still there
        events = conn2.execute(
            "SELECT id FROM events WHERE task_id = ?", ("t1",)
        ).fetchall()
        assert [r[0] for r in events] == ["evt-existing"]
        idem = conn2.execute(
            "SELECT seq FROM idempotency WHERE lease_id = ?", ("lease-x",)
        ).fetchall()
        assert [int(r[0]) for r in idem] == [0]
    finally:
        conn2.close()


def test_migration_3_creates_dispatcher_tables(tmp_path):
    """Issue 17 migration 3 must add dispatcher_tasks and
    dispatcher_pending_wakes tables to a fresh DB."""
    db = tmp_path / "noeta.db"
    conn = _open_connection(db)
    try:
        apply_migrations(conn)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"dispatcher_tasks", "dispatcher_pending_wakes"}.issubset(names)
        # No FK on pending_wakes (issue 17 B1).
        fk_rows = conn.execute(
            "PRAGMA foreign_key_list(dispatcher_pending_wakes)"
        ).fetchall()
        assert fk_rows == []
    finally:
        conn.close()


def test_migration_v2_db_upgrades_to_v3_preserving_data(tmp_path, monkeypatch):
    """A v2 DB must upgrade cleanly to v3 without losing v1/v2 rows."""
    db = tmp_path / "noeta.db"

    v2_only = [m for m in MIGRATIONS if m.version <= 2]
    monkeypatch.setattr(migrations_module, "MIGRATIONS", v2_only)
    conn = _open_connection(db)
    try:
        apply_migrations(conn)
        assert _user_version(conn) == 2
        conn.execute(
            "INSERT INTO content (hash, size, media_type, body) "
            "VALUES (?, ?, ?, ?)",
            ("a" * 64, 5, "text/plain", b"hello"),
        )
    finally:
        conn.close()

    monkeypatch.undo()
    conn2 = _open_connection(db)
    try:
        apply_migrations(conn2)
        assert _user_version(conn2) == SCHEMA_VERSION
        names = {
            row[0]
            for row in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "events",
            "idempotency",
            "content",
            "dispatcher_tasks",
            "dispatcher_pending_wakes",
        }.issubset(names)
        rows = conn2.execute(
            "SELECT body FROM content WHERE hash = ?", ("a" * 64,)
        ).fetchall()
        assert [bytes(r[0]) for r in rows] == [b"hello"]
    finally:
        conn2.close()


def test_concurrent_upgrade_from_v1_to_v3_safe(tmp_path, monkeypatch):
    """Two connections concurrently upgrading a v1 DB to v3 must both
    succeed without ``table already exists`` or stalls. Covers the
    'upgrade-not-fresh-init' branch of the race-safe runner."""
    db = tmp_path / "noeta.db"

    # 1. Initialise the DB at v1 only (pin MIGRATIONS to v1).
    v1_only = [m for m in MIGRATIONS if m.version <= 1]
    monkeypatch.setattr(migrations_module, "MIGRATIONS", v1_only)
    conn = _open_connection(db)
    try:
        apply_migrations(conn)
        assert _user_version(conn) == 1
    finally:
        conn.close()

    # 2. Restore real MIGRATIONS list and run two threads concurrently.
    monkeypatch.undo()

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            c = _open_connection(db)
            try:
                apply_migrations(c)
            finally:
                c.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "workers did not finish"
    assert not errors, f"concurrent upgrade raised: {errors!r}"

    conn = _open_connection(db)
    try:
        assert _user_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_concurrent_apply_migrations_safe(tmp_path):
    """Two independent connections concurrently initialising the same
    empty database must both succeed and converge on the final schema
    without raising ``table already exists`` or stalling.

    This is the regression test for issue 16 B1: pre-fix, both
    connections read ``user_version=0`` outside the lock, the loser
    of the BEGIN IMMEDIATE race then re-ran v1 DDL with stale state
    and crashed. Post-fix, the loser re-reads ``user_version`` inside
    its own write lock and exits cleanly because the winner has
    already bumped past it.
    """
    db = tmp_path / "noeta.db"

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            conn = _open_connection(db)
            try:
                apply_migrations(conn)
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001 — capture & rethrow in main
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "workers did not finish"
    assert not errors, f"concurrent apply_migrations raised: {errors!r}"

    conn = _open_connection(db)
    try:
        assert _user_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()
