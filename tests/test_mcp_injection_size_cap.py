"""MCP prompt / resource injection is bounded, on its OWN budget.

A ``prompts/get`` or ``resources/read`` result is flattened and injected as an
``origin="system"`` message, and the server controls that text — so an unbounded
body is both a prompt-injection surface and a context/token bomb (the transport
alone only caps at ~8 MB). Injection used to borrow ``INLINE_CONTENT_MAX_BYTES``,
which was 64 KiB when that was written and has since grown to 1 MiB for content
the model actually asked for. Injected text is content nobody asked for that
then sits in the history for the rest of the task, so it has its own 64 KiB
ceiling (``MCP_INJECTION_MAX_BYTES``) with a visible truncation marker.
"""

from __future__ import annotations

from noeta.tools.limits import INLINE_CONTENT_MAX_BYTES, MCP_INJECTION_MAX_BYTES
from noeta.builtins.mcp.impl.prompts import flatten_prompt_messages
from noeta.builtins.mcp.impl.resources import flatten_resource_contents


def test_injection_cap_is_independent_of_the_content_cap() -> None:
    assert MCP_INJECTION_MAX_BYTES == 64 * 1024
    assert MCP_INJECTION_MAX_BYTES < INLINE_CONTENT_MAX_BYTES


def test_flatten_prompt_messages_caps_oversize_injection() -> None:
    huge = "x" * (MCP_INJECTION_MAX_BYTES + 10_000)
    result = {"messages": [{"role": "user", "content": {"type": "text", "text": huge}}]}
    out = flatten_prompt_messages(result)
    assert len(out.encode("utf-8")) <= MCP_INJECTION_MAX_BYTES + 100
    assert f"[truncated: MCP prompt exceeded {MCP_INJECTION_MAX_BYTES} bytes]" in out


def test_flatten_prompt_messages_keeps_small_body_verbatim() -> None:
    result = {"messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}]}
    assert flatten_prompt_messages(result) == "hi"


def test_flatten_resource_contents_caps_oversize_injection() -> None:
    huge = "y" * (MCP_INJECTION_MAX_BYTES + 10_000)
    result = {"contents": [{"uri": "file://x", "text": huge}]}
    out = flatten_resource_contents(result)
    assert len(out.encode("utf-8")) <= MCP_INJECTION_MAX_BYTES + 100
    assert f"[truncated: MCP resource exceeded {MCP_INJECTION_MAX_BYTES} bytes]" in out


def test_flatten_resource_contents_keeps_small_body_verbatim() -> None:
    result = {"contents": [{"uri": "file://x", "text": "snapshot"}]}
    assert flatten_resource_contents(result) == "snapshot"
