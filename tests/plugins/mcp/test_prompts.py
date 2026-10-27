"""MCP prompts (slash commands) and resources (``@`` mentions)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from noeta.plugins.mcp import (
    McpPool,
    McpServer,
    expand_prompt,
    list_prompts,
    list_resources,
    read_resource,
)
from noeta.plugins.mcp.config import INJECTION_MAX_BYTES, McpError, cap_injected
from noeta.plugins.mcp.prompts import flatten_prompt, flatten_resource

FAKE = str(Path(__file__).with_name("fake_server.py"))
SERVER = McpServer(alias="fake", command=[sys.executable, FAKE])


async def test_prompts_become_commands_and_expand_to_text() -> None:
    pool = McpPool()
    try:
        [prompt] = await list_prompts([SERVER], pool=pool)
        assert prompt.command_name == "mcp__fake__review"
        assert prompt.arguments == [{"name": "path", "description": "", "required": True}]
        command = prompt.command()
        assert command.name == "mcp__fake__review" and command.description == "Review code"
        text = await expand_prompt(SERVER, "review", {"path": "a.py"}, pool=pool)
        assert text == "Review a.py"
    finally:
        await pool.close()


async def test_resources_list_with_mentions_and_read_as_text() -> None:
    pool = McpPool()
    try:
        [res] = await list_resources([SERVER], pool=pool)
        assert res.mention == "fake:file:///notes.md" and res.name == "notes"
        assert await read_resource(SERVER, res.uri, pool=pool) == "the notes"
    finally:
        await pool.close()


async def test_an_unreachable_server_raises() -> None:
    pool = McpPool()
    with pytest.raises(McpError):
        await list_prompts([McpServer(alias="x", command=["/no/such"])], pool=pool)


def test_injected_text_is_capped_with_a_marker() -> None:
    big = "a" * (INJECTION_MAX_BYTES + 10)
    out = cap_injected(big, kind="prompt")
    assert out.endswith(f"[truncated: MCP prompt exceeded {INJECTION_MAX_BYTES} bytes]")
    assert flatten_prompt({"messages": [{"role": "user", "content": "plain"},
                                        {"role": "user", "content": [
                                            {"type": "image", "data": ""},
                                            {"type": "text", "text": "two"}]}]}) == "plain\n\ntwo"
    assert flatten_resource({"contents": [{"text": "x"}, {"blob": "AA=="}]}) == "x"
