"""MCP prompt / resource injection is bounded (tools m5).

A ``prompts/get`` or ``resources/read`` result is flattened and injected as an
``origin="system"`` message. The server controls that text, so an unbounded
body is both a prompt-injection surface and a context/token bomb (the transport
only caps at ~8 MB). The flatten functions cap the injected text at the
inline-content ceiling (64 KiB) with a visible truncation marker.
"""

from __future__ import annotations

from noeta.tools._limits import INLINE_CONTENT_MAX_BYTES
from noeta.tools.mcp.prompts import flatten_prompt_messages
from noeta.tools.mcp.resources import flatten_resource_contents


def test_flatten_prompt_messages_caps_oversize_injection() -> None:
    huge = "x" * (INLINE_CONTENT_MAX_BYTES + 10_000)
    result = {"messages": [{"role": "user", "content": {"type": "text", "text": huge}}]}
    out = flatten_prompt_messages(result)
    assert len(out.encode("utf-8")) <= INLINE_CONTENT_MAX_BYTES + 100
    assert "[truncated: MCP prompt exceeded" in out


def test_flatten_prompt_messages_keeps_small_body_verbatim() -> None:
    result = {"messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}]}
    assert flatten_prompt_messages(result) == "hi"


def test_flatten_resource_contents_caps_oversize_injection() -> None:
    huge = "y" * (INLINE_CONTENT_MAX_BYTES + 10_000)
    result = {"contents": [{"uri": "file://x", "text": huge}]}
    out = flatten_resource_contents(result)
    assert len(out.encode("utf-8")) <= INLINE_CONTENT_MAX_BYTES + 100
    assert "[truncated: MCP resource exceeded" in out


def test_flatten_resource_contents_keeps_small_body_verbatim() -> None:
    result = {"contents": [{"uri": "file://x", "text": "snapshot"}]}
    assert flatten_resource_contents(result) == "snapshot"
