"""MCP vocabulary: server specs, errors, tool naming and the text caps.

Naming (Claude Code's): a server tool is offered to the model as
``mcp__{alias}__{safe}``, where ``safe`` is the server's raw tool name with
every character outside ``[A-Za-z0-9_-]`` replaced by ``_``, case kept and
nothing cut. Two raw names of one server that sanitize alike are told apart
with ``_`` plus 8 hex digits of the raw name's sha256 (a raw name that is
already safe keeps its plain form), so one awkward name never costs the rest
of the server's tools. A full name over :data:`NAME_MAX` characters (the
Messages API's limit) is not offered: the server's log says so and its other
tools stay. A provider with a shorter limit maps names in its adapter
(``noeta/backends/providers/_common.py`` ``wire_tool_name``). ``mcp__`` is a
reserved prefix.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TOOL_TIMEOUT_S",
    "DESCRIPTION_MAX_CHARS",
    "LOADED_DESCRIPTION_MAX_CHARS",
    "MAX_TIMEOUT_S",
    "MCP_PREFIX",
    "NAME_MAX",
    "TRANSPORTS",
    "McpAuthRequired",
    "McpConfigError",
    "McpError",
    "McpServer",
    "McpServerError",
    "OAuthSettings",
    "cap_text",
    "describe_tool",
    "description_max_chars",
    "make_tool_name",
    "map_server_names",
    "request_timeout_s",
    "tool_timeout_s",
]

MCP_PREFIX = "mcp__"

#: The transports a server may use (Claude Code's ``type``; ``streamable-http``
#: is read as ``http``): a local process, Streamable HTTP, the legacy
#: HTTP+SSE pair, a WebSocket.
TRANSPORTS = ("stdio", "http", "sse", "ws")

#: initialize / list / prompts / resources budget unless ``NOETA_MCP_TIMEOUT``
#: (milliseconds) says otherwise (Claude Code's default; its ``MCP_TIMEOUT``
#: under noeta's prefix).
DEFAULT_TIMEOUT_S = 30.0

#: ``tools/call``'s budget when neither the server (``call_timeout``, or
#: ``timeout`` in milliseconds in a settings file) nor
#: ``NOETA_MCP_TOOL_TIMEOUT`` sets one: Claude Code's 1e8 ms, in effect no limit.
DEFAULT_TOOL_TIMEOUT_S = 100_000.0

#: The longest timeout Claude Code accepts (a 32-bit millisecond count).
MAX_TIMEOUT_S = 2_147_483.647


def _env_ms(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def request_timeout_s() -> float:
    """The budget of every MCP request but ``tools/call``."""
    ms = _env_ms("NOETA_MCP_TIMEOUT")
    return min(ms / 1000, MAX_TIMEOUT_S) if ms is not None else DEFAULT_TIMEOUT_S


def tool_timeout_s(server_timeout: float | None) -> float:
    """One ``tools/call``'s budget: the server's own, else
    ``NOETA_MCP_TOOL_TIMEOUT`` (milliseconds), else the default; between 1 s and
    :data:`MAX_TIMEOUT_S`."""
    if server_timeout is not None:
        seconds = server_timeout
    else:
        ms = _env_ms("NOETA_MCP_TOOL_TIMEOUT")
        seconds = ms / 1000 if ms is not None else DEFAULT_TOOL_TIMEOUT_S
    return min(max(seconds, 1.0), MAX_TIMEOUT_S)

#: Claude Code's caps on server-written text the model reads on every request
#: (a tool's description, a server's instructions), in characters: a deferred
#: tool, sent only once ``ToolSearch`` has loaded it, gets the larger one.
#: ``NOETA_MAX_MCP_DESCRIPTION_LENGTH`` (its
#: ``CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH``) replaces both.
DESCRIPTION_MAX_CHARS = 4096
LOADED_DESCRIPTION_MAX_CHARS = 16_384
_TRUNCATED = "… [truncated]"

_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]+$")
#: The longest tool name sent (the Messages API's limit, Claude Code's cut-off).
NAME_MAX = 128
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_NAME_HASH_LEN = 8


class McpConfigError(ValueError):
    """The operator's wiring is wrong (a bad alias, an empty command, a tool
    name that is empty, two servers producing one name)."""


class McpError(Exception):
    """A transport, protocol or timeout fault talking to a server."""


class McpServerError(McpError):
    """The server answered a request with a JSON-RPC ``error``: it is up,
    and refused or could not do what was asked."""

    def __init__(self, message: str, code: object = None, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class McpAuthRequired(McpError):
    """A remote server answered 401 and no stored token could be renewed:
    the person must sign in (OAuth). ``challenge`` is the response's
    ``WWW-Authenticate`` header."""

    def __init__(self, message: str, challenge: str = "") -> None:
        super().__init__(message)
        self.challenge = challenge


@dataclass(frozen=True)
class OAuthSettings:
    """A remote server's ``oauth`` block (Claude Code's fields):
    ``clientId`` (skip dynamic registration), ``callbackPort`` (a fixed
    redirect port), ``authServerMetadataUrl`` (skip discovery; https only),
    ``scopes`` (space-separated)."""

    client_id: str = ""
    callback_port: int | None = None
    auth_server_metadata_url: str = ""
    scopes: str = ""


@dataclass(frozen=True)
class McpServer:
    """One MCP server a task may use.

    Exactly one of ``command`` (a local stdio server: argv, never a shell
    line) and ``url`` (a remote endpoint) is set. ``type`` is the transport
    (:data:`TRANSPORTS`; default ``stdio`` for a command, ``http`` for a
    url). ``env`` is laid over a stdio server's inherited environment;
    ``headers`` are sent with every request to a remote one, under the
    headers ``headers_helper`` (a shell command printing a JSON object)
    gives when a connection opens. All of them are credentials: they live
    in this host-side value only and are never recorded. ``oauth`` tunes
    the sign-in an ``http`` / ``sse`` server may ask for.

    ``tools`` keeps only the named raw tools (``None`` = all advertised).
    ``call_timeout`` bounds one ``tools/call`` (``None``: :func:`tool_timeout_s`).
    ``deferred`` (the default) announces the server's tools by name only:
    the model loads them with ``ToolSearch`` (the ``tool_search`` plugin),
    except a tool whose ``_meta["anthropic/alwaysLoad"]`` is true; ``False``
    sends their schemas from the first request. ``defer_all`` (the settings'
    ``alwaysLoad: false``) defers every tool, whatever its ``_meta`` says.
    ``bare_elicitation`` declares the elicitation capability without its
    ``form`` / ``url`` modes, for servers that reject them.
    """

    alias: str
    command: Sequence[str] = ()
    url: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    tools: Sequence[str] | None = None
    call_timeout: float | None = None
    deferred: bool = True
    defer_all: bool = False
    type: str = ""
    headers_helper: str = ""
    oauth: OAuthSettings | None = None
    bare_elicitation: bool = False

    def __post_init__(self) -> None:
        if not _ALIAS_RE.match(self.alias):
            raise McpConfigError(
                f"invalid MCP server alias {self.alias!r} (must match ^[A-Za-z0-9_-]+$)"
            )
        if bool(self.command) == bool(self.url):
            raise McpConfigError(
                f"MCP server {self.alias!r} needs exactly one of command and url"
            )
        kind = self.type or ("http" if self.url else "stdio")
        if kind not in TRANSPORTS:
            raise McpConfigError(f"MCP server {self.alias!r}: unknown transport {kind!r}")
        object.__setattr__(self, "type", kind)
        if (kind == "stdio") != bool(self.command):
            raise McpConfigError(f"MCP server {self.alias!r}: a {kind} server needs "
                                 + ("a command" if kind == "stdio" else "a url"))
        if self.call_timeout is not None and not self.call_timeout > 0:
            raise McpConfigError(
                f"MCP server {self.alias!r} call_timeout must be > 0, got {self.call_timeout!r}"
            )

    @property
    def transport(self) -> str:
        return self.type

    def key(self) -> tuple[object, ...]:
        """The connection's identity, credentials included (memory only).
        ``tools`` and ``deferred`` filter what is offered; they do not change
        the connection."""
        if self.url:
            return (self.transport, self.alias, self.url, tuple(sorted(self.headers.items())),
                    self.call_timeout, self.headers_helper, self.oauth, self.bare_elicitation)
        return ("stdio", self.alias, tuple(self.command), tuple(sorted(self.env.items())),
                self.call_timeout, self.bare_elicitation)


def description_max_chars(*, loaded: bool = False) -> int:
    """``NOETA_MAX_MCP_DESCRIPTION_LENGTH`` when it is a positive number, else
    :data:`LOADED_DESCRIPTION_MAX_CHARS` for a ``loaded`` description and
    :data:`DESCRIPTION_MAX_CHARS` otherwise."""
    try:
        value = int(os.environ.get("NOETA_MAX_MCP_DESCRIPTION_LENGTH", ""))
    except ValueError:
        value = 0
    if value > 0:
        return value
    return LOADED_DESCRIPTION_MAX_CHARS if loaded else DESCRIPTION_MAX_CHARS


def cap_text(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, marked as cut."""
    return text if len(text) <= limit else text[:limit] + _TRUNCATED


def describe_tool(description: object) -> tuple[str, str]:
    """The model-facing descriptions of one server tool, the server's own
    text, unprefixed: ``(listed, loaded)``, the form sent while it is
    offered outright and the one sent once ``ToolSearch`` loaded it."""
    text = description if isinstance(description, str) else ""
    return (cap_text(text, description_max_chars()),
            cap_text(text, description_max_chars(loaded=True)))


def make_tool_name(alias: str, raw: object, *, disambiguate: bool = False) -> str:
    """``mcp__{alias}__{safe}``, with a hash of the raw name when
    ``disambiguate`` (see the module docstring)."""
    if not isinstance(raw, str) or raw == "":
        raise McpConfigError(f"MCP server {alias!r} advertised a tool with a missing/empty name")
    name = f"{MCP_PREFIX}{alias}__{_UNSAFE_RE.sub('_', raw)}"
    if disambiguate:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        name = f"{name}_{digest[:_NAME_HASH_LEN]}"
    return name


def map_server_names(alias: str, raw_names: Sequence[object]) -> dict[str, str]:
    """``raw name -> offered name`` for one server, collision-free and
    independent of advertised order."""
    plain: dict[str, list[str]] = {}
    for raw in raw_names:
        plain.setdefault(make_tool_name(alias, raw), []).append(str(raw))
    out: dict[str, str] = {}
    for name, raws in plain.items():
        if len(set(raws)) == 1:
            out[raws[0]] = name
            continue
        for raw in raws:
            keep = _UNSAFE_RE.search(raw) is None and name == f"{MCP_PREFIX}{alias}__{raw}"
            out[raw] = name if keep else make_tool_name(alias, raw, disambiguate=True)
    if len(set(out.values())) != len(out):
        raise McpConfigError(f"MCP server {alias!r}: tool names could not be told apart")
    return out
