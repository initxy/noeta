"""Space and membership store (sqlite3, WAL, thread-safe within the process).

A personal space (is_personal=1) is created automatically on a user's first
login; team spaces are created by users. Member roles are owner/member.
Business constraints (the last owner cannot be removed/demoted; a personal
space's name is immutable) are enforced here by raising ValueError, which
the API layer translates into a 400.

The users table shares the database (app.db); list_members LEFT JOINs it
directly for profiles. Follows the schema-in-code style of sessions.py.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

ROLE_OWNER = "owner"
ROLE_MEMBER = "member"
VALID_ROLES = (ROLE_OWNER, ROLE_MEMBER)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spaces (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_personal INTEGER NOT NULL DEFAULT 0,
    owner       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS space_members (
    space_id   TEXT NOT NULL,
    username   TEXT NOT NULL,
    role       TEXT NOT NULL,
    added_by   TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (space_id, username)
);
CREATE INDEX IF NOT EXISTS idx_members_username ON space_members(username);
CREATE INDEX IF NOT EXISTS idx_spaces_owner ON spaces(owner, is_personal);
"""


class LastOwnerError(ValueError):
    """The last owner of a space cannot be removed or demoted."""


class PersonalSpaceError(ValueError):
    """Operation not allowed on a personal space (rename / add member /
    delete)."""


def _row_to_space(row: tuple) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "description": row[2],
        "is_personal": bool(row[3]),
        "owner": row[4],
        "created_at": row[5],
        "updated_at": row[6],
    }


class SpaceStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    # -------------------------------------------------------------- spaces
    def _create_space_locked(
        self,
        space_id: str,
        name: str,
        description: str,
        is_personal: bool,
        owner: str,
        now: float,
    ) -> None:
        """Create the space + write the owner membership row. The caller
        must hold the lock."""
        self._conn.execute(
            "INSERT INTO spaces (id, name, description, is_personal, owner,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (space_id, name, description, 1 if is_personal else 0, owner, now, now),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO space_members (space_id, username, role,"
            " added_by, created_at) VALUES (?,?,?,?,?)",
            (space_id, owner, ROLE_OWNER, owner, now),
        )

    def create_space(
        self,
        space_id: str,
        name: str,
        description: str,
        is_personal: bool,
        owner: str,
    ) -> dict:
        """Create a space and write the owner into space_members
        (role=owner)."""
        now = time.time()
        with self._lock:
            self._create_space_locked(
                space_id, name, description, is_personal, owner, now
            )
        return self.get_space(space_id)  # type: ignore[return-value]

    def get_space(self, space_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,name,description,is_personal,owner,created_at,updated_at"
                " FROM spaces WHERE id=?",
                (space_id,),
            ).fetchone()
        return _row_to_space(row) if row else None

    def list_spaces_for_user(self, username: str) -> list[dict]:
        """All spaces the user has joined, with my_role and member_count;
        the personal space is pinned first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id,s.name,s.description,s.is_personal,s.owner,"
                " s.created_at,s.updated_at, m.role,"
                " (SELECT COUNT(*) FROM space_members mm WHERE mm.space_id=s.id)"
                " FROM spaces s JOIN space_members m"
                " ON m.space_id=s.id AND m.username=?"
                " ORDER BY s.is_personal DESC, s.updated_at DESC",
                (username,),
            ).fetchall()
        result = []
        for row in rows:
            space = _row_to_space(row[:7])
            space["my_role"] = row[7]
            space["member_count"] = row[8]
            result.append(space)
        return result

    def list_all_spaces(self, offset: int, limit: int) -> list[dict]:
        """Fully paginated space list (admin console): each row carries
        member_count + session_count aggregates.

        The sessions table shares the database (app.db); session counts are
        aggregated with a subquery. Personal spaces are pinned first, then
        ordered by update time descending.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id,s.name,s.description,s.is_personal,s.owner,"
                " s.created_at,s.updated_at,"
                " (SELECT COUNT(*) FROM space_members mm WHERE mm.space_id=s.id),"
                " (SELECT COUNT(*) FROM sessions ss WHERE ss.space_id=s.id)"
                " FROM spaces s"
                " ORDER BY s.is_personal DESC, s.updated_at DESC"
                " LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        result = []
        for row in rows:
            space = _row_to_space(row[:7])
            space["member_count"] = row[7]
            space["session_count"] = row[8]
            result.append(space)
        return result

    def count_spaces(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM spaces").fetchone()
        return row[0] if row else 0

    def update_space(
        self,
        space_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[dict]:
        """Owner edits the space info; a personal space's name is
        immutable."""
        space = self.get_space(space_id)
        if space is None:
            return None
        fields: dict = {}
        if name is not None and name != space["name"]:
            if space["is_personal"]:
                raise PersonalSpaceError("A personal space cannot be renamed")
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if not fields:
            return space
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE spaces SET {cols} WHERE id=?",
                (*fields.values(), space_id),
            )
        return self.get_space(space_id)

    def delete_space(self, space_id: str) -> None:
        """Delete the space + its membership rows. Cascaded session cleanup
        is handled beforehand by the caller (the API layer)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM space_members WHERE space_id=?", (space_id,)
            )
            self._conn.execute("DELETE FROM spaces WHERE id=?", (space_id,))

    # ------------------------------------------------------------- members
    def _owner_count(self, space_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM space_members WHERE space_id=? AND role=?",
            (space_id, ROLE_OWNER),
        ).fetchone()
        return row[0] if row else 0

    def add_member(
        self, space_id: str, username: str, role: str, added_by: str
    ) -> None:
        """Add a member; if already present, update the role (upsert)."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO space_members (space_id, username, role, added_by,"
                " created_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(space_id, username) DO UPDATE SET role=excluded.role",
                (space_id, username, role, added_by, now),
            )

    def remove_member(self, space_id: str, username: str) -> None:
        with self._lock:
            role = self._role_locked(space_id, username)
            if role is None:
                return
            if role == ROLE_OWNER and self._owner_count(space_id) <= 1:
                raise LastOwnerError("Cannot remove the only owner of the space")
            self._conn.execute(
                "DELETE FROM space_members WHERE space_id=? AND username=?",
                (space_id, username),
            )

    def update_member_role(self, space_id: str, username: str, role: str) -> None:
        with self._lock:
            current = self._role_locked(space_id, username)
            if current is None:
                raise ValueError("The user is not a member of the space")
            if (
                current == ROLE_OWNER
                and role != ROLE_OWNER
                and self._owner_count(space_id) <= 1
            ):
                raise LastOwnerError("Cannot demote the last owner of the space")
            self._conn.execute(
                "UPDATE space_members SET role=? WHERE space_id=? AND username=?",
                (role, space_id, username),
            )

    def list_members(self, space_id: str) -> list[dict]:
        """Member list, LEFT JOINed with users to carry name/avatar/email
        (None for users who have never logged in)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.username, m.role, m.added_by, m.created_at,"
                " u.name, u.avatar, u.email"
                " FROM space_members m LEFT JOIN users u ON u.username=m.username"
                " WHERE m.space_id=? ORDER BY m.created_at ASC",
                (space_id,),
            ).fetchall()
        return [
            {
                "username": r[0],
                "role": r[1],
                "added_by": r[2],
                "created_at": r[3],
                "name": r[4],
                "avatar": r[5],
                "email": r[6],
            }
            for r in rows
        ]

    def _role_locked(self, space_id: str, username: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT role FROM space_members WHERE space_id=? AND username=?",
            (space_id, username),
        ).fetchone()
        return row[0] if row else None

    def get_member_role(self, space_id: str, username: str) -> Optional[str]:
        with self._lock:
            return self._role_locked(space_id, username)

    def is_member(self, space_id: str, username: str) -> bool:
        return self.get_member_role(space_id, username) is not None

    # -------------------------------------------------------------- personal
    def ensure_personal_space(self, username: str) -> str:
        """Return the user's personal-space id; create it if missing
        ("My Space").

        Called on every request; check + create happen entirely under the
        lock: otherwise parallel requests from the same user (the frontend
        fires me/config/spaces at once) would each create a personal space —
        (owner, is_personal) has no unique constraint, and the uuid PK
        cannot catch this kind of duplicate.

        The concurrency guarantee is per-process only (_lock is an
        in-process lock). Before a multi-worker deployment, add a partial
        unique index on `is_personal=1` at the DB level, or switch to an
        advisory lock; the current single worker is safe.
        """
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM spaces WHERE owner=? AND is_personal=1 LIMIT 1",
                (username,),
            ).fetchone()
            if row:
                return row[0]
            space_id = uuid.uuid4().hex
            self._create_space_locked(space_id, "My Space", "", True, username, now)
            return space_id

    def close(self) -> None:
        with self._lock:
            self._conn.close()
