"""Channel store (sqlite3, WAL, thread-safe within the process) — the space
collaboration layer.

Four tables:

- `channels`: channels under a space. `session_id` is the channel's
  persistent session (lazily created and backfilled when the first @Agent
  topic is started).
- `channel_messages`: main-stream messages (human chat + topic root
  messages). `seq` is globally monotonically increasing and serves as the
  replay axis for the channel SSE `since_seq`; Agent replies do **not**
  land in this table (the topic view replays per task via the EventLog,
  avoiding a double source of truth). A non-NULL `topic_id` = a topic root
  message.
- `channel_topics`: the mapping from a topic (a root task triggered by
  @Agent) to a task inside the session (`node_index` matches a
  `session_tasks` row). Task status is not duplicated here — the truth
  lives in `session_tasks`, joined by the service layer;
  `last_reply_preview` is the reply excerpt shown on the topic card
  (written by the watcher projection).
- `channel_reads`: per-user unread watermark (last_read_seq).

Schema-in-code; boilerplate mirrors store/skills.py.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id          TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    session_id  TEXT,
    created_by  TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channels_space ON channels(space_id, archived);
CREATE INDEX IF NOT EXISTS idx_channels_session ON channels(session_id);

CREATE TABLE IF NOT EXISTS channel_messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    author     TEXT NOT NULL,
    text       TEXT NOT NULL,
    topic_id   TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channel_messages ON channel_messages(channel_id, seq);

CREATE TABLE IF NOT EXISTS channel_topics (
    id                 TEXT PRIMARY KEY,
    channel_id         TEXT NOT NULL,
    root_message_seq   INTEGER NOT NULL,
    node_index         INTEGER NOT NULL,
    created_by         TEXT NOT NULL,
    last_reply_preview TEXT NOT NULL DEFAULT '',
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channel_topics ON channel_topics(channel_id, created_at);

CREATE TABLE IF NOT EXISTS channel_reads (
    channel_id    TEXT NOT NULL,
    username      TEXT NOT NULL,
    last_read_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, username)
);
"""

_CHANNEL_COLS = (
    "id,space_id,name,description,session_id,created_by,archived,created_at,updated_at"
)
_MESSAGE_COLS = "seq,channel_id,author,text,topic_id,created_at"
_TOPIC_COLS = (
    "id,channel_id,root_message_seq,node_index,created_by,last_reply_preview,"
    "created_at,updated_at"
)


def _channel_row(row: tuple) -> dict:
    return {
        "id": row[0],
        "space_id": row[1],
        "name": row[2],
        "description": row[3],
        "session_id": row[4],
        "created_by": row[5],
        "archived": bool(row[6]),
        "created_at": row[7],
        "updated_at": row[8],
    }


def _message_row(row: tuple) -> dict:
    return {
        "seq": row[0],
        "channel_id": row[1],
        "author": row[2],
        "text": row[3],
        "topic_id": row[4],
        "created_at": row[5],
    }


def _topic_row(row: tuple) -> dict:
    return {
        "id": row[0],
        "channel_id": row[1],
        "root_message_seq": row[2],
        "node_index": row[3],
        "created_by": row[4],
        "last_reply_preview": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


class ChannelStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    # ------------------------------------------------------------- channels
    def create_channel(
        self, space_id: str, name: str, created_by: str, description: str = ""
    ) -> dict:
        now = time.time()
        cid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                f"INSERT INTO channels ({_CHANNEL_COLS})"
                " VALUES (?,?,?,?,NULL,?,0,?,?)",
                (cid, space_id, name, description, created_by, now, now),
            )
        return self.get_channel(cid)  # type: ignore[return-value]

    def get_channel(self, channel_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_CHANNEL_COLS} FROM channels WHERE id=?", (channel_id,)
            ).fetchone()
        return _channel_row(row) if row else None

    def get_channel_by_session(self, session_id: str) -> Optional[dict]:
        if not session_id:
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_CHANNEL_COLS} FROM channels WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return _channel_row(row) if row else None

    def list_channels(self, space_id: str, include_archived: bool = False) -> list[dict]:
        where = "" if include_archived else " AND archived=0"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_CHANNEL_COLS} FROM channels WHERE space_id=?{where}"
                " ORDER BY created_at",
                (space_id,),
            ).fetchall()
        return [_channel_row(r) for r in rows]

    def update_channel(self, channel_id: str, **fields) -> None:
        allowed = {"name", "description", "archived", "session_id"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Unsupported fields: {bad}")
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE channels SET {cols} WHERE id=?",
                (*fields.values(), channel_id),
            )

    def delete_channel(self, channel_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))
            self._conn.execute(
                "DELETE FROM channel_messages WHERE channel_id=?", (channel_id,)
            )
            self._conn.execute(
                "DELETE FROM channel_topics WHERE channel_id=?", (channel_id,)
            )
            self._conn.execute(
                "DELETE FROM channel_reads WHERE channel_id=?", (channel_id,)
            )

    # ------------------------------------------------------------- messages
    def add_message(
        self,
        channel_id: str,
        author: str,
        text: str,
        topic_id: Optional[str] = None,
    ) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO channel_messages (channel_id, author, text, topic_id,"
                " created_at) VALUES (?,?,?,?,?)",
                (channel_id, author, text, topic_id, time.time()),
            )
            row = self._conn.execute(
                f"SELECT {_MESSAGE_COLS} FROM channel_messages WHERE seq=?",
                (cur.lastrowid,),
            ).fetchone()
        return _message_row(row)

    def set_message_topic(self, seq: int, topic_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE channel_messages SET topic_id=? WHERE seq=?", (topic_id, seq)
            )

    def list_messages(
        self,
        channel_id: str,
        before_seq: Optional[int] = None,
        after_seq: Optional[int] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return one page of messages in ascending seq order. before_seq is
        for paging back through history (the limit messages closest before
        it), after_seq is for replay (the limit messages after it); the two
        are mutually exclusive, and both empty = the latest page."""
        with self._lock:
            if after_seq is not None:
                rows = self._conn.execute(
                    f"SELECT {_MESSAGE_COLS} FROM channel_messages"
                    " WHERE channel_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
                    (channel_id, after_seq, limit),
                ).fetchall()
                return [_message_row(r) for r in rows]
            if before_seq is not None:
                rows = self._conn.execute(
                    f"SELECT {_MESSAGE_COLS} FROM channel_messages"
                    " WHERE channel_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
                    (channel_id, before_seq, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {_MESSAGE_COLS} FROM channel_messages"
                    " WHERE channel_id=? ORDER BY seq DESC LIMIT ?",
                    (channel_id, limit),
                ).fetchall()
        return [_message_row(r) for r in reversed(rows)]

    def get_message(self, seq: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_MESSAGE_COLS} FROM channel_messages WHERE seq=?", (seq,)
            ).fetchone()
        return _message_row(row) if row else None

    def latest_seq(self, channel_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(seq) FROM channel_messages WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
        return row[0] or 0

    # --------------------------------------------------------------- topics
    def add_topic(
        self,
        channel_id: str,
        root_message_seq: int,
        node_index: int,
        created_by: str,
    ) -> dict:
        now = time.time()
        tid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                f"INSERT INTO channel_topics ({_TOPIC_COLS})"
                " VALUES (?,?,?,?,?,'',?,?)",
                (tid, channel_id, root_message_seq, node_index, created_by, now, now),
            )
            row = self._conn.execute(
                f"SELECT {_TOPIC_COLS} FROM channel_topics WHERE id=?", (tid,)
            ).fetchone()
        return _topic_row(row)

    def get_topic(self, topic_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TOPIC_COLS} FROM channel_topics WHERE id=?", (topic_id,)
            ).fetchone()
        return _topic_row(row) if row else None

    def get_topic_by_node(self, channel_id: str, node_index: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TOPIC_COLS} FROM channel_topics"
                " WHERE channel_id=? AND node_index=?",
                (channel_id, node_index),
            ).fetchone()
        return _topic_row(row) if row else None

    def list_topics(self, channel_id: str, limit: int = 100) -> list[dict]:
        """The most recent `limit` topics in ascending creation order
        (topic-index injection + frontend snapshot)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_TOPIC_COLS} FROM channel_topics WHERE channel_id=?"
                " ORDER BY created_at DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        return [_topic_row(r) for r in reversed(rows)]

    def update_topic_preview(self, topic_id: str, preview: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE channel_topics SET last_reply_preview=?, updated_at=?"
                " WHERE id=?",
                (preview, time.time(), topic_id),
            )

    def channels_with_topics(self) -> list[str]:
        """Deduplicated session_ids of channels that have topics (used to
        restore watchers after a restart)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT c.session_id FROM channels c"
                " JOIN channel_topics t ON t.channel_id=c.id"
                " WHERE c.session_id IS NOT NULL"
            ).fetchall()
        return [r[0] for r in rows]

    # ---------------------------------------------------------------- reads
    def set_read(self, channel_id: str, username: str, seq: int) -> None:
        """Advance the watermark (it only moves forward, never back)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO channel_reads (channel_id, username, last_read_seq)"
                " VALUES (?,?,?)"
                " ON CONFLICT(channel_id, username) DO UPDATE SET"
                " last_read_seq=MAX(last_read_seq, excluded.last_read_seq)",
                (channel_id, username, seq),
            )

    def unread_counts(self, space_id: str, username: str) -> dict[str, int]:
        """Unread counts per channel in the space (channel_id → count).
        Messages sent by the user themselves are not counted."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.channel_id, COUNT(*) FROM channel_messages m"
                " JOIN channels c ON c.id=m.channel_id"
                " LEFT JOIN channel_reads r"
                "   ON r.channel_id=m.channel_id AND r.username=?"
                " WHERE c.space_id=? AND c.archived=0 AND m.author!=?"
                "   AND m.seq > COALESCE(r.last_read_seq, 0)"
                " GROUP BY m.channel_id",
                (username, space_id, username),
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
