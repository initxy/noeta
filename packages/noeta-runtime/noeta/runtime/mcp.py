"""MCP connector configuration types, kernel-side.

The connector itself (stdio / HTTP clients, ``McpTool``, discovery) lives in
the ``mcp`` built-in plugin, reachable only through the loader's dynamic
doorway. What lives here is the vocabulary both sides of that doorway must
agree on: the reserved tool-name prefix, the two public error types, the
operator-authored server specs, and the HTTP transport type.
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


#: The HTTP POST entrypoint: JSON-RPC request object + merged request headers
#: in, raw response body bytes out. Injectable so a test can substitute a fake
#: transport and prove resume NEVER reaches it.
HttpPostFn = Callable[[dict[str, Any], Mapping[str, str]], bytes]


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """One operator-named local stdio MCP server.

    ``argv`` is the launch command; it is never run through a shell. ``env``
    rides into the scrubbed environment at spawn time only — it never enters
    any event or recording. ``tool_subset`` is the per-server **raw tool
    name** allow-list (``None`` ⇒ keep every advertised tool): names outside
    it never enter the tool set, so they never reach the model, and the
    subset itself stays host-side and never rides a request body."""

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
    credential headers injected on every request. **Credentials live here
    only** — they ride on the wire and are NEVER written to any event,
    recording, or request body. ``tool_subset`` is the same per-server
    raw-name allow-list as the stdio spec."""

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


McpAnyServerSpec = Union[McpServerSpec, McpHttpServerSpec]
