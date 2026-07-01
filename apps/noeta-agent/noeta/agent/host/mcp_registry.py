"""MCP server registry — host-side config store for MCP connectors.

Holds the operator/user-configured MCP servers (alias + transport + url +
credentials) persisted to a JSON file (default ``~/.noeta/mcp_servers.json``).
Mirrors :class:`WorkspaceRegistry` / :class:`SessionRecordStore`: an in-memory
map written on every mutation, ``load()`` once at server startup.

**Why host-side:** the
server config — and especially the **credentials** (a Bearer token / API key /
custom header) — live ONLY here. A chat/task request body carries just the
enabled **alias clean list** (``["github", "notion"]``) — never a url or token.
The backend resolves an alias → this store → the full spec (with credentials) →
connects the server. So credentials never ride a request body, never enter any
event/recording, and the per-task replay determinism is whatever R-1 already
guarantees (the recorded tool spec is the durable truth).

Transports: ``type="http"`` (remote HTTP, issue 01) and ``type="stdio"`` (a
local subprocess configured from the front-end, issue 02, given
noeta-agent's personal-single-machine positioning). A ``stdio`` entry carries
``command`` + ``args`` + ``env`` instead of ``url`` + ``headers``. The
OAuth refresh fields are reserved as an optional ``refresh`` blob
(left ``None`` in v1).

``tools`` (issue 02) is the optional per-server **tool subset** —
the raw ``tools/list`` names the user ticked at config time. ``None`` ⇒ every
advertised tool is enabled (back-compat); a list ⇒ only those are wrapped into
the task tool set. The subset lives host-side here and never rides a request
body (D3) — a task request still carries only the enabled alias clean list.

On-disk shape is a per-alias **record object** keyed by alias::

    {
      "github": {
        "type": "http",
        "url": "https://mcp.example.com/github",
        "headers": {"Authorization": "Bearer ghp_…"},
        "tools": ["create_issue"],
        "refresh": null
      },
      "fs": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {"DEBUG": "1"},
        "tools": null,
        "refresh": null
      }
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from noeta.tools.mcp import (
    McpAnyServerSpec,
    McpConfigError,
    McpHttpServerSpec,
    McpServerSpec,
    build_mcp_tools,
    discover_prompts,
    discover_resources,
    expand_prompt,
    read_resource,
)
from noeta.tools.mcp._client import SpawnFn
from noeta.tools.mcp._http_client import HttpPostFn


__all__ = ["McpServerEntry", "McpServerRegistry"]


@dataclass
class McpServerEntry:
    """One configured MCP server.

    HTTP transport: ``url`` + ``headers`` (the latter holds the credential /
    custom headers — sent on the wire only, never recorded, D3). stdio
    transport (D6): ``command`` + ``args`` + ``env``. ``tools`` is the optional
    per-server tool subset (raw names); ``None`` ⇒ keep all."""

    alias: str
    type: str  # "http" | "stdio"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    #: The user-ticked raw tool-name allow-list; ``None`` ⇒ all.
    tools: Optional[list[str]] = None
    #: Reserved OAuth refresh blob; ``None`` in v1 (static creds).
    refresh: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "url": self.url,
            "headers": dict(self.headers),
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "tools": list(self.tools) if self.tools is not None else None,
            "refresh": self.refresh,
        }

    def as_public_dict(self) -> dict[str, Any]:
        """A credential-SCRUBBED view for the management API / front-end.

        Returns the header **names** only (so the UI can show "Authorization is
        set") but NEVER the header values — a token must not leave the host even
        on the management surface that configured it. The alias / type / url /
        command / args / tool subset are safe to echo back; ``env`` values are
        scrubbed to **names** only (an env var may carry a secret too)."""
        return {
            "alias": self.alias,
            "type": self.type,
            "url": self.url,
            "header_names": sorted(self.headers.keys()),
            "command": self.command,
            "args": list(self.args),
            "env_names": sorted(self.env.keys()),
            "tools": list(self.tools) if self.tools is not None else None,
            "has_refresh": self.refresh is not None,
        }

    @staticmethod
    def from_dict(alias: str, d: dict[str, Any]) -> "McpServerEntry":
        headers_raw = d.get("headers")
        headers = (
            {str(k): str(v) for k, v in headers_raw.items()}
            if isinstance(headers_raw, dict)
            else {}
        )
        env_raw = d.get("env")
        env = (
            {str(k): str(v) for k, v in env_raw.items()}
            if isinstance(env_raw, dict)
            else {}
        )
        args_raw = d.get("args")
        args = [str(a) for a in args_raw] if isinstance(args_raw, list) else []
        tools_raw = d.get("tools")
        tools = (
            [str(t) for t in tools_raw] if isinstance(tools_raw, list) else None
        )
        refresh = d.get("refresh")
        return McpServerEntry(
            alias=alias,
            type=str(d.get("type", "http")),
            url=str(d.get("url", "")),
            headers=headers,
            command=str(d.get("command", "")),
            args=args,
            env=env,
            tools=tools,
            refresh=refresh if isinstance(refresh, dict) else None,
        )

    def to_spec(self) -> McpAnyServerSpec:
        """Build the SDK-level connect spec (with credentials) for this entry.

        ``http`` → :class:`McpHttpServerSpec` (url + header tuple); ``stdio`` →
        :class:`McpServerSpec` (argv = command + args, env). Both carry the
        per-server ``tool_subset`` so the SDK's ``build_mcp_tools`` keeps only the
        ticked tools (D6). Raises :class:`McpConfigError` on an unknown type."""
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
            f"MCP server {self.alias!r} has unsupported type {self.type!r} "
            "(supported: 'http', 'stdio')"
        )


class McpServerRegistry:
    """In-memory map of ``alias -> McpServerEntry``, persisted to a JSON file.

    Args:
        path: Path to the JSON persistence file. The parent directory is created
            on the first write if it does not exist.
        spawn: Optional process-launch entrypoint injected into the stdio client
            for :meth:`discover_tools` (tests pass a fake; production leaves it
            ``None`` ⇒ real subprocess).
        http_post: Optional HTTP POST transport injected into the HTTP client for
            :meth:`discover_tools` (tests pass a fake; production leaves it
            ``None`` ⇒ stdlib ``urllib``).
    """

    def __init__(
        self,
        path: Path,
        *,
        spawn: Optional[SpawnFn] = None,
        http_post: Optional[HttpPostFn] = None,
    ) -> None:
        self._path = path
        self._entries: dict[str, McpServerEntry] = {}
        self._spawn = spawn
        self._http_post = http_post

    # ------------------------------------------------------------------
    # Persistence

    def load(self) -> None:
        """Load entries from the JSON file. No-op if the file is absent.

        Tolerant by design (a config store must never crash a boot): a missing,
        unreadable, or malformed file leaves the store empty.
        """
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        entries: dict[str, McpServerEntry] = {}
        for alias, rec in raw.items():
            if isinstance(rec, dict):
                try:
                    entries[str(alias)] = McpServerEntry.from_dict(str(alias), rec)
                except (KeyError, TypeError, ValueError):
                    pass  # skip malformed entries
        self._entries = entries

    def _save(self) -> None:
        """Persist current entries to the JSON file, creating dirs as needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {a: e.as_dict() for a, e in self._entries.items()}
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Mutations

    def upsert_http(
        self,
        *,
        alias: str,
        url: str,
        headers: Optional[dict[str, str]] = None,
        tools: Optional[list[str]] = None,
    ) -> McpServerEntry:
        """Add or replace an ``http`` server config.

        The alias is validated against the SDK's alias rule by constructing the
        spec (``McpHttpServerSpec.__post_init__`` raises :class:`McpConfigError`
        on a bad alias / empty url). Credentials in ``headers`` are stored
        host-side only (D3). ``tools`` is the optional raw tool-name subset (D6).
        """
        entry = McpServerEntry(
            alias=alias,
            type="http",
            url=url,
            headers=dict(headers or {}),
            tools=list(tools) if tools is not None else None,
        )
        # Fail-fast validation: this raises McpConfigError on a bad alias / url.
        entry.to_spec()
        self._entries[alias] = entry
        self._save()
        return entry

    def upsert_stdio(
        self,
        *,
        alias: str,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        tools: Optional[list[str]] = None,
    ) -> McpServerEntry:
        """Add or replace a ``stdio`` server config.

        ``command`` + ``args`` form the launch argv (never run through a shell —
        the SDK's ``McpStdioClient`` spawns the argv list directly); ``env`` is
        merged onto the scrubbed base env at spawn. ``tools`` is the optional raw
        tool-name subset. The alias / empty-command rule is validated by
        constructing the spec (``McpServerSpec.__post_init__`` fail-fast).
        """
        entry = McpServerEntry(
            alias=alias,
            type="stdio",
            command=command,
            args=list(args or []),
            env=dict(env or {}),
            tools=list(tools) if tools is not None else None,
        )
        # Fail-fast validation: raises McpConfigError on a bad alias / empty cmd.
        entry.to_spec()
        self._entries[alias] = entry
        self._save()
        return entry

    def set_tools(
        self, alias: str, tools: Optional[list[str]]
    ) -> Optional[McpServerEntry]:
        """Replace the per-server tool subset of an existing entry (D6).

        ``tools=None`` clears the subset (⇒ all advertised tools enabled);
        a list restricts to those raw names. Returns the updated entry, or
        ``None`` if the alias is not configured."""
        entry = self._entries.get(alias)
        if entry is None:
            return None
        entry.tools = list(tools) if tools is not None else None
        self._save()
        return entry

    def update_merge(
        self,
        alias: str,
        *,
        url: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        tools: Optional[list[str]] = None,
        clear_tools: bool = False,
    ) -> Optional[McpServerEntry]:
        """Edit an existing server, MERGING the given fields.

        Backs ``PUT /mcp-servers/{alias}``: a field passed ``None`` is **kept**
        (so editing a server's ``url`` need not re-paste its ``Authorization``
        token / ``env`` secrets — they survive untouched, D3). The transport
        ``type`` is immutable here (changing it would orphan the credentials of
        the old transport); a caller wanting to flip http⇄stdio re-POSTs a fresh
        config. ``clear_tools=True`` explicitly resets the subset to all-tools
        (``None``); otherwise ``tools`` (a list) replaces the subset and a
        missing ``tools`` keeps the current one. The merged entry is re-validated
        through ``to_spec`` (a bad alias / empty url / empty command raises
        :class:`McpConfigError`). Returns the updated entry, or ``None`` if the
        alias is not configured."""
        entry = self._entries.get(alias)
        if entry is None:
            return None
        merged = McpServerEntry(
            alias=alias,
            type=entry.type,
            url=url if url is not None else entry.url,
            headers=dict(headers) if headers is not None else dict(entry.headers),
            command=command if command is not None else entry.command,
            args=list(args) if args is not None else list(entry.args),
            env=dict(env) if env is not None else dict(entry.env),
            tools=(
                None
                if clear_tools
                else (list(tools) if tools is not None else entry.tools)
            ),
            refresh=entry.refresh,
        )
        # Fail-fast validation: raises McpConfigError on a bad alias / url / cmd.
        merged.to_spec()
        self._entries[alias] = merged
        self._save()
        return merged

    def delete(self, alias: str) -> bool:
        """Remove a server config by alias. Returns whether one was removed."""
        if alias not in self._entries:
            return False
        del self._entries[alias]
        self._save()
        return True

    # ------------------------------------------------------------------
    # Queries

    def list_all(self) -> list[McpServerEntry]:
        """All configured entries (alias-sorted for deterministic display)."""
        return [self._entries[a] for a in sorted(self._entries)]

    def get(self, alias: str) -> Optional[McpServerEntry]:
        return self._entries.get(alias)

    def resolve_spec(self, alias: str) -> Optional[McpAnyServerSpec]:
        """Resolve one enabled alias → its connect spec.

        This is the callback injected into the SDK host as
        ``mcp_server_resolver``: given an alias the frontend enabled, return the
        full spec (with credentials) to connect, or ``None`` when the alias is
        not configured (the host skips it). The SDK never sees the store itself —
        only this resolved spec, per turn. The returned spec carries the
        per-server ``tool_subset`` (D6), so the connected task only wraps the
        ticked tools.
        """
        entry = self._entries.get(alias)
        if entry is None:
            return None
        return entry.to_spec()

    def discover_tools(self, alias: str) -> Optional[list[dict[str, Any]]]:
        """Connect a configured server and list its FULL tool menu.

        Backs ``GET /mcp-servers/{alias}/tools`` so the config UI can show every
        advertised tool and let the user tick a subset. Connects with the entry's
        spec but **ignores its stored ``tools`` subset** (the menu must show all
        candidates, not just the already-ticked ones), shuts the client down, and
        returns ``[{name, description}]`` in advertised order. Returns ``None``
        when the alias is not configured; raises :class:`McpConfigError` /
        ``McpError`` on a connect / handshake failure (the HTTP layer maps it to
        a 502).
        """
        entry = self._entries.get(alias)
        if entry is None:
            return None
        # Build a subset-free clone so the menu lists every advertised tool.
        entry_full = McpServerEntry.from_dict(alias, {**entry.as_dict(), "tools": None})
        spec = entry_full.to_spec()
        tools, clients, _skipped = build_mcp_tools(
            (spec,), spawn=self._spawn, http_post=self._http_post
        )
        try:
            # Report the RAW tool names (what the UI ticks into the subset) +
            # descriptions, in deterministic (Noeta-name-sorted) order — the same
            # order ``build_mcp_tools`` already applied.
            return [
                {"name": t.remote_tool_name, "description": t.description}
                for t in tools.values()
            ]
        finally:
            for c in clients:
                c.shutdown()

    # ------------------------------------------------------------------
    # prompts (slash commands)

    def discover_prompts(self, alias: str) -> Optional[list[dict[str, Any]]]:
        """Connect a configured server and list its prompts.

        Backs ``GET /mcp-servers/{alias}/prompts`` so the chat composer can show
        each prompt as a ``/mcp__<alias>__<name>`` slash command and render a
        form from its declared ``arguments``. Returns
        ``[{name, noeta_name, description, arguments}]`` (the SDK's
        :func:`noeta.tools.mcp.discover_prompts` connects, lists, shuts down).
        Returns ``None`` when the alias is not configured; ``[]`` when the server
        advertises no prompts (an optional capability). Raises
        :class:`McpConfigError` / ``McpError`` on a connect / handshake failure
        (the HTTP layer maps it to a 502).
        """
        entry = self._entries.get(alias)
        if entry is None:
            return None
        spec = entry.to_spec()
        return discover_prompts(
            spec, spawn=self._spawn, http_post=self._http_post
        )

    def expand_prompt(
        self, alias: str, prompt_name: str, arguments: dict[str, Any]
    ) -> Optional[str]:
        """Connect a server and expand one prompt (``prompts/get``).

        Backs the prompt-as-slash-command goal injection: given the alias + raw
        prompt name + filled-in arguments (from the front-end form), returns the
        flattened text the host injects as that turn's opening user content
        (recorded as an ordinary ``origin="system"`` message; replay reads it
        back and never re-calls ``prompts/get``). Returns ``None`` when the alias
        is not configured; raises :class:`McpConfigError` / ``McpError`` on a
        connect / handshake / unknown-prompt failure (HTTP layer → typed error).
        """
        entry = self._entries.get(alias)
        if entry is None:
            return None
        spec = entry.to_spec()
        return expand_prompt(
            spec,
            prompt_name=prompt_name,
            arguments=dict(arguments),
            spawn=self._spawn,
            http_post=self._http_post,
        )

    # ------------------------------------------------------------------
    # resources (unified ``@`` mention)

    def discover_resources(self, alias: str) -> Optional[list[dict[str, Any]]]:
        """Connect a configured server and list its STATIC resources.

        Backs ``GET /mcp-servers/{alias}/resources`` so the chat composer's
        unified ``@`` selector can list each resource alongside workspace files.
        Returns ``[{uri, name, description, mime_type, noeta_ref}]`` (``noeta_ref``
        = the ``<alias>:<uri>`` mention token; the SDK's
        :func:`noeta.tools.mcp.discover_resources` connects, lists, shuts down).
        Returns ``None`` when the alias is not configured; ``[]`` when the server
        advertises no resources (an optional capability). Raises
        :class:`McpConfigError` / ``McpError`` on a connect / handshake failure
        (the HTTP layer maps it to a 502).
        """
        entry = self._entries.get(alias)
        if entry is None:
            return None
        spec = entry.to_spec()
        return discover_resources(
            spec, spawn=self._spawn, http_post=self._http_post
        )

    def read_resource(self, alias: str, uri: str) -> Optional[str]:
        """Connect a server and read one resource (``resources/read``).

        Backs the unified ``@<alias>:<uri>`` snapshot: given the alias + URI,
        returns the flattened text the host records as that turn's ``origin="system"``
        content (replay reads it back, never re-reading). Returns ``None`` when the
        alias is not configured; raises :class:`McpConfigError` / ``McpError`` on a
        connect / handshake / unknown-URI failure (HTTP layer → typed error).
        Credentials never leave the host (only the alias + URI rode the request, D3).
        """
        entry = self._entries.get(alias)
        if entry is None:
            return None
        spec = entry.to_spec()
        return read_resource(
            spec, uri=uri, spawn=self._spawn, http_post=self._http_post
        )
