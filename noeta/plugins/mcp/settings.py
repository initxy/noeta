"""MCP servers from settings files, in Claude Code's ``mcpServers`` format::

    {"mcpServers": {
        "github": {"type": "http", "url": "https://...",
                   "headers": {"Authorization": "Bearer ${GH_TOKEN}"}},
        "files": {"command": "npx", "args": ["-y", "some-server"],
                  "env": {"ROOT": "${HOME:-/tmp}"}}
    }}

``type`` is ``stdio`` (the default with a ``command``), ``http`` (also
spelled ``streamable-http``; the default with a ``url``), ``sse`` (the
legacy HTTP+SSE transport) or ``ws``. A server's ``timeout`` (milliseconds)
bounds each of its tool calls; ``alwaysLoad: true`` sends its tools' schemas
from the first request instead of deferring them. A remote server may add
``headersHelper`` (a shell command printing a JSON object of headers when a
connection opens), and an ``http`` / ``sse`` one an ``oauth`` block
(``clientId``, ``callbackPort``, ``authServerMetadataUrl``, ``scopes``);
``bareElicitationCapability: true`` is for servers that reject the
elicitation modes. ``${VAR}`` and ``${VAR:-default}`` are filled from the
environment in ``command``, ``args``, ``env``, ``url``, ``headers`` and
``headersHelper``.

A workspace's ``.noeta/mcp.json`` (Claude Code's ``.mcp.json``, in noeta's
place) uses the same format, but a server in it starts only in a trusted
workspace and with the user's consent for that workspace, given with Claude
Code's keys in one of the workspace's own settings files
(``.noeta/settings.local.json``, as Claude Code keeps it per project):
``enableAllProjectMcpServers: true``, or the server's name in
``enabledMcpjsonServers``. A name in ``disabledMcpjsonServers`` of any file
keeps it off. Consent given elsewhere (the user's settings) is not consent
for every repository that reuses the name: a cloned repository must not
start processes by itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from noeta.plugins.mcp.config import McpConfigError, McpServer, OAuthSettings

__all__ = ["PROJECT_FILE", "alias_for", "read_servers", "servers_from"]

log = logging.getLogger("noeta.mcp")

#: A workspace's MCP servers, relative to its directory.
PROJECT_FILE = ".noeta/mcp.json"

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def alias_for(name: str) -> str:
    """A server name as an alias: every character outside ``[A-Za-z0-9_-]``
    replaced by ``_``, case kept, nothing cut (Claude Code's)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "_"


def _expand(value: str) -> str:
    return _VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)


def _strings(value: Any, what: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise McpConfigError(f"{what} must be an object of strings")
    return {str(k): _expand(str(v)) for k, v in value.items()}


def _call_timeout(ms: Any) -> float | None:
    """A server's ``timeout`` (milliseconds) in seconds; as in Claude Code,
    one under 1000 ms (or not a number) is ignored."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)) or ms < 1000:
        return None
    return float(ms) / 1000


def _oauth(name: str, spec: Any) -> OAuthSettings | None:
    if spec is None:
        return None
    if not isinstance(spec, Mapping):
        raise McpConfigError(f"MCP server {name!r}: 'oauth' must be an object")
    port = spec.get("callbackPort")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int)
                             or not 0 < port <= 65535):
        raise McpConfigError(f"MCP server {name!r}: oauth.callbackPort must be a port number")
    metadata = str(spec.get("authServerMetadataUrl") or "")
    if metadata and not metadata.startswith("https://"):
        raise McpConfigError(f"MCP server {name!r}: authServerMetadataUrl must use https://")
    return OAuthSettings(client_id=str(spec.get("clientId") or ""), callback_port=port,
                         auth_server_metadata_url=metadata,
                         scopes=str(spec.get("scopes") or ""))


def _server(name: str, spec: Any) -> McpServer:
    if not isinstance(spec, Mapping):
        raise McpConfigError(f"MCP server {name!r}: must be an object")
    kind = spec.get("type") or ("stdio" if spec.get("command") else "http")
    if kind == "streamable-http":
        kind = "http"
    common: dict[str, Any] = {
        "alias": alias_for(name), "call_timeout": _call_timeout(spec.get("timeout")),
        "deferred": spec.get("alwaysLoad") is not True,
        "bare_elicitation": spec.get("bareElicitationCapability") is True}
    if kind == "stdio":
        args = spec.get("args") or []
        if not isinstance(args, list):
            raise McpConfigError(f"MCP server {name!r}: 'args' must be a list")
        return McpServer(command=[_expand(str(spec.get("command") or "")),
                                  *(_expand(str(a)) for a in args)],
                         env=_strings(spec.get("env") or {}, f"MCP server {name!r} env"),
                         **common)
    if kind in ("http", "sse", "ws"):
        return McpServer(type=kind, url=_expand(str(spec.get("url") or "")),
                         headers=_strings(spec.get("headers") or {},
                                          f"MCP server {name!r} headers"),
                         headers_helper=_expand(str(spec.get("headersHelper") or "")),
                         oauth=_oauth(name, spec.get("oauth")) if kind != "ws" else None,
                         **common)
    raise McpConfigError(f"MCP server {name!r}: unknown type {kind!r} (valid types are "
                         "stdio, sse, http (or streamable-http), ws)")


def _read(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("mcp: cannot read %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def servers_from(data: Mapping[str, Any], source: str,
                 allow: Any = None) -> list[McpServer]:
    """The servers of one settings mapping; ``allow(name)`` filters them.
    A server that cannot be read is logged and skipped."""
    table = data.get("mcpServers") or {}
    if not isinstance(table, dict):
        log.warning("mcp: %s: 'mcpServers' must be an object", source)
        return []
    out: list[McpServer] = []
    for name, spec in table.items():
        if allow is not None and not allow(name):
            continue
        try:
            out.append(_server(str(name), spec))
        except McpConfigError as exc:
            log.warning("mcp: %s: %s", source, exc)
    return out


def _own(path: str | Path, project: Path) -> bool:
    """``path`` is one of ``project``'s own settings files."""
    try:
        return Path(path).expanduser().resolve().parent == project / ".noeta"
    except OSError:
        return False


def read_servers(settings_files: Sequence[str | Path],
                 project_dirs: Iterable[str] = ()) -> list[McpServer]:
    """The servers of ``project_dirs``' ``.noeta/mcp.json`` the project's own
    settings files (among ``settings_files``) consent to, then those of
    ``settings_files``; the first of an alias wins. A host passes only
    trusted directories as ``project_dirs``."""
    settings = [_read(Path(f).expanduser()) for f in settings_files]
    disabled = {str(n) for s in settings for n in s.get("disabledMcpjsonServers") or []}
    out: list[McpServer] = []
    for d in project_dirs:
        project = Path(d).expanduser().resolve()
        own = [s for f, s in zip(settings_files, settings, strict=True) if _own(f, project)]
        enable_all = any(s.get("enableAllProjectMcpServers") is True for s in own)
        enabled = {str(n) for s in own for n in s.get("enabledMcpjsonServers") or []}

        def consented(name: str, enable_all: bool = enable_all,
                      enabled: set[str] = enabled) -> bool:
            return name not in disabled and (enable_all or name in enabled)

        path = project / PROJECT_FILE
        data = _read(path)
        out += servers_from(data, str(path), consented)
        waiting = sorted(n for n in data.get("mcpServers") or {} if not consented(str(n)))
        if waiting:
            log.warning("mcp: %s names servers not started without consent (%s); add them "
                        "to enabledMcpjsonServers in %s", path, ", ".join(waiting),
                        project / ".noeta" / "settings.local.json")
    for f, data in zip(settings_files, settings, strict=True):
        out += servers_from(data, str(f))
    return out
