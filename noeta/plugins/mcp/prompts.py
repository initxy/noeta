"""MCP prompts and resources, for front ends.

Both are user-driven: nothing here is a tool, and the model never pulls a
prompt or a resource itself.

* **Prompts become slash commands.** :func:`list_prompts` returns each
  server prompt as an :class:`McpPrompt` (named ``mcp__{alias}__{prompt}``,
  the tool naming rule) with its declared arguments, and
  :meth:`McpPrompt.command` as a ``Command`` a front end can list. When the
  user picks one, :func:`expand_prompt` runs ``prompts/get`` and returns the
  text, which the front end sends as the turn's message
  (``client.send(task, user(text, origin="system"))``). Recorded as an
  ordinary message, it is read back on resume and never fetched again.
* **Resources become ``@`` mentions.** :func:`list_resources` returns the
  static resources with their ``alias:uri`` mention token;
  :func:`read_resource` returns the text snapshot to send the same way.

Injected text is capped at 64 KiB with a marker. A server without prompts or
resources lists none; a connection fault raises ``McpError``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from noeta.core.extension import Command
from noeta.plugins.mcp.config import McpError, McpServer, cap_injected, make_tool_name
from noeta.plugins.mcp.pool import McpPool, default_pool

__all__ = [
    "McpPrompt",
    "McpResource",
    "expand_prompt",
    "flatten_prompt",
    "flatten_resource",
    "list_prompts",
    "list_resources",
    "read_resource",
]


@dataclass(frozen=True)
class McpPrompt:
    alias: str
    name: str
    command_name: str
    description: str = ""
    #: ``[{name, description, required}]``: one input per argument.
    arguments: list[dict[str, Any]] = field(default_factory=list)

    def command(self) -> Command:
        return Command(name=self.command_name, description=self.description)


@dataclass(frozen=True)
class McpResource:
    alias: str
    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""

    @property
    def mention(self) -> str:
        return f"{self.alias}:{self.uri}"


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _arguments(args: object) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in args if isinstance(args, list) else []:
        if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]:
            out.append({"name": a["name"], "description": _str(a.get("description")),
                        "required": bool(a.get("required", False))})
    return out


async def list_prompts(servers: Sequence[McpServer], *, scope: str | None = None,
                       pool: McpPool | None = None) -> list[McpPrompt]:
    pool = pool if pool is not None else default_pool()
    out: list[McpPrompt] = []
    for server in servers:
        async with pool.acquire(server, scope) as client:  # a connect fault raises
            try:
                raw = await client.list_prompts()
            except McpError:
                continue  # an optional capability the server does not have
        for p in raw:
            name = p.get("name")
            if not isinstance(name, str) or not name:
                continue
            out.append(McpPrompt(alias=server.alias, name=name,
                                 command_name=make_tool_name(server.alias, name),
                                 description=_str(p.get("description")),
                                 arguments=_arguments(p.get("arguments"))))
    return out


def _blocks(content: object) -> list[Any]:
    if isinstance(content, list):
        return content
    if isinstance(content, dict):
        return [content]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def flatten_prompt(result: dict[str, Any]) -> str:
    """The text blocks of a ``prompts/get`` result, in order, joined by blank
    lines (other blocks skipped), capped."""
    parts: list[str] = []
    messages = result.get("messages")
    for msg in messages if isinstance(messages, list) else []:
        if not isinstance(msg, dict):
            continue
        for block in _blocks(msg.get("content")):
            if isinstance(block, dict) and block.get("type") == "text" \
                    and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return cap_injected("\n\n".join(p for p in parts if p), kind="prompt")


async def expand_prompt(server: McpServer, name: str, arguments: dict[str, Any], *,
                        scope: str | None = None, pool: McpPool | None = None) -> str:
    pool = pool if pool is not None else default_pool()
    result = await pool.use(server, scope, lambda c: c.get_prompt(name, dict(arguments)),
                            retry=True)
    return flatten_prompt(result)


async def list_resources(servers: Sequence[McpServer], *, scope: str | None = None,
                         pool: McpPool | None = None) -> list[McpResource]:
    pool = pool if pool is not None else default_pool()
    out: list[McpResource] = []
    for server in servers:
        async with pool.acquire(server, scope) as client:  # a connect fault raises
            try:
                raw = await client.list_resources()
            except McpError:
                continue  # an optional capability the server does not have
        for r in raw:
            uri = r.get("uri")
            if isinstance(uri, str) and uri:
                out.append(McpResource(alias=server.alias, uri=uri, name=_str(r.get("name")),
                                       description=_str(r.get("description")),
                                       mime_type=_str(r.get("mimeType"))))
    return out


def flatten_resource(result: dict[str, Any]) -> str:
    """The text contents of a ``resources/read`` result (binary skipped),
    capped."""
    contents = result.get("contents")
    parts = [c["text"] for c in (contents if isinstance(contents, list) else [])
             if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"]]
    return cap_injected("\n\n".join(parts), kind="resource")


async def read_resource(server: McpServer, uri: str, *, scope: str | None = None,
                        pool: McpPool | None = None) -> str:
    pool = pool if pool is not None else default_pool()
    result = await pool.use(server, scope, lambda c: c.read_resource(uri), retry=True)
    return flatten_resource(result)
