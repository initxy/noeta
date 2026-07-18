"""Knowledge-source store (sqlite3, WAL, thread-safe within the process).

A knowledge source belongs to a space (space_id); name is unique within the
space. The materialization directory uses id (UUID), not name — this avoids
collisions after non-ASCII source names collapse under the sanitizing
whitelist (CJK characters all become `_`). config is stored as a JSON
string and converted back to a dict on read; the store treats it as opaque.
Expected shape per type: git_repo uses {url, branch (optional), token
(optional)}; local_dir uses {path}.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_sources (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL,
    config       TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',
    last_sync_at REAL,
    last_error   TEXT,
    created_by   TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_space_name
    ON knowledge_sources(space_id, name);
CREATE INDEX IF NOT EXISTS idx_knowledge_space
    ON knowledge_sources(space_id);
"""

VALID_TYPES = ("git_repo", "local_dir")
VALID_STATUSES = ("pending", "syncing", "ready", "failed")


def _row_to_source(row: tuple) -> dict:
    config_str = row[4] or "{}"
    try:
        config = json.loads(config_str)
    except (json.JSONDecodeError, TypeError):
        config = {}
    return {
        "id": row[0],
        "space_id": row[1],
        "name": row[2],
        "type": row[3],
        "config": config,
        "status": row[5],
        "last_sync_at": row[6],
        "last_error": row[7],
        "created_by": row[8],
        "created_at": row[9],
        "updated_at": row[10],
    }


class KnowledgeSourceStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ CRUD

    def create_source(
        self,
        source_id: str,
        space_id: str,
        name: str,
        source_type: str,
        config: dict,
        created_by: str,
    ) -> dict:
        if source_type not in VALID_TYPES:
            raise ValueError(f"Invalid knowledge-source type: {source_type}")
        now = time.time()
        config_json = json.dumps(config, ensure_ascii=False)
        with self._lock:
            # name is unique within the space
            existing = self._conn.execute(
                "SELECT id FROM knowledge_sources WHERE space_id=? AND name=?",
                (space_id, name),
            ).fetchone()
            if existing:
                raise ValueError(
                    f"A knowledge source with this name already exists in the space: {name}"
                )
            self._conn.execute(
                "INSERT INTO knowledge_sources (id, space_id, name, type, config,"
                " status, last_sync_at, last_error, created_by, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_id, space_id, name, source_type, config_json,
                    "pending", None, None, created_by, now, now,
                ),
            )
        return self.get_source(source_id)  # type: ignore[return-value]

    def get_source(self, source_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,space_id,name,type,config,status,last_sync_at,"
                "last_error,created_by,created_at,updated_at"
                " FROM knowledge_sources WHERE id=?",
                (source_id,),
            ).fetchone()
        return _row_to_source(row) if row else None

    def get_source_by_name(self, space_id: str, name: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,space_id,name,type,config,status,last_sync_at,"
                "last_error,created_by,created_at,updated_at"
                " FROM knowledge_sources WHERE space_id=? AND name=?",
                (space_id, name),
            ).fetchone()
        return _row_to_source(row) if row else None

    def list_sources(self, space_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,space_id,name,type,config,status,last_sync_at,"
                "last_error,created_by,created_at,updated_at"
                " FROM knowledge_sources WHERE space_id=? ORDER BY created_at ASC",
                (space_id,),
            ).fetchall()
        return [_row_to_source(r) for r in rows]

    def update_source(
        self,
        source_id: str,
        name: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> Optional[dict]:
        source = self.get_source(source_id)
        if source is None:
            return None
        now = time.time()
        fields: dict = {"updated_at": now}
        if name is not None and name != source["name"]:
            # Check that the new name is unique within the space
            with self._lock:
                existing = self._conn.execute(
                    "SELECT id FROM knowledge_sources WHERE space_id=? AND name=? AND id!=?",
                    (source["space_id"], name, source_id),
                ).fetchone()
                if existing:
                    raise ValueError(
                        f"A knowledge source with this name already exists in the space: {name}"
                    )
            fields["name"] = name
        if config is not None:
            fields["config"] = json.dumps(config, ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE knowledge_sources SET {cols} WHERE id=?",
                (*fields.values(), source_id),
            )
        return self.get_source(source_id)

    def delete_source(self, source_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM knowledge_sources WHERE id=?", (source_id,)
            )

    def update_status(
        self,
        source_id: str,
        status: str,
        last_sync_at: Optional[float] = None,
        last_error: Optional[str] = None,
    ) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        now = time.time()
        fields: dict = {"status": status, "updated_at": now}
        if last_sync_at is not None:
            fields["last_sync_at"] = last_sync_at
        if last_error is not None:
            fields["last_error"] = last_error
        elif last_error is None and status == "ready":
            # Clear the stale error when the source becomes ready
            fields["last_error"] = None
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE knowledge_sources SET {cols} WHERE id=?",
                (*fields.values(), source_id),
            )

    def try_set_syncing(self, source_id: str) -> bool:
        """Atomic claim: set status to syncing only if the current status
        != 'syncing'.

        A single UPDATE ... WHERE status!='syncing' guarantees that only one
        of the concurrent POST sync requests wins the claim (rowcount==1),
        closing the TOCTOU window between get_source reading the status and
        setting it. If the source does not exist, rowcount==0 and False is
        returned as well.
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE knowledge_sources SET status='syncing', updated_at=?"
                " WHERE id=? AND status!='syncing'",
                (now, source_id),
            )
            return cur.rowcount == 1

    def count_by_status(self) -> dict[str, int]:
        """Knowledge-source count per status (admin overview); returns
        status→count."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM knowledge_sources GROUP BY status"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def reset_syncing_to_failed(self) -> int:
        """At startup, reset stale syncing statuses to failed (a backend
        restart loses the sync threads). Returns the number of rows reset."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE knowledge_sources SET status='failed',"
                " last_error='Backend restarted; sync interrupted', updated_at=?"
                " WHERE status='syncing'",
                (time.time(),),
            )
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
