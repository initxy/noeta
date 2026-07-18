"""Session metadata store (sqlite3, WAL, thread-safe within the process).

status semantics: idle (messages may be sent) / running (a turn is being
driven) / waiting (a follow-up question is pending an answer). For workflow
sessions the status is an aggregate (any task running → running); the
per-task truth lives in the `session_tasks` table.

Workflow sessions: `workflow_json` is the **workflow definition snapshot**
taken at session creation (later template edits / deletions do not affect
sessions in progress); the `session_tasks` table records each node's root
task by node_index (task_id / status / confirmed params).
`sessions.task_id` degrades to a "latest started task" snapshot (used for
title generation / plain-session compatibility).

Legacy databases may still carry the retired flow_id / flow_state columns
(the old flow design, superseded by the workflow snapshot model): they are
no longer read, written, or created by migration; SELECTs use explicit
column names and are unaffected.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Column order matches every SELECT / Session(*row) one-to-one; new columns
# are appended at the end to keep the change surface small.
_COLS = (
    "id,user,title,model,task_id,status,created_at,updated_at,space_id,"
    "title_generated,template_id,workflow_json"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    user       TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT 'New session',
    model      TEXT NOT NULL,
    task_id    TEXT,
    status     TEXT NOT NULL DEFAULT 'idle',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    space_id   TEXT NOT NULL DEFAULT '',
    title_generated INTEGER NOT NULL DEFAULT 0,
    template_id TEXT,
    workflow_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user, updated_at DESC);

CREATE TABLE IF NOT EXISTS session_tasks (
    session_id  TEXT NOT NULL,
    node_index  INTEGER NOT NULL,
    task_id     TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    params_json TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    PRIMARY KEY (session_id, node_index)
);
CREATE INDEX IF NOT EXISTS idx_session_tasks_task ON session_tasks(task_id);
"""

_TASK_COLS = "session_id,node_index,task_id,status,params_json,created_at"


def _task_row(row: tuple) -> dict:
    try:
        params = json.loads(row[4])
    except (ValueError, TypeError):
        params = {}
    return {
        "session_id": row[0],
        "node_index": row[1],
        "task_id": row[2],
        "status": row[3],
        "params": params if isinstance(params, dict) else {},
        "created_at": row[5],
    }


@dataclass
class Session:
    id: str
    user: str
    title: str
    model: str
    task_id: Optional[str]
    status: str
    created_at: float
    updated_at: float
    space_id: str = ""
    # Whether the title has already been generated asynchronously by the LLM:
    # generation is triggered once when the first turn ends, set to 1 on
    # success. sqlite has no bool, so INTEGER 0/1 is used; Session(*row)
    # reads it back as an int — just test it for truthiness.
    title_generated: int = 0
    # Template id when started from a single template (pure record); None
    # for workflow sessions.
    template_id: Optional[str] = None
    # Workflow definition snapshot JSON; None for non-workflow sessions.
    workflow_json: Optional[str] = None

    @property
    def workflow(self) -> Optional[dict]:
        """Parsed workflow snapshot; None for non-workflow sessions / on
        parse failure."""
        if not self.workflow_json:
            return None
        try:
            val = json.loads(self.workflow_json)
        except (ValueError, TypeError):
            return None
        return val if isinstance(val, dict) else None

    def to_api(self) -> dict:
        out = {
            "id": self.id,
            "title": self.title,
            "model": self.model,
            "status": self.status,
            "space_id": self.space_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "template_id": self.template_id,
        }
        # The workflow view (data source of the tab bar) is assembled by the
        # API layer via to_api_with_tasks (it needs to join session_tasks);
        # here we only flag whether this is a workflow session.
        out["is_workflow"] = bool(self.workflow_json)
        return out


class SessionStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._ensure_space_id()
        self._ensure_title_generated()
        self._ensure_workflow_columns()
        self._lock = threading.Lock()

    def _ensure_space_id(self) -> None:
        """Legacy-database migration: add the space_id column + space index
        (skipped if the column already exists).

        Backfill (moving legacy sessions into each user's personal space)
        needs SpaceStore; it is done by lifespan calling backfill_space_ids,
        not during table creation.
        """
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")]
        if "space_id" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN space_id TEXT NOT NULL DEFAULT ''"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_space"
            " ON sessions(space_id, updated_at DESC)"
        )
        # The admin console's global pagination sorts by updated_at alone;
        # add the single-column index (usable even without filters).
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_updated"
            " ON sessions(updated_at DESC)"
        )

    def _ensure_title_generated(self) -> None:
        """Legacy-database migration: add the title_generated column
        (skipped if it already exists).

        Legacy sessions default this column to 0, so after a restart the
        first finished turn generates an LLM title once more — acceptable.
        """
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")]
        if "title_generated" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN title_generated"
                " INTEGER NOT NULL DEFAULT 0"
            )

    def _ensure_workflow_columns(self) -> None:
        """Legacy-database migration: add the template_id / workflow_json
        columns (skipped if they already exist)."""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")]
        if "template_id" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN template_id TEXT")
        if "workflow_json" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN workflow_json TEXT")

    def backfill_space_ids(self, resolve_personal_space: Callable[[str], str]) -> None:
        """Move legacy sessions with an empty space_id into their user's
        personal space.

        resolve_personal_space(user) -> space_id (usually
        SpaceStore.ensure_personal_space); called once per user that has
        gaps.
        """
        with self._lock:
            users = [
                r[0]
                for r in self._conn.execute(
                    "SELECT DISTINCT user FROM sessions WHERE space_id=''"
                ).fetchall()
            ]
        for user in users:
            space_id = resolve_personal_space(user)
            with self._lock:
                self._conn.execute(
                    "UPDATE sessions SET space_id=? WHERE user=? AND space_id=''",
                    (space_id, user),
                )

    # ------------------------------------------------------------------
    def create(
        self,
        user: str,
        model: str,
        space_id: str,
        template_id: Optional[str] = None,
        workflow_json: Optional[str] = None,
    ) -> Session:
        now = time.time()
        sid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, user, title, model, task_id, status,"
                " created_at, updated_at, space_id, template_id, workflow_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, user, "New session", model, None, "idle", now, now, space_id,
                 template_id, workflow_json),
            )
        return self.get(sid)  # type: ignore[return-value]

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLS} FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        return Session(*row) if row else None

    def list_for_space(
        self, space_id: str, include_system: bool = False
    ) -> list[Session]:
        """Session list for a space. System sessions (user is a sentinel
        starting with ``__``, e.g. the channel session ``__channel__``) are
        excluded by default — they belong to nobody's "my sessions"."""
        where = "" if include_system else " AND substr(user,1,2)!='__'"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM sessions WHERE space_id=?{where}"
                " ORDER BY updated_at DESC",
                (space_id,),
            ).fetchall()
        return [Session(*r) for r in rows]

    def list_all_with_task(self) -> list[Session]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM sessions WHERE task_id IS NOT NULL",
            ).fetchall()
        return [Session(*r) for r in rows]

    # ------------------------------------------------------------ admin console
    def _admin_filter(
        self,
        user: Optional[str],
        space_id: Optional[str],
        status: Optional[str],
    ) -> tuple[str, list]:
        """Build the WHERE clause for the admin session list (any
        combination of user / space_id / status)."""
        clauses: list[str] = []
        params: list = []
        if user:
            clauses.append("user=?")
            params.append(user)
        if space_id:
            clauses.append("space_id=?")
            params.append(space_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def list_all(
        self,
        offset: int,
        limit: int,
        user: Optional[str] = None,
        space_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[Session]:
        """Globally paginated session list (admin console), ordered by
        updated_at descending + optional filters."""
        where, params = self._admin_filter(user, space_id, status)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM sessions{where}"
                " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [Session(*r) for r in rows]

    def count_all(
        self,
        user: Optional[str] = None,
        space_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        where, params = self._admin_filter(user, space_id, status)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM sessions{where}", params
            ).fetchone()
        return row[0] if row else 0

    def count_by_status(self) -> dict[str, int]:
        """Session count per status (admin overview); returns status→count."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM sessions GROUP BY status"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._conn.execute(
                "DELETE FROM session_tasks WHERE session_id=?", (session_id,)
            )

    # ------------------------------------------------------- workflow task rows
    def add_session_task(
        self,
        session_id: str,
        node_index: int,
        task_id: Optional[str],
        params: Optional[dict] = None,
        status: str = "running",
    ) -> None:
        """Record a node's root task (reopening the same node_index
        overwrites the old row; the MVP keeps no earlier generations)."""
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO session_tasks ({_TASK_COLS})"
                " VALUES (?,?,?,?,?,?)",
                (session_id, node_index, task_id, status,
                 json.dumps(params or {}, ensure_ascii=False), time.time()),
            )

    def list_session_tasks(self, session_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_TASK_COLS} FROM session_tasks WHERE session_id=?"
                " ORDER BY node_index ASC",
                (session_id,),
            ).fetchall()
        return [_task_row(r) for r in rows]

    def get_session_task_by_task_id(self, task_id: str) -> Optional[dict]:
        if not task_id:
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TASK_COLS} FROM session_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return _task_row(row) if row else None

    def update_session_task_status(self, task_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE session_tasks SET status=? WHERE task_id=?",
                (status, task_id),
            )

    def list_all_session_tasks(self) -> list[dict]:
        """All workflow task rows (used to rebuild the task→session mapping
        after a restart)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_TASK_COLS} FROM session_tasks WHERE task_id IS NOT NULL",
            ).fetchall()
        return [_task_row(r) for r in rows]

    def reset_stale_running_tasks(self) -> None:
        """After a service restart, reset stale running rows in
        session_tasks back to idle."""
        with self._lock:
            self._conn.execute(
                "UPDATE session_tasks SET status='idle' WHERE status='running'"
            )

    def update(self, session_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE sessions SET {cols} WHERE id=?",
                (*fields.values(), session_id),
            )

    def reset_stale_running(self) -> None:
        """After a service restart, reset statuses left running by the
        previous run (v1 does not auto-resume)."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status='idle' WHERE status='running'"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
