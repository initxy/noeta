"""MCP connector configuration types, kernel-side.

The connector itself (stdio / HTTP clients, ``McpTool``, discovery) lives in
the ``mcp`` built-in plugin, reachable only through the loader's dynamic
doorway. What lives here is the vocabulary both sides of that doorway must
agree on: the reserved tool-name prefix, the two public error types, the
operator-authored server specs, and the HTTP transport type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Union


__all__ = [
    "HttpPostFn",
    "MCP_PREFIX",
    "McpAnyServerSpec",
    "McpConfigError",
    "McpError",
    "McpHttpResponse",
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


@dataclass(frozen=True, slots=True)
class McpHttpResponse:
    """One HTTP response to a posted JSON-RPC request.

    ``body`` is what the client parses; ``headers`` and ``status`` are the
    envelope a Streamable HTTP session needs — the client reads exactly one
    header out of them, ``Mcp-Session-Id``, and treats it like a credential
    (echoed on the wire, never logged, recorded, or folded into provenance).
    Returning this instead of plain bytes is what lets a transport join a
    stateful server; a transport that returns bytes runs stateless.
    """

    body: bytes
    headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    status: int = 200


#: The HTTP POST entrypoint: JSON-RPC request object + merged request headers
#: in, the response out. Returning raw body ``bytes`` is the original shape and
#: stays fully supported — such a connection simply runs stateless. Returning an
#: :class:`McpHttpResponse` also hands back the response headers, so the client
#: can pick up the ``Mcp-Session-Id`` a Streamable HTTP server assigns and echo
#: it on every later request. Injectable so a test can substitute a fake
#: transport and prove resume NEVER reaches it.
HttpPostFn = Callable[
    [dict[str, Any], Mapping[str, str]], Union[bytes, McpHttpResponse]
]


def _check_call_timeout(alias: str, call_timeout_s: Optional[float]) -> None:
    if call_timeout_s is not None and not call_timeout_s > 0:
        raise McpConfigError(
            f"MCP server {alias!r} call_timeout_s must be > 0, got {call_timeout_s!r}"
        )


def _check_deferred(alias: str, deferred: object) -> None:
    if not isinstance(deferred, bool):
        raise McpConfigError(
            f"MCP server {alias!r} deferred must be a bool, got {deferred!r}"
        )


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """One operator-named local stdio MCP server.

    ``argv`` is the launch command; it is never run through a shell. ``env``
    rides into the scrubbed environment at spawn time only — it never enters
    any event or recording. ``tool_subset`` is the per-server **raw tool
    name** allow-list (``None`` ⇒ keep every advertised tool): names outside
    it never enter the tool set, so they never reach the model, and the
    subset itself stays host-side and never rides a request body.
    ``call_timeout_s`` bounds one ``tools/call`` (``None`` ⇒ the connector's
    30 s default); the handshake and the list calls keep the default.
    ``deferred`` keeps the server's tool schemas out of the provider tool
    list: the tools stay registered, guarded and audited under their real
    ``mcp__{alias}__{tool}`` names, and the model reaches them through the
    ``ToolSearch`` / ``McpCall`` pair advertised once for every deferred
    server (``False`` ⇒ every schema rides every request, as before)."""

    alias: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    tool_subset: Optional[tuple[str, ...]] = None
    call_timeout_s: Optional[float] = None
    deferred: bool = False

    def __post_init__(self) -> None:
        if not _ALIAS_RE.match(self.alias):
            raise McpConfigError(
                f"invalid MCP server alias {self.alias!r} "
                "(must match ^[a-z0-9_-]{1,32}$)"
            )
        if not self.argv or not self.argv[0]:
            raise McpConfigError(f"MCP server {self.alias!r} has an empty command")
        _check_call_timeout(self.alias, self.call_timeout_s)
        _check_deferred(self.alias, self.deferred)

    def env_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.env}


@dataclass(frozen=True, slots=True)
class McpHttpServerSpec:
    """One remote HTTP MCP server.

    ``url`` is the single JSON-RPC endpoint; ``headers`` carry the static
    credential headers injected on every request. **Credentials live here
    only** — they ride on the wire and are NEVER written to any event,
    recording, or request body. ``tool_subset`` is the same per-server
    raw-name allow-list as the stdio spec, ``call_timeout_s`` the same
    per-``tools/call`` bound, and ``deferred`` the same schema deferral."""

    alias: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    tool_subset: Optional[tuple[str, ...]] = None
    call_timeout_s: Optional[float] = None
    deferred: bool = False

    def __post_init__(self) -> None:
        if not _ALIAS_RE.match(self.alias):
            raise McpConfigError(
                f"invalid MCP server alias {self.alias!r} "
                "(must match ^[a-z0-9_-]{1,32}$)"
            )
        if not self.url:
            raise McpConfigError(f"MCP server {self.alias!r} has an empty url")
        _check_call_timeout(self.alias, self.call_timeout_s)
        _check_deferred(self.alias, self.deferred)

    def headers_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.headers}


McpAnyServerSpec = Union[McpServerSpec, McpHttpServerSpec]
