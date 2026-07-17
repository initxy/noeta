"""Template store (sqlite3, WAL, thread-safe within the process).

Two tables:

- **templates** (single-node templates): `name + description + prompt (with
  {param} placeholders) + parameter definition list`. name is unique within
  a space; params_json is
  ``[{"name": str, "description": str, "required": bool}]``.
- **workflow_templates** (workflow templates): an ordered node list whose
  nodes **reference** single-node templates
  (nodes_json = ``[{"template_id": str}]``). Deleting a referenced
  single-node template is blocked by the API layer via
  :meth:`workflows_referencing`.

Templates are pure data (no file payload) and live in the DB, not in files.
At workflow start the session side takes a **snapshot**
(sessions.workflow_json); sessions in progress are unaffected by template
edits.

Schema-in-code; boilerplate mirrors store/skills.py.
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
CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    prompt      TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '[]',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE (space_id, name)
);
CREATE INDEX IF NOT EXISTS idx_templates_space ON templates(space_id);

CREATE TABLE IF NOT EXISTS workflow_templates (
    id          TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    nodes_json  TEXT NOT NULL DEFAULT '[]',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE (space_id, name)
);
CREATE INDEX IF NOT EXISTS idx_workflow_templates_space
    ON workflow_templates(space_id);
"""

_T_COLS = "id,space_id,name,description,prompt,params_json,created_at,updated_at"
_W_COLS = "id,space_id,name,description,nodes_json,created_at,updated_at"


def _loads(text: str, fallback):
    try:
        val = json.loads(text)
    except (ValueError, TypeError):
        return fallback
    return val if isinstance(val, type(fallback)) else fallback


def _template_row(row: tuple) -> dict:
    return {
        "id": row[0],
        "space_id": row[1],
        "name": row[2],
        "description": row[3],
        "prompt": row[4],
        "params": _loads(row[5], []),
        "created_at": row[6],
        "updated_at": row[7],
    }


def _workflow_row(row: tuple) -> dict:
    return {
        "id": row[0],
        "space_id": row[1],
        "name": row[2],
        "description": row[3],
        "nodes": _loads(row[4], []),
        "created_at": row[5],
        "updated_at": row[6],
    }


class TemplateStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    # ------------------------------------------------------ single-node templates
    def create_template(
        self,
        space_id: str,
        name: str,
        description: str,
        prompt: str,
        params: list[dict],
    ) -> dict:
        now = time.time()
        tid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                f"INSERT INTO templates ({_T_COLS}) VALUES (?,?,?,?,?,?,?,?)",
                (tid, space_id, name, description, prompt,
                 json.dumps(params, ensure_ascii=False), now, now),
            )
        return self.get_template(space_id, tid)  # type: ignore[return-value]

    def get_template(self, space_id: str, template_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_T_COLS} FROM templates WHERE space_id=? AND id=?",
                (space_id, template_id),
            ).fetchone()
        return _template_row(row) if row else None

    def get_template_by_name(self, space_id: str, name: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_T_COLS} FROM templates WHERE space_id=? AND name=?",
                (space_id, name),
            ).fetchone()
        return _template_row(row) if row else None

    def list_templates(self, space_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_T_COLS} FROM templates WHERE space_id=?"
                " ORDER BY name ASC",
                (space_id,),
            ).fetchall()
        return [_template_row(r) for r in rows]

    def update_template(self, space_id: str, template_id: str, **fields) -> None:
        """fields ∈ {name, description, prompt, params}; params is
        JSON-encoded automatically."""
        if "params" in fields:
            fields["params_json"] = json.dumps(
                fields.pop("params"), ensure_ascii=False
            )
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE templates SET {cols} WHERE space_id=? AND id=?",
                (*fields.values(), space_id, template_id),
            )

    def delete_template(self, space_id: str, template_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM templates WHERE space_id=? AND id=?",
                (space_id, template_id),
            )

    # -------------------------------------------------------- workflow templates
    def create_workflow(
        self, space_id: str, name: str, description: str, nodes: list[dict]
    ) -> dict:
        now = time.time()
        wid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                f"INSERT INTO workflow_templates ({_W_COLS})"
                " VALUES (?,?,?,?,?,?,?)",
                (wid, space_id, name, description,
                 json.dumps(nodes, ensure_ascii=False), now, now),
            )
        return self.get_workflow(space_id, wid)  # type: ignore[return-value]

    def get_workflow(self, space_id: str, workflow_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_W_COLS} FROM workflow_templates"
                " WHERE space_id=? AND id=?",
                (space_id, workflow_id),
            ).fetchone()
        return _workflow_row(row) if row else None

    def get_workflow_by_name(self, space_id: str, name: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_W_COLS} FROM workflow_templates"
                " WHERE space_id=? AND name=?",
                (space_id, name),
            ).fetchone()
        return _workflow_row(row) if row else None

    def list_workflows(self, space_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_W_COLS} FROM workflow_templates WHERE space_id=?"
                " ORDER BY name ASC",
                (space_id,),
            ).fetchall()
        return [_workflow_row(r) for r in rows]

    def update_workflow(self, space_id: str, workflow_id: str, **fields) -> None:
        """fields ∈ {name, description, nodes}; nodes is JSON-encoded
        automatically."""
        if "nodes" in fields:
            fields["nodes_json"] = json.dumps(fields.pop("nodes"), ensure_ascii=False)
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE workflow_templates SET {cols} WHERE space_id=? AND id=?",
                (*fields.values(), space_id, workflow_id),
            )

    def delete_workflow(self, space_id: str, workflow_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM workflow_templates WHERE space_id=? AND id=?",
                (space_id, workflow_id),
            )

    def workflows_referencing(self, space_id: str, template_id: str) -> list[str]:
        """Names of workflow templates that reference a given single-node
        template (used as a deletion guard).

        nodes_json has no queryable index; the per-space workflow count is
        tiny, so fetch everything and filter in memory.
        """
        names: list[str] = []
        for wf in self.list_workflows(space_id):
            if any(n.get("template_id") == template_id for n in wf["nodes"]):
                names.append(wf["name"])
        return names

    def close(self) -> None:
        with self._lock:
            self._conn.close()
