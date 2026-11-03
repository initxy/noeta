"""MCP server instructions: the ``instructions`` a server's ``initialize``
result gives for using its tools, told to the model as Claude Code tells
them (a heading, then one ``## <server>`` block each), and withdrawn when the
server goes away."""

from __future__ import annotations

from collections.abc import Collection, Mapping

from noeta.plugins.mcp.config import cap_text, description_max_chars
from noeta.toolkit.deltas import AMBIENT_NOTE, changed

__all__ = ["cap_instructions", "instructions_delta"]

INSTRUCTIONS_HEAD = (
    "# MCP Server Instructions\n"
    "These MCP servers gave instructions for using their tools and resources:")
INSTRUCTIONS_GONE = ("These MCP servers disconnected; their instructions above no longer "
                     "apply:")


def cap_instructions(text: str) -> str:
    """Server instructions cut as a listed tool's description is."""
    return cap_text(text, description_max_chars())


def instructions_delta(told: Mapping[str, str], now: Mapping[str, str],
                       connected: Collection[str] | None = None) -> str:
    """The note on servers whose instructions are new or changed since
    ``told`` (by name, as Claude Code orders them) and servers that left
    (``""`` when nothing changed). With ``connected``, a server still
    connected whose instructions went away is not said to have left."""
    joined, left = changed(told, now)
    joined = sorted(joined)
    if connected is not None:
        left = [alias for alias in left if alias not in connected]
    parts: list[str] = []
    if joined:
        parts.append("\n".join([INSTRUCTIONS_HEAD,
                                *(f"## {alias}\n{now[alias]}" for alias in joined)]))
    if left:
        parts.append("\n".join([INSTRUCTIONS_GONE, *left]))
        parts.append(AMBIENT_NOTE)
    return "\n".join(parts)
