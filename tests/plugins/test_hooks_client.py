"""Hooks a connected client answers (Claude Code's SDK hook callbacks) and
the MCP tool that answers permission requests (``--permission-prompt-tool``)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from noeta.core.agent import Agent
from noeta.core.extension import Extension
from noeta.plugins.hooks import HookCommand, Hooks
from noeta.plugins.hooks.runners import run_callback
from noeta.testing import reply
from noeta.toolkit.hook_callbacks import HookCallbacks, parse_callbacks
from tests.plugins.test_hooks_events import READ_A, AskAll, McpLike, Setup


def with_hooks(s: Setup, hooks: Hooks, *extensions: Extension) -> Setup:
    s.hooks = hooks
    s.agent = Agent(name="a", model="m", extensions=[*extensions, hooks, *s.agent.extensions[-1:]])
    return s


def test_parse_callbacks() -> None:
    hooks = parse_callbacks({"PreToolUse": [{"matcher": "Read", "hookCallbackIds": ["h1", "h2"],
                                             "timeout": 5}],
                             "Stop": [{"hookCallbackIds": []}]})
    assert hooks == {"PreToolUse": [{"matcher": "Read", "hooks": [
        {"type": "callback", "callback_id": "h1", "timeout": 5},
        {"type": "callback", "callback_id": "h2", "timeout": 5}]}]}
    for bad in ([], {"Stop": {}}, {"Stop": [{"matcher": "x"}]},
                {"Stop": [{"hookCallbackIds": [1]}]}):
        with pytest.raises(ValueError, match="hookCallbackIds"):
            parse_callbacks(bad)


async def test_a_client_hook_answers_and_is_replaced(tmp_path: Path) -> None:
    asked: list[tuple[str, dict[str, Any], str | None]] = []

    async def send(callback_id: str, payload: dict[str, Any],
                   tool_use_id: str | None) -> dict[str, Any]:
        asked.append((callback_id, payload, tool_use_id))
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": "the client says no"}}

    callbacks = HookCallbacks()
    callbacks.register(parse_callbacks({"PreToolUse": [{"matcher": "Read",
                                                        "hookCallbackIds": ["h1"]}]}), send)
    hooks = Hooks(callbacks=callbacks)  # made after the client registered: it still sees them
    s = with_hooks(Setup(tmp_path, {}, [reply("", READ_A), reply("ok")]), hooks)
    await s.start()
    [(callback_id, payload, tool_use_id)] = asked
    assert callback_id == "h1" and payload["tool_name"] == "Read"
    assert tool_use_id == payload["tool_use_id"]
    assert await s.results() == [("the client says no", True)]
    # A new registration replaces the old one; none leaves no hook.
    callbacks.register({}, send)
    assert "PreToolUse" not in hooks.matchers


async def test_settings_files_cannot_declare_callback_hooks(tmp_path: Path) -> None:
    hooks = Hooks(hooks={"Stop": [{"hooks": [{"type": "callback", "callback_id": "x"}]}]})
    assert hooks.matchers == {}


async def test_a_callback_without_a_client_or_in_time_fails() -> None:
    hook = HookCommand(type="callback", callback_id="h", timeout=0.01)
    assert (await run_callback(hook, {}, None)).failed

    async def slow(*_a: Any) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {}

    async def broken(*_a: Any) -> dict[str, Any]:
        raise RuntimeError("the client is gone")

    assert "timed out" in (await run_callback(hook, {}, slow)).stderr
    assert "the client is gone" in (await run_callback(hook, {}, broken)).stderr
    with pytest.raises(RuntimeError, match="no client"):
        await HookCallbacks().call("h", {}, None)


async def test_the_permission_prompt_tool_answers_asks(tmp_path: Path) -> None:
    allow = McpLike(json.dumps({"behavior": "allow", "updatedInput": {"file_path": "b.txt"}}))
    s = with_hooks(Setup(tmp_path, {}, [reply("", READ_A), reply("ok")]),
                   Hooks(permission_prompt_tool="mcp__chk__verify"), AskAll(), allow)
    await s.start()
    [call] = allow.calls
    assert call["tool_name"] == "Read" and call["input"] == {"file_path": "a.txt"}
    assert call["tool_use_id"]
    [(text, is_error)] = await s.results()
    assert not is_error and "other" in text
    deny = McpLike(json.dumps({"behavior": "deny", "message": "Not that."}))
    s = with_hooks(Setup(tmp_path / "2", {}, [reply("", READ_A), reply("ok")]),
                   Hooks(permission_prompt_tool="mcp__chk__verify"), AskAll(), deny)
    await s.start()
    assert await s.results() == [("Not that.", True)]


async def test_an_unusable_prompt_tool_answer_denies(tmp_path: Path) -> None:
    for answer in ("yes", json.dumps({"behavior": "deny"})):
        odd = McpLike(answer)
        s = with_hooks(Setup(tmp_path / str(len(answer)), {}, [reply("", READ_A), reply("ok")]),
                       Hooks(permission_prompt_tool="mcp__chk__verify"), AskAll(), odd)
        await s.start()
        [(text, is_error)] = await s.results()
        assert is_error and "permission prompt tool chk/verify answered" in text
    with pytest.raises(ValueError, match="not an MCP tool name"):
        Hooks(permission_prompt_tool="Read")
