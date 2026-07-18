"""Space-level agent configuration store (sqlite3, WAL, in-process thread-safe).

One row per space (space_id primary key); no row = all defaults. Fields:

- `prompt`: appended persona segment. At assembly time it is written into the
  session workspace `AGENT.md` (it does not override the platform's base
  system prompt).
- `memory_enabled`: memory toggle. The SDK memory capability is not wired in
  yet (it lands with the SDK upgrade branch); the column is persisted ahead of
  time and takes effect as soon as the capability is wired in.
- `knowledge_sources_json`: JSON list of knowledge-source ids that take part
  in assembly; NULL = all of them.
- `default_model` / `default_effort`: default model and reasoning effort for
  new sessions in this space; empty = platform default.

Schema-in-code; the boilerplate follows store/knowledge.py.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS space_agent_config (
    space_id               TEXT PRIMARY KEY,
    prompt                 TEXT NOT NULL DEFAULT '',
    memory_enabled         INTEGER NOT NULL DEFAULT 1,
    knowledge_sources_json TEXT,
    default_model          TEXT NOT NULL DEFAULT '',
    default_effort         TEXT NOT NULL DEFAULT '',
    updated_at             REAL NOT NULL
);
"""

_COLS = (
    "space_id,prompt,memory_enabled,"
    "knowledge_sources_json,default_model,default_effort,updated_at"
)

#: Defaults when no row exists (get never returns None, so callers never
#: need a None check).
DEFAULT_CONFIG = {
    "prompt": "",
    "memory_enabled": True,
    "knowledge_sources": None,  # None = all knowledge sources take part
    "default_model": "",
    "default_effort": "",
}


def _row_to_dict(row: tuple) -> dict:
    sources: Optional[list[str]] = None
    if row[3]:
        try:
            val = json.loads(row[3])
            if isinstance(val, list):
                sources = [str(s) for s in val]
        except ValueError:
            sources = None
    return {
        "prompt": row[1] or "",
        "memory_enabled": bool(row[2]),
        "knowledge_sources": sources,
        "default_model": row[4] or "",
        "default_effort": row[5] or "",
    }


class AgentConfigStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def get(self, space_id: str) -> dict:
        """Space config; returns a copy of DEFAULT_CONFIG when no row exists."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLS} FROM space_agent_config WHERE space_id=?",
                (space_id,),
            ).fetchone()
        return _row_to_dict(row) if row else dict(DEFAULT_CONFIG)

    def put(self, space_id: str, **fields) -> dict:
        """Partial update (omitted fields keep their current value; the first
        write fills the rest in with defaults).

        Accepted fields: prompt, memory_enabled,
        knowledge_sources (list | None), default_model, default_effort.
        """
        cur = self.get(space_id)
        allowed = set(DEFAULT_CONFIG)
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown config fields: {sorted(unknown)}")
        cur.update(fields)
        sources = cur["knowledge_sources"]
        sources_json = (
            json.dumps(list(sources), ensure_ascii=False)
            if sources is not None
            else None
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO space_agent_config"
                f" ({_COLS}) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(space_id) DO UPDATE SET"
                " prompt=excluded.prompt,"
                " memory_enabled=excluded.memory_enabled,"
                " knowledge_sources_json=excluded.knowledge_sources_json,"
                " default_model=excluded.default_model,"
                " default_effort=excluded.default_effort,"
                " updated_at=excluded.updated_at",
                (
                    space_id, cur["prompt"],
                    int(cur["memory_enabled"]), sources_json,
                    cur["default_model"], cur["default_effort"], time.time(),
                ),
            )
        return self.get(space_id)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
