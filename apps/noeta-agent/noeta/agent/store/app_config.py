"""Dynamic config KV store (sqlite3, WAL, in-process thread-safe).

The small set of admin-managed hot-reload config items lands here:
key -> JSON value plus who changed it / when. Only items that have been
explicitly overridden are stored; a read of a non-overridden item falls back
to the static Settings value (see config_registry). Values are persisted as
JSON strings and converted back to Python values on read. Schema-in-code;
the boilerplate follows store/knowledge.py.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_by TEXT,
    updated_at REAL NOT NULL
);
"""


class AppConfigStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """The overridden value (JSON-parsed); None when not overridden or
        when parsing fails (fall back to the static value)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_config WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None

    def get_meta(self, key: str) -> Optional[dict]:
        """Override metadata for one item {value, updated_by, updated_at};
        None when not overridden."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value, updated_by, updated_at FROM app_config WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (ValueError, TypeError):
            return None
        return {"value": value, "updated_by": row[1], "updated_at": row[2]}

    def set(self, key: str, value: Any, updated_by: Optional[str]) -> None:
        now = time.time()
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_config (key, value, updated_by, updated_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " value=excluded.value, updated_by=excluded.updated_by,"
                " updated_at=excluded.updated_at",
                (key, payload, updated_by, now),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
