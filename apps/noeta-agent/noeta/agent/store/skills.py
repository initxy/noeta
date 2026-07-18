"""Skill registry (sqlite3, WAL, thread-safe within the process).

A single `skills` table holds both kinds of skills, scoped by space_id:

- **Global builtin**: `space_id="*"` (sentinel `GLOBAL_SPACE_ID`),
  `source="builtin"`, `group_name` always NULL. Platform-provisioned
  capabilities, uploaded / deleted / disabled by admins in the admin
  console; visible to every space's sessions and auto-assembled into
  sessions.
- **Space skills**: `space_id` = a real space id,
  `source ∈ {upload, market}`, visible only within that space.

Authority on existence: listing and session assembly consult only this
table (pure SELECT, no directory scans). The files (builtin under
`builtin-skills/<name>/`, space under `space-skills/<space_id>/<name>/`)
degrade to pure content, read only for preview / assembling symlinks.
`enabled` / `group_name` are folded into columns (no separate override
table).

Schema-in-code; boilerplate mirrors store/sessions.py. space_id + name is
unique.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

# Sentinel space_id for global builtin rows: used only as a space_id
# constant in the skills table; it never enters the spaces table and never
# takes part in space path construction (real space_ids are uuid-grade and
# never "*").
GLOBAL_SPACE_ID = "*"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    space_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    source       TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    group_name   TEXT,
    installed_at REAL NOT NULL,
    PRIMARY KEY (space_id, name)
);
CREATE INDEX IF NOT EXISTS idx_skills_space ON skills(space_id);
"""

_COLS = "space_id,name,source,description,enabled,group_name,installed_at"


def _row_to_dict(row: tuple) -> dict:
    return {
        "space_id": row[0],
        "name": row[1],
        "source": row[2],
        "description": row[3],
        "enabled": bool(row[4]),
        "group": row[5],
        "installed_at": row[6],
    }


class SkillStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def add(
        self,
        space_id: str,
        name: str,
        source: str,
        description: str = "",
        group: Optional[str] = None,
        enabled: bool = True,
        installed_at: Optional[float] = None,
    ) -> dict:
        """Insert / reinstall a row (reinstall = INSERT OR REPLACE: back to
        enabled by default, group cleared).

        For global builtins pass space_id=GLOBAL_SPACE_ID +
        source="builtin"; for space skills pass a real space_id +
        source ∈ {upload, market}.
        """
        now = installed_at if installed_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO skills ({_COLS})"
                " VALUES (?,?,?,?,?,?,?)",
                (space_id, name, source, description, 1 if enabled else 0, group, now),
            )
        return self.get(space_id, name)  # type: ignore[return-value]

    def get(self, space_id: str, name: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLS} FROM skills WHERE space_id=? AND name=?",
                (space_id, name),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_by_space(self, space_id: str) -> list[dict]:
        """A space's own skill rows (excluding global builtins), ordered by
        name."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM skills WHERE space_id=?"
                " ORDER BY name ASC",
                (space_id,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_builtin(self) -> list[dict]:
        """All global builtin rows (space_id="*"), ordered by name. Used by
        the admin builtin management + the space's read-only display."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM skills WHERE space_id=?"
                " ORDER BY name ASC",
                (GLOBAL_SPACE_ID,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_builtin(self, name: str) -> Optional[dict]:
        return self.get(GLOBAL_SPACE_ID, name)

    def enabled_names(self, space_id: str) -> set[str]:
        """Names of the skills enabled in this space (used when assembling
        the space segment to pick symlink targets)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM skills WHERE space_id=? AND enabled=1",
                (space_id,),
            ).fetchall()
        return {r[0] for r in rows}

    def builtin_enabled_names(self) -> set[str]:
        """Names of the enabled global builtin skills (used when assembling
        the global segment, effective for all sessions)."""
        return self.enabled_names(GLOBAL_SPACE_ID)

    def set_enabled(self, space_id: str, name: str, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE skills SET enabled=? WHERE space_id=? AND name=?",
                (1 if enabled else 0, space_id, name),
            )

    def set_group(self, space_id: str, name: str, group: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE skills SET group_name=? WHERE space_id=? AND name=?",
                (group, space_id, name),
            )

    def set_description(self, space_id: str, name: str, description: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE skills SET description=? WHERE space_id=? AND name=?",
                (description, space_id, name),
            )

    def delete(self, space_id: str, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM skills WHERE space_id=? AND name=?",
                (space_id, name),
            )

    def count_space(self) -> int:
        """Platform-wide count of space skill rows (excluding global
        builtins; admin overview)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM skills WHERE space_id!=?", (GLOBAL_SPACE_ID,)
            ).fetchone()
        return row[0] if row else 0

    def count_builtin(self) -> int:
        """Count of global builtin skill rows (admin overview)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM skills WHERE space_id=?", (GLOBAL_SPACE_ID,)
            ).fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
