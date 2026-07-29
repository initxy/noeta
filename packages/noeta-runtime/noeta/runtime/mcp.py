"""MCP vocabulary — the connector *configuration* types, kernel-side.

Microkernel M3: the MCP connector implementation (the stdio / HTTP clients,
``McpTool``, discovery, prompts, resources) moved into the ``mcp`` built-in
plugin (``noeta.builtins.mcp.impl``), reached SDK-side only through the
loader's dynamic-import doorway. What sinks here is the **vocabulary** both
sides of that doorway speak — the M2 ``noeta.runtime.governance`` precedent:

* the reserved tool-name prefix (:data:`MCP_PREFIX`) the kernel builder's
  merge stage enforces against built-in collisions;
* the fail-fast config error (:class:`McpConfigError`) that check raises and
  the wire fault (:class:`McpError`) the clients raise — both on the public
  ``noeta.sdk`` error surface;
* the operator-authored server specs (:class:`McpServerSpec` /
  :class:`McpHttpServerSpec` / :data:`McpAnyServerSpec`) the host's
  ``mcp_server_resolver`` seam returns; and
* the injectable HTTP transport type (:data:`HttpPostFn`) that seam pairs
  with.

Everything here was moved verbatim from ``noeta.tools.mcp`` (that package is
gone); the connector behaviour lives with the impl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Union


__all__ = [
    "HttpPostFn",
    "MCP_PREFIX",
    "McpAnyServerSpec",
    "McpConfigError",
    "McpError",
    "McpHttpServerSpec",
    "McpServerSpec",
]


MCP_PREFIX = "mcp__"

_ALIAS_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class McpConfigError(ValueError):
    """A fail-fast MCP configuration / discovery fault (bad alias, name
    collision, unmappable tool name). Raised at config-parse or
    ``prepare`` time — never swallowed into a ``ToolResult``."""


class McpError(Exception):
    """A transport / protocol / timeout fault talking to an MCP server.

    Always caught by ``McpTool.invoke`` and turned into a typed failed
    ``ToolResult`` (never propagates out of a tool call). At ``prepare``
    time (spawn / initialize / tools-list) it propagates as a fail-fast.
    """


#: The HTTP POST entrypoint. Injectable so tests can substitute a fake
#: transport (and prove resume NEVER reaches it). Takes the JSON-RPC request
#: object + the merged request headers; returns the raw response body bytes.
HttpPostFn = Callable[[dict[str, Any], Mapping[str, str]], bytes]


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """One operator-named local stdio MCP server. ``argv`` is the launch
    command (``argv[0]`` + args); never run through a shell.

    ``env`` carries extra environment variables for the spawned process
    (issue 02 — stdio configured from the front-end). It rides into
    the scrubbed env at spawn time only; it never enters any event/recording.

    ``tool_subset`` is the per-server **raw tool name** allow-list
    chosen by the user at config time: ``None`` ⇒ keep every advertised tool
    (back-compat); a tuple ⇒ keep only those whose raw ``tools/list`` name is in
    the set (others never enter the tool set / never reach the model). The subset
    lives host-side and never rides a request body (D3)."""

    alias: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    tool_subset: Optional[tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if not _ALIAS_RE.match(self.alias):
            raise McpConfigError(
                f"invalid MCP server alias {self.alias!r} "
                "(must match ^[a-z0-9_-]{1,32}$)"
            )
        if not self.argv or not self.argv[0]:
            raise McpConfigError(f"MCP server {self.alias!r} has an empty command")

    def env_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.env}


@dataclass(frozen=True, slots=True)
class McpHttpServerSpec:
    """One remote HTTP MCP server.

    ``url`` is the single JSON-RPC endpoint; ``headers`` carry the static
    credential / custom headers (a Bearer token / API key) injected on every
    request (D5). **Credentials live here only** — passed from the host-side
    config store at build time; they ride on the wire and are NEVER written to
    any event, recording, or request body (D3). The discovered tools are wrapped
    as the same ``mcp__{alias}__{tool}`` ``McpTool``s as the stdio path, so
    naming / collision / R-1 resume-rebuild are shared verbatim.

    ``tool_subset``: same per-server raw-name allow-list as the
    stdio spec — ``None`` ⇒ keep all; a tuple ⇒ keep only those raw names."""

    alias: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    tool_subset: Optional[tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if not _ALIAS_RE.match(self.alias):
            raise McpConfigError(
                f"invalid MCP server alias {self.alias!r} "
                "(must match ^[a-z0-9_-]{1,32}$)"
            )
        if not self.url:
            raise McpConfigError(f"MCP server {self.alias!r} has an empty url")

    def headers_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.headers}


#: A server spec the live build can connect: local stdio (``McpServerSpec``) or
#: remote HTTP (``McpHttpServerSpec``). Both map to the same ``McpTool`` set.
McpAnyServerSpec = Union[McpServerSpec, McpHttpServerSpec]
