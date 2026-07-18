"""User profile store (sqlite3, WAL, thread-safe within the process).

Upserted after a successful login; member search matches by
username/email/name prefix. Follows the schema-in-code style of sessions.py.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    email      TEXT,
    name       TEXT,
    avatar     TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
"""


@dataclass
class User:
    username: str
    email: Optional[str]
    name: Optional[str]
    avatar: Optional[str]
    created_at: float
    updated_at: float

    def to_api(self) -> dict:
        return {
            "username": self.username,
            "email": self.email,
            "name": self.name,
            "avatar": self.avatar,
        }


class UserStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def upsert_user(
        self,
        username: str,
        email: Optional[str] = None,
        name: Optional[str] = None,
        avatar: Optional[str] = None,
    ) -> None:
        """Called after a successful login; if the row exists, update the
        profile fields while keeping created_at.

        Uses `IS NOT` for field-by-field comparison (NULL-safe): when no
        profile field changed, the whole UPDATE is skipped and updated_at is
        not refreshed — this keeps the per-request upsert from churning the
        timestamp and preserves search_users' updated_at ordering semantics.
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (username, email, name, avatar,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(username) DO UPDATE SET"
                " email=excluded.email, name=excluded.name,"
                " avatar=excluded.avatar,"
                " updated_at=excluded.updated_at"
                " WHERE users.email IS NOT excluded.email"
                " OR users.name IS NOT excluded.name"
                " OR users.avatar IS NOT excluded.avatar",
                (username, email, name, avatar, now, now),
            )

    def ensure_user(self, username: str, email: Optional[str] = None) -> None:
        """Create a stub user (username + optional email); if the row
        exists, keep the profile as-is without overwriting.

        Used when adding members: the person being added may never have
        logged in, so reserve a placeholder first (email invites store the
        email too, so the member list can display it); their first login
        then completes the profile via upsert_user. Uses INSERT OR IGNORE
        rather than an upsert to avoid wiping a logged-in user's
        email/name/avatar.
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO users (username, email, name, avatar,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (username, email, None, None, now, now),
            )

    def get_user(self, username: str) -> Optional[User]:
        with self._lock:
            row = self._conn.execute(
                "SELECT username,email,name,avatar,created_at,updated_at"
                " FROM users WHERE username=?",
                (username,),
            ).fetchone()
        return User(*row) if row else None

    def search_users(self, q: str, limit: int = 20) -> list[User]:
        """Prefix search by username/email/name (for member selection). An
        empty q returns the most recently updated users."""
        with self._lock:
            if q:
                like = f"{q}%"
                rows = self._conn.execute(
                    "SELECT username,email,name,avatar,created_at,updated_at"
                    " FROM users WHERE username LIKE ? OR email LIKE ? OR name LIKE ?"
                    " ORDER BY updated_at DESC LIMIT ?",
                    (like, like, like, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT username,email,name,avatar,created_at,updated_at"
                    " FROM users ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [User(*r) for r in rows]

    # ------------------------------------------------------------ admin console
    def list_users(self, offset: int, limit: int, q: str = "") -> list[User]:
        """Paginated user list (admin console), ordered by updated_at
        descending. A non-empty q applies prefix filtering."""
        with self._lock:
            if q:
                like = f"{q}%"
                rows = self._conn.execute(
                    "SELECT username,email,name,avatar,created_at,updated_at"
                    " FROM users WHERE username LIKE ? OR email LIKE ? OR name LIKE ?"
                    " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (like, like, like, limit, offset),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT username,email,name,avatar,created_at,updated_at"
                    " FROM users ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [User(*r) for r in rows]

    def count_users(self, q: str = "") -> int:
        with self._lock:
            if q:
                like = f"{q}%"
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM users"
                    " WHERE username LIKE ? OR email LIKE ? OR name LIKE ?",
                    (like, like, like),
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
