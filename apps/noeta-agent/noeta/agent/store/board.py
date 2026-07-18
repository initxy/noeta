"""Task board store (sqlite3, WAL, thread-safe within the process) — the
space collaboration layer, Phase 2.

One board per space with three fixed columns (todo / doing / done; columns
have no independent entity). Cards are primarily created and managed by
people; `links_json` carries backlinks to collaboration artifacts (topics /
sessions), and `position` is the in-column ordering (REAL; the frontend
inserts at the midpoint between the two neighboring cards to avoid
reordering the whole column).

Schema-in-code; boilerplate mirrors store/channels.py.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

BOARD_COLUMNS = ("todo", "doing", "done")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS board_cards (
    id          TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL,
    column_key  TEXT NOT NULL DEFAULT 'todo',
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    assignee    TEXT,
    due_date    TEXT,
    links_json  TEXT NOT NULL DEFAULT '[]',
    position    REAL NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_board_cards_space
    ON board_cards(space_id, column_key, position);
"""

_COLS = (
    "id,space_id,column_key,title,description,assignee,due_date,links_json,"
    "position,created_by,created_at,updated_at"
)


def _card_row(row: tuple) -> dict:
    try:
        links = json.loads(row[7])
    except (ValueError, TypeError):
        links = []
    return {
        "id": row[0],
        "space_id": row[1],
        "column_key": row[2],
        "title": row[3],
        "description": row[4],
        "assignee": row[5],
        "due_date": row[6],
        "links": links if isinstance(links, list) else [],
        "position": row[8],
        "created_by": row[9],
        "created_at": row[10],
        "updated_at": row[11],
    }


class BoardStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def create_card(
        self,
        space_id: str,
        title: str,
        created_by: str,
        column_key: str = "todo",
        description: str = "",
        assignee: Optional[str] = None,
        due_date: Optional[str] = None,
        links: Optional[list[dict]] = None,
    ) -> dict:
        if column_key not in BOARD_COLUMNS:
            raise ValueError(f"Unknown board column: {column_key}")
        now = time.time()
        cid = uuid.uuid4().hex
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(position), 0) FROM board_cards"
                " WHERE space_id=? AND column_key=?",
                (space_id, column_key),
            ).fetchone()
            position = (row[0] or 0) + 1.0
            self._conn.execute(
                f"INSERT INTO board_cards ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid, space_id, column_key, title, description, assignee,
                    due_date, json.dumps(links or [], ensure_ascii=False),
                    position, created_by, now, now,
                ),
            )
        return self.get_card(cid)  # type: ignore[return-value]

    def get_card(self, card_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLS} FROM board_cards WHERE id=?", (card_id,)
            ).fetchone()
        return _card_row(row) if row else None

    def list_cards(self, space_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM board_cards WHERE space_id=?"
                " ORDER BY column_key, position",
                (space_id,),
            ).fetchall()
        return [_card_row(r) for r in rows]

    def update_card(self, card_id: str, **fields) -> None:
        allowed = {
            "title", "description", "assignee", "due_date",
            "column_key", "position", "links_json",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Unsupported fields: {bad}")
        if "column_key" in fields and fields["column_key"] not in BOARD_COLUMNS:
            raise ValueError(f"Unknown board column: {fields['column_key']}")
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE board_cards SET {cols} WHERE id=?",
                (*fields.values(), card_id),
            )

    def add_link(self, card_id: str, link: dict) -> Optional[dict]:
        """Idempotently append a backlink (skipped if one with the same
        type+id already exists)."""
        card = self.get_card(card_id)
        if card is None:
            return None
        links = card["links"]
        if not any(
            l.get("type") == link.get("type") and l.get("id") == link.get("id")
            for l in links
        ):
            links = [*links, link]
            self.update_card(
                card_id, links_json=json.dumps(links, ensure_ascii=False)
            )
        return self.get_card(card_id)

    def move_to_column_end(self, card_id: str, column_key: str) -> Optional[dict]:
        """Move to the end of the target column (used by the Agent tool;
        frontend drag-and-drop uses an explicit position)."""
        if column_key not in BOARD_COLUMNS:
            raise ValueError(f"Unknown board column: {column_key}")
        card = self.get_card(card_id)
        if card is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(position), 0) FROM board_cards"
                " WHERE space_id=? AND column_key=?",
                (card["space_id"], column_key),
            ).fetchone()
            self._conn.execute(
                "UPDATE board_cards SET column_key=?, position=?, updated_at=?"
                " WHERE id=?",
                (column_key, (row[0] or 0) + 1.0, time.time(), card_id),
            )
        return self.get_card(card_id)

    def delete_card(self, card_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM board_cards WHERE id=?", (card_id,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
