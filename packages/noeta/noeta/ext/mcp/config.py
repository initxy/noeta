"""MCP vocabulary: server specs, errors, tool naming and the text caps.

Naming: a server tool is offered to the model as ``mcp__{alias}__{safe}``,
where ``safe`` is the server's raw tool name with every character outside
``[A-Za-z0-9_-]`` replaced by ``_``. The full name must match
``^[A-Za-z0-9_-]{1,64}$`` (provider-safe). A name that would pass 64
characters keeps the ``mcp__{alias}__`` prefix, is cut, and ends in ``_`` plus
8 hex digits of the raw name's sha256; two raw names of one server that
sanitize alike are told apart the same way (a raw name that is already
provider-safe keeps its plain form), so one awkward name never costs the rest
of the server's tools. ``mcp__`` is a reserved prefix.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from noeta.tools._common import truncate_bytes

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "DESCRIPTION_MAX_BYTES",
    "INJECTION_MAX_BYTES",
    "MCP_PREFIX",
    "McpConfigError",
    "McpError",
    "McpServer",
    "cap_injected",
    "describe_tool",
    "make_tool_name",
    "map_server_names",
]

MCP_PREFIX = "mcp__"

#: initialize / list / prompts / resources budget, and ``tools/call``'s
#: unless the server sets its own ``call_timeout``.
DEFAULT_TIMEOUT_S = 30.0

#: Cap on a server-written tool description. It rides the tool list on every
#: request for the rest of the task, so an essay is paid for forever.
DESCRIPTION_MAX_BYTES = 1024

#: Cap on server-written text that enters the conversation unasked (a
#: prompt, a resource, a ToolSearch listing): it then sits in the history for
#: the rest of the task.
INJECTION_MAX_BYTES = 64 * 1024

_ALIAS_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_NAME_MAX = 64
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_NAME_HASH_LEN = 8


class McpConfigError(ValueError):
    """The operator's wiring is wrong (a bad alias, an empty command, a tool
    name that cannot be made provider-safe, two servers producing one name)."""


class McpError(Exception):
    """A transport, protocol or timeout fault talking to a server."""


@dataclass(frozen=True)
class McpServer:
    """One MCP server a task may use.

    Exactly one of ``command`` (a local stdio server: argv, never a shell
    line) and ``url`` (a remote Streamable HTTP endpoint) is set. ``env`` is
    laid over the scrubbed environment of a stdio server; ``headers`` are
    sent with every HTTP request. Both are credentials: they live in this
    host-side value only and are never recorded.

    ``tools`` keeps only the named raw tools (``None`` = all advertised).
    ``call_timeout`` bounds one ``tools/call`` (``None`` = 30 s).
    ``deferred`` keeps the server's schemas out of every request: its tools
    are reached through ``ToolSearch`` / ``McpCall``.
    """

    alias: str
    command: Sequence[str] = ()
    url: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    tools: Sequence[str] | None = None
    call_timeout: float | None = None
    deferred: bool = False

    def __post_init__(self) -> None:
        if not _ALIAS_RE.match(self.alias):
            raise McpConfigError(
                f"invalid MCP server alias {self.alias!r} (must match ^[a-z0-9_-]{{1,32}}$)"
            )
        if bool(self.command) == bool(self.url):
            raise McpConfigError(
                f"MCP server {self.alias!r} needs exactly one of command and url"
            )
        if self.call_timeout is not None and not self.call_timeout > 0:
            raise McpConfigError(
                f"MCP server {self.alias!r} call_timeout must be > 0, got {self.call_timeout!r}"
            )

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def key(self) -> tuple[object, ...]:
        """The connection's identity, credentials included (memory only).
        ``tools`` and ``deferred`` filter what is offered; they do not change
        the connection."""
        if self.url:
            return ("http", self.alias, self.url, tuple(sorted(self.headers.items())),
                    self.call_timeout)
        return ("stdio", self.alias, tuple(self.command), tuple(sorted(self.env.items())),
                self.call_timeout)


def cap_injected(text: str, *, kind: str) -> str:
    """Server-written text that enters the conversation, bounded at
    :data:`INJECTION_MAX_BYTES` with a marker naming ``kind``."""
    if len(text.encode("utf-8")) <= INJECTION_MAX_BYTES:
        return text
    return (
        truncate_bytes(text, INJECTION_MAX_BYTES)
        + f"\n\n[truncated: MCP {kind} exceeded {INJECTION_MAX_BYTES} bytes]"
    )


def describe_tool(alias: str, description: object) -> str:
    """The model-facing description of one server tool: whose words they
    are first (a server sentence like "always call this first" must not read
    as harness documentation), then the server's text, capped."""
    text = description if isinstance(description, str) else ""
    if len(text.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
        text = (
            truncate_bytes(text, DESCRIPTION_MAX_BYTES)
            + f"\n[truncated: description exceeded {DESCRIPTION_MAX_BYTES} bytes]"
        )
    prefix = f"[MCP server {alias!r}]"
    return f"{prefix} {text}" if text else prefix


def make_tool_name(alias: str, raw: object, *, disambiguate: bool = False) -> str:
    """``mcp__{alias}__{safe}``, shortened or disambiguated with a hash of
    the raw name (see the module docstring)."""
    if not isinstance(raw, str) or raw == "":
        raise McpConfigError(f"MCP server {alias!r} advertised a tool with a missing/empty name")
    safe = _UNSAFE_RE.sub("_", raw)
    prefix = f"{MCP_PREFIX}{alias}__"
    name = f"{prefix}{safe}"
    if disambiguate or len(name) > _NAME_MAX:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        tail = f"_{digest[:_NAME_HASH_LEN]}"
        name = f"{prefix}{safe[: _NAME_MAX - len(prefix) - len(tail)]}{tail}"
    if not _NAME_RE.match(name):
        raise McpConfigError(
            f"MCP tool name {name!r} is not provider-safe (must match ^[A-Za-z0-9_-]{{1,64}}$)"
        )
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
