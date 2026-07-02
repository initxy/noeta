"""MCP prompts → slash-command menu + expanded-content injection.

A remote / local MCP server can advertise **prompts** (``prompts/list``) — named,
optionally-parameterised message templates the user invokes. Maps
each one to a slash command ``/mcp__<alias>__<promptname>`` in the SAME pick-from-menu
flow skills use; the front-end renders
a form from the prompt's declared ``arguments`` schema; on submit the host calls
``prompts/get`` (with the filled args), flattens the returned messages into plain
text, and injects it as that turn's opening user content — recorded as an
**ordinary message** tagged ``origin="system"``. Because it lands
as a normal recorded message, resume simply reads it back and NEVER re-calls
``prompts/get`` — the same naturally resume-safe path resources/memory use.

The two entrypoints connect a single server spec (HTTP or stdio), do one
request-response round-trip, and shut the client down — they never hold a stream
open (D4: request-response subset only). Credentials live in the spec and ride
the wire only; nothing here records them.

* :func:`discover_prompts` — ``prompts/list`` → ``[{name, noeta_name,
  description, arguments}]`` for the menu (``noeta_name`` is the
  ``mcp__alias__prompt`` slash name; ``arguments`` is the declared param list so
  the front-end can render a form). A server that does not support prompts
  returns ``[]`` (the fault is swallowed — prompts are optional).
* :func:`expand_prompt` — ``prompts/get`` → the flattened text the host injects.
  Faults propagate (``McpError`` / ``McpConfigError``) so the HTTP layer maps a
  bad alias / unreachable server to a typed error.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.tools.mcp._client import McpError, SpawnFn
from noeta.tools.mcp._http_client import HttpPostFn
from noeta.tools.mcp.tool import (
    McpAnyServerSpec,
    _connect_client,
    cap_injected,
    make_mcp_tool_name,
)


__all__ = [
    "MCP_PROMPT_ORIGIN_PREFIX",
    "discover_prompts",
    "expand_prompt",
    "flatten_prompt_messages",
    "make_mcp_prompt_name",
]


#: A human-readable provenance prefix the host prepends to an injected prompt's
#: text so the conversation transcript shows "this came from an MCP prompt"
#: (the structural origin is ``Message.origin="system"``; this is the visible
#: label, mirroring how memory recall reads as an attributed turn).
MCP_PROMPT_ORIGIN_PREFIX = "mcp-prompt"


def make_mcp_prompt_name(alias: str, raw_prompt_name: object) -> str:
    """Map a raw prompt name to the slash-command name ``mcp__alias__prompt``.

    Reuses the tool-name mapper (same provider-safe sanitisation, same fail-fast
    on empty / over-long / collision-prone names) so a prompt slash command and
    an MCP tool share one naming rule (D9)."""
    return make_mcp_tool_name(alias, raw_prompt_name)


def flatten_prompt_messages(result: dict[str, Any]) -> str:
    """Flatten a ``prompts/get`` result's messages into one injectable string.

    An MCP ``prompts/get`` returns ``{description?, messages: [{role, content}]}``
    where each ``content`` is a single content block or a list of them. We keep
    the **text** of every text block, in order, joined by blank lines (non-text
    blocks — images / resources — are out of scope for v1 prompt injection and
    are skipped). A ``description`` (when present) is NOT injected; it labels the
    prompt in the menu, not the conversation."""
    messages = result.get("messages")
    parts: list[str] = []
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            for block in _iter_blocks(content):
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    parts.append(block["text"])
    return cap_injected(
        "\n\n".join(p for p in parts if p), kind="prompt"
    )


def _iter_blocks(content: Any) -> list[Any]:
    """Normalise a message's ``content`` to a list of blocks.

    MCP allows a single content object or a list; a bare string is wrapped as a
    text block (some servers emit ``content: "..."``)."""
    if isinstance(content, list):
        return content
    if isinstance(content, dict):
        return [content]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def discover_prompts(
    spec: McpAnyServerSpec,
    *,
    spawn: Optional[SpawnFn] = None,
    http_post: Optional[HttpPostFn] = None,
) -> list[dict[str, Any]]:
    """Connect ``spec``, list its prompts, and shut down.

    Returns ``[{name, noeta_name, description, arguments}]`` in advertised order
    (``name`` = raw prompt name, ``noeta_name`` = ``mcp__alias__prompt`` slash
    name, ``arguments`` = the declared parameter list so the UI renders a form).
    A server that does not implement prompts (``prompts/list`` faults) yields
    ``[]`` — prompts are an optional capability, so a missing one is not an
    error. Connect / handshake faults DO propagate (the alias is misconfigured).
    """
    client = _connect_client(spec, spawn=spawn, http_post=http_post)
    try:
        client.start()
        try:
            raw_prompts = client.list_prompts()
        except McpError:
            # Optional capability — a server with no prompts surface is fine.
            return []
        out: list[dict[str, Any]] = []
        for p in raw_prompts:
            name = p.get("name")
            if not isinstance(name, str) or not name:
                continue
            noeta_name = make_mcp_prompt_name(spec.alias, name)
            args = p.get("arguments")
            out.append(
                {
                    "name": name,
                    "noeta_name": noeta_name,
                    "description": (
                        p["description"]
                        if isinstance(p.get("description"), str)
                        else ""
                    ),
                    "arguments": _normalise_arguments(args),
                }
            )
        return out
    finally:
        client.shutdown()


def _normalise_arguments(args: Any) -> list[dict[str, Any]]:
    """Normalise an MCP prompt's ``arguments`` to ``[{name, description, required}]``.

    The MCP prompt spec declares ``arguments: [{name, description?, required?}]``.
    We keep that shape (names only — never a value) so the front-end renders one
    input per argument and marks the required ones."""
    out: list[dict[str, Any]] = []
    if not isinstance(args, list):
        return out
    for a in args:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        if not isinstance(name, str) or not name:
            continue
        out.append(
            {
                "name": name,
                "description": (
                    a["description"]
                    if isinstance(a.get("description"), str)
                    else ""
                ),
                "required": bool(a.get("required", False)),
            }
        )
    return out


def expand_prompt(
    spec: McpAnyServerSpec,
    *,
    prompt_name: str,
    arguments: dict[str, Any],
    spawn: Optional[SpawnFn] = None,
    http_post: Optional[HttpPostFn] = None,
) -> str:
    """Connect ``spec``, ``prompts/get`` the prompt with ``arguments``, flatten.

    Returns the plain text the host injects as the turn's opening user content
    (recorded as an ordinary ``origin="system"`` message; resume reads it back,
    never re-calling this). Faults propagate so the HTTP layer maps an
    unreachable server / unknown prompt to a typed error.
    """
    client = _connect_client(spec, spawn=spawn, http_post=http_post)
    try:
        client.start()
        result = client.get_prompt(prompt_name, dict(arguments))
        return flatten_prompt_messages(result)
    finally:
        client.shutdown()
