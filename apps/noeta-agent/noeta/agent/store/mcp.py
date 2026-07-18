"""Per-space MCP connector store (sqlite3, WAL, thread-safe within the
process).

A connector belongs to a space and is keyed (space_id, alias) — the re-scope
of the retired app's global ``~/.noeta`` MCP registry onto the multi-user
platform (D9 item 1). It persists the transport config, the credential fields
(HTTP header values / stdio env values), the enabled flag, and the optional
enabled-tool subset.

**Credentials never leave the server.** Header and env VALUES are stored here
and ride only on the wire when a connector is actually connected (the SDK
receives them through the resolved spec, or the discovery client sends them on
its own requests). Every read path that feeds the API uses
:meth:`McpConnector.as_public_dict`, which scrubs values to sorted NAME lists —
the same rule as the retired registry's ``as_public_dict``.

``tools`` is the optional per-connector enabled-tool subset (raw ``tools/list``
names). ``None`` ⇒ every advertised tool is enabled; a list ⇒ only those are
wrapped into the task tool set (carried on the spec as ``tool_subset``).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from noeta.sdk import (
    McpAnyServerSpec,
    McpConfigError,
    McpHttpServerSpec,
    McpServerSpec,
)

VALID_TYPES = ("http", "stdio")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_connectors (
    space_id   TEXT NOT NULL,
    alias      TEXT NOT NULL,
    type       TEXT NOT NULL,
    url        TEXT NOT NULL DEFAULT '',
    headers    TEXT NOT NULL DEFAULT '{}',
    command    TEXT NOT NULL DEFAULT '',
    args       TEXT NOT NULL DEFAULT '[]',
    env        TEXT NOT NULL DEFAULT '{}',
    enabled    INTEGER NOT NULL DEFAULT 1,
    tools      TEXT,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (space_id, alias)
);
"""

# Column order matches every SELECT / _row_to_connector one-to-one.
_COLS = (
    "space_id,alias,type,url,headers,command,args,env,enabled,tools,"
    "created_by,created_at,updated_at"
)


def _load_str_dict(raw: Any) -> dict[str, str]:
    try:
        value = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        value = {}
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _load_str_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        value = []
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


@dataclass
class McpConnector:
    """One configured MCP connector of a space.

    HTTP transport: ``url`` + ``headers`` (the latter holds the credential /
    custom headers — sent on the wire only, never echoed). stdio transport:
    ``command`` + ``args`` + ``env``. ``tools`` is the optional enabled-tool
    subset (raw names); ``None`` ⇒ keep all advertised tools."""

    space_id: str
    alias: str
    type: str  # "http" | "stdio"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    tools: Optional[list[str]] = None
    created_by: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def as_public_dict(self) -> dict[str, Any]:
        """A credential-SCRUBBED view for the management API / frontend.

        Returns header and env NAMES only (so the UI can show "Authorization
        is set") but NEVER the values — a token must not leave the server even
        on the management surface that configured it. Alias / type / url /
        command / args / enabled / tool subset are safe to echo back."""
        return {
            "space_id": self.space_id,
            "alias": self.alias,
            "type": self.type,
            "url": self.url,
            "header_names": sorted(self.headers.keys()),
            "command": self.command,
            "args": list(self.args),
            "env_names": sorted(self.env.keys()),
            "enabled": self.enabled,
            "tools": list(self.tools) if self.tools is not None else None,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_spec(self) -> McpAnyServerSpec:
        """Build the SDK-level connect spec (WITH credentials).

        ``http`` → :class:`McpHttpServerSpec` (url + header tuple); ``stdio``
        → :class:`McpServerSpec` (argv = command + args, env). Both carry the
        per-connector ``tool_subset`` so the SDK keeps only the ticked tools.
        Raises :class:`McpConfigError` on a bad alias / empty url / empty
        command / unknown type — construction doubles as fail-fast
        validation."""
        subset = tuple(self.tools) if self.tools is not None else None
        if self.type == "http":
            return McpHttpServerSpec(
                alias=self.alias,
                url=self.url,
                headers=tuple(sorted(self.headers.items())),
                tool_subset=subset,
            )
        if self.type == "stdio":
            return McpServerSpec(
                alias=self.alias,
                argv=tuple([self.command, *self.args]),
                env=tuple(sorted(self.env.items())),
                tool_subset=subset,
            )
        raise McpConfigError(
            f"MCP connector {self.alias!r} has unsupported type {self.type!r} "
            "(supported: 'http', 'stdio')"
        )


def _row_to_connector(row: tuple) -> McpConnector:
    return McpConnector(
        space_id=row[0],
        alias=row[1],
        type=row[2],
        url=row[3] or "",
        headers=_load_str_dict(row[4]),
        command=row[5] or "",
        args=_load_str_list(row[6]),
        env=_load_str_dict(row[7]),
        enabled=bool(row[8]),
        tools=_load_str_list(row[9]) if row[9] is not None else None,
        created_by=row[10],
        created_at=row[11],
        updated_at=row[12],
    )


class McpConnectorStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ mutations

    def upsert(
        self,
        space_id: str,
        alias: str,
        *,
        connector_type: str,
        url: str = "",
        headers: Optional[dict[str, str]] = None,
        command: str = "",
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        tools: Optional[list[str]] = None,
        enabled: bool = True,
        created_by: str,
    ) -> McpConnector:
        """Add or replace a connector config (POST = create/replace,
        mirroring the retired registry's upsert semantics).

        Validation is fail-fast through spec construction: a bad alias /
        empty url / empty command / unknown type raises
        :class:`McpConfigError` before anything is written."""
        now = time.time()
        connector = McpConnector(
            space_id=space_id,
            alias=alias,
            type=connector_type,
            url=url,
            headers=dict(headers or {}),
            command=command,
            args=list(args or []),
            env=dict(env or {}),
            enabled=enabled,
            tools=list(tools) if tools is not None else None,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        connector.to_spec()  # fail-fast validation (McpConfigError)
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at, created_by FROM mcp_connectors"
                " WHERE space_id=? AND alias=?",
                (space_id, alias),
            ).fetchone()
            if existing:
                # A replace keeps the original provenance.
                connector.created_at = existing[0]
                connector.created_by = existing[1]
            self._conn.execute(
                f"INSERT OR REPLACE INTO mcp_connectors ({_COLS})"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                self._values(connector),
            )
        return connector

    def update_merge(
        self,
        space_id: str,
        alias: str,
        *,
        url: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        tools: Optional[list[str]] = None,
        clear_tools: bool = False,
        enabled: Optional[bool] = None,
    ) -> Optional[McpConnector]:
        """Edit an existing connector, MERGING the given fields.

        Backs ``PUT .../mcp/servers/{alias}``: a field passed ``None`` is
        KEPT, so editing a url need not re-paste the ``Authorization`` token /
        env secrets — they survive untouched. The transport ``type`` is
        immutable here (changing it would orphan the other transport's
        credentials); flipping http⇄stdio re-POSTs a fresh config.
        ``clear_tools=True`` explicitly resets the subset to all-tools
        (``None``); otherwise ``tools`` (a list) replaces the subset and a
        missing ``tools`` keeps the current one. The merged config is
        re-validated through ``to_spec`` (raises :class:`McpConfigError`).
        Returns the updated connector, or ``None`` when the alias is not
        configured in the space."""
        current = self.get(space_id, alias)
        if current is None:
            return None
        merged = McpConnector(
            space_id=space_id,
            alias=alias,
            type=current.type,
            url=url if url is not None else current.url,
            headers=dict(headers) if headers is not None else dict(current.headers),
            command=command if command is not None else current.command,
            args=list(args) if args is not None else list(current.args),
            env=dict(env) if env is not None else dict(current.env),
            enabled=enabled if enabled is not None else current.enabled,
            tools=(
                None
                if clear_tools
                else (list(tools) if tools is not None else current.tools)
            ),
            created_by=current.created_by,
            created_at=current.created_at,
            updated_at=time.time(),
        )
        merged.to_spec()  # fail-fast validation (McpConfigError)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO mcp_connectors ({_COLS})"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                self._values(merged),
            )
        return merged

    def set_tools(
        self, space_id: str, alias: str, tools: Optional[list[str]]
    ) -> Optional[McpConnector]:
        """Replace the enabled-tool subset. ``tools=None`` clears it (⇒ all
        advertised tools enabled). Returns the updated connector, or ``None``
        when the alias is not configured in the space."""
        return self.update_merge(
            space_id, alias, tools=tools, clear_tools=tools is None
        )

    def set_enabled(
        self, space_id: str, alias: str, enabled: bool
    ) -> Optional[McpConnector]:
        """Flip the enabled flag. Disabled connectors are excluded from new
        turns (list_enabled and resolve_spec both skip them); in-flight turns
        are unaffected."""
        return self.update_merge(space_id, alias, enabled=enabled)

    def delete(self, space_id: str, alias: str) -> bool:
        """Remove a connector. Returns whether one was removed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM mcp_connectors WHERE space_id=? AND alias=?",
                (space_id, alias),
            )
            return cur.rowcount > 0

    # -------------------------------------------------------------- queries

    def get(self, space_id: str, alias: str) -> Optional[McpConnector]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLS} FROM mcp_connectors"
                " WHERE space_id=? AND alias=?",
                (space_id, alias),
            ).fetchone()
        return _row_to_connector(row) if row else None

    def list_for_space(self, space_id: str) -> list[McpConnector]:
        """The space's connectors, alias-sorted for deterministic display."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM mcp_connectors"
                " WHERE space_id=? ORDER BY alias ASC",
                (space_id,),
            ).fetchall()
        return [_row_to_connector(r) for r in rows]

    def list_enabled_aliases(self, space_id: str) -> list[str]:
        """The space's ENABLED connector aliases (sorted) — what a new turn
        passes as its enabled-MCP set."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT alias FROM mcp_connectors"
                " WHERE space_id=? AND enabled=1 ORDER BY alias ASC",
                (space_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def resolve_spec(self, space_id: str, alias: str) -> Optional[McpAnyServerSpec]:
        """Resolve one enabled connector → its connect spec (WITH
        credentials).

        This backs the callback wired into the SDK host as
        ``mcp_server_resolver``: given a space + alias the turn enabled,
        return the full spec to connect, or ``None`` when the alias is not
        configured / disabled / invalid (the host skips it). The SDK never
        sees the store itself — only the resolved spec, per turn."""
        connector = self.get(space_id, alias)
        if connector is None or not connector.enabled:
            return None
        try:
            return connector.to_spec()
        except McpConfigError:
            # A row that no longer validates (should not happen — writes are
            # validated) is skipped rather than sinking the turn.
            return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _values(c: McpConnector) -> tuple:
        return (
            c.space_id,
            c.alias,
            c.type,
            c.url,
            json.dumps(c.headers, ensure_ascii=False),
            c.command,
            json.dumps(c.args, ensure_ascii=False),
            json.dumps(c.env, ensure_ascii=False),
            1 if c.enabled else 0,
            json.dumps(c.tools, ensure_ascii=False) if c.tools is not None else None,
            c.created_by,
            c.created_at,
            c.updated_at,
        )
