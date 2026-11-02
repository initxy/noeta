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
from noeta.plugins.mcp.config import McpError
from noeta.plugins.mcp.prompts import INJECTION_MAX_BYTES, flatten_prompt, flatten_resource

FAKE = str(Path(__file__).with_name("fake_server.py"))
SERVER = McpServer(alias="fake", command=[sys.executable, FAKE])


async def test_prompts_become_commands_and_expand_to_text() -> None:
    pool = McpPool()
    try:
        [prompt] = await list_prompts([SERVER], pool=pool)
        assert len(pool) == 1  # the pool given, though it was empty, not the default one
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


def test_prompt_and_resource_text_is_capped_with_a_marker() -> None:
    """The text sits in a durable task's history as a user message, so it is
    cut at 64 KiB (a deviation: Claude Code sends it uncut)."""
    fits = "a" * INJECTION_MAX_BYTES
    assert flatten_prompt({"messages": [{"role": "user", "content": fits}]}) == fits
    big = "é" * 200_000
    prompt = flatten_prompt({"messages": [{"role": "user", "content": big}]})
    assert prompt.endswith(f"\n\n[truncated: MCP prompt exceeded {INJECTION_MAX_BYTES} bytes]")
    assert prompt.startswith("é" * (INJECTION_MAX_BYTES // 2))
    assert len(prompt.encode()) < INJECTION_MAX_BYTES + 100
    resource = flatten_resource({"contents": [{"text": big}]})
    assert resource.endswith(f"[truncated: MCP resource exceeded {INJECTION_MAX_BYTES} bytes]")
    assert flatten_prompt({"messages": [{"role": "user", "content": "plain"},
                                        {"role": "user", "content": [
                                            {"type": "image", "data": ""},
                                            {"type": "text", "text": "two"}]}]}) == (
        "plain\n\n[image (unknown type) in the prompt: not shown, a command's message is "
        "text]\n\ntwo")
    assert flatten_resource({"contents": [{"text": "x"}, {"blob": "AA=="}]}) == "x"


def test_a_prompts_other_blocks_become_text_as_claude_code_carries_them() -> None:
    # Claude Code 2.1.312 converts a prompt's blocks as a tool result's: an
    # embedded resource's text under a line naming it, a link as a line.
    text = flatten_prompt({"messages": [{"role": "user", "content": [
        {"type": "resource", "resource": {"uri": "file:///a.md", "text": "notes"}},
        {"type": "resource_link", "uri": "file:///b", "name": "b"},
        {"type": "audio", "mimeType": "audio/wav", "data": "AAAA"},
        {"type": "mystery"}]}]}, "fake")
    assert text.split("\n\n") == [
        "[resource file:///a.md from fake]\nnotes",
        "[resource link: b <file:///b> (unknown type)]",
        "[audio (audio/wav, 3 bytes): not shown]"]
