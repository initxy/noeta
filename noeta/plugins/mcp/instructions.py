"""MCP server instructions: the ``instructions`` a server's ``initialize``
result gives for using its tools, told to the model as Claude Code tells
them (a heading, then one ``## <server>`` block each), and withdrawn when the
server goes away."""

from __future__ import annotations

from collections.abc import Mapping

from noeta.toolkit.deltas import AMBIENT_NOTE, changed

__all__ = ["INSTRUCTIONS_MAX_CHARS", "cap_instructions", "instructions_delta"]

#: Server instructions beyond this are cut, as Claude Code cuts them.
INSTRUCTIONS_MAX_CHARS = 2048
INSTRUCTIONS_HEAD = (
    "# MCP Server Instructions\n"
    "These MCP servers gave instructions for using their tools and resources:")
INSTRUCTIONS_GONE = ("These MCP servers disconnected; their instructions above no longer "
                     "apply:")


def cap_instructions(text: str) -> str:
    if len(text) <= INSTRUCTIONS_MAX_CHARS:
        return text
    return text[:INSTRUCTIONS_MAX_CHARS] + "… [truncated]"


def instructions_delta(told: Mapping[str, str], now: Mapping[str, str]) -> str:
    """The note on servers whose instructions are new or changed since
    ``told`` and servers that left (``""`` when nothing changed)."""
    joined, left = changed(told, now)
    parts: list[str] = []
    if joined:
        parts.append("\n".join([INSTRUCTIONS_HEAD,
                                *(f"## {alias}\n{now[alias]}" for alias in joined)]))
    if left:
        parts.append("\n".join([INSTRUCTIONS_GONE, *left]))
        parts.append(AMBIENT_NOTE)
    return "\n".join(parts)
