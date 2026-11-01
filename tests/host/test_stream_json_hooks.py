"""The client's own hooks over the headless protocol: ``initialize``
carries ``hooks`` with callback ids, and each time one fires the client gets
a ``hook_callback`` request and its reply is the hook's answer."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

from noeta.cli.headless import FlagError, hooks_arguments
from noeta.core.agent import Agent
from noeta.host.stream_json import HeadlessOptions, StreamJson, drive
from noeta.plugins.hooks import Hooks
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.hook_callbacks import HookCallbacks
from tests.host.test_stream_json import MODEL, Client, Echo, local, results


async def test_client_hooks_are_asked_through_hook_callback(tmp_path: Path) -> None:
    echo = Echo()
    callbacks = HookCallbacks()
    provider = ScriptedProvider([reply("", ("Echo", {"x": 1})), reply("blocked, fine")])
    app = local(tmp_path, provider, Agent(name="main", model=MODEL, tools=[echo],
                                          extensions=[Hooks(callbacks=callbacks)]))
    c = Client()

    def answer(request: dict[str, Any]) -> dict[str, Any]:
        assert request["subtype"] == "hook_callback" and request["callback_id"] == "hook_0"
        assert request["input"]["hook_event_name"] == "PreToolUse"
        assert request["tool_use_id"] == request["input"]["tool_use_id"]
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": "the SDK hook says no"}}

    c.answer = answer
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path),
                                               hook_callbacks=callbacks), c.emit)
        running = asyncio.create_task(drive(conv, lines=c.lines()))
        c.control("init", subtype="initialize", hooks={
            "PreToolUse": [{"matcher": "Echo", "hookCallbackIds": ["hook_0"]}]})
        await c.until(lambda m: m.get("response", {}).get("request_id") == "init")
        c.say("echo please")
        await c.until(lambda m: m["type"] == "result")
        assert echo.runs == [] and results(c)[0]["result"] == "blocked, fine"
        refused = [m for m in c.out if m["type"] == "user"][-1]["message"]["content"][0]
        assert refused["is_error"] and refused["content"] == "the SDK hook says no"
        c.control("bad", subtype="initialize", hooks={"PreToolUse": {}})
        await c.until(lambda m: m.get("response", {}).get("request_id") == "bad")
        [bad] = [m["response"] for m in c.out if m.get("response", {}).get("request_id")
                 == "bad"]
        assert bad["subtype"] == "error" and "hookCallbackIds" in bad["error"]
        await c.inbox.put(None)
        await asyncio.wait_for(running, 10)
    # The client is gone: its hooks are too.
    assert callbacks.hooks == {}


async def test_a_failed_callback_reply_is_a_failed_hook(tmp_path: Path) -> None:
    echo = Echo()
    callbacks = HookCallbacks()
    provider = ScriptedProvider([reply("", ("Echo", {"x": 1})), reply("ran anyway")])
    app = local(tmp_path, provider, Agent(name="main", model=MODEL, tools=[echo],
                                          extensions=[Hooks(callbacks=callbacks)]))
    c = Client()

    async def emit(message: dict[str, Any]) -> None:
        c.out.append(message)
        c.seen.set()
        if message.get("type") == "control_request":
            await c.inbox.put({"type": "control_response", "response": {
                "subtype": "error", "request_id": message["request_id"], "error": "boom"}})

    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path),
                                               hook_callbacks=callbacks), emit)
        running = asyncio.create_task(drive(conv, lines=c.lines()))
        c.control("init", subtype="initialize", hooks={
            "PreToolUse": [{"hookCallbackIds": ["h"]}]})
        c.say("echo please")
        await c.until(lambda m: m["type"] == "result")
        assert echo.runs == [{"x": 1}] and results(c)[0]["result"] == "ran anyway"
        await c.inbox.put(None)
        await asyncio.wait_for(running, 10)


def test_hooks_arguments_from_the_flags() -> None:
    def args(tool: str | None, prompts: str = "host") -> argparse.Namespace:
        return argparse.Namespace(permission_prompt_tool=tool, permission_prompts=prompts)

    assert set(hooks_arguments(args(None))) == {"callbacks"}
    assert set(hooks_arguments(args("stdio"))) == {"callbacks"}
    assert hooks_arguments(args("mcp__s__t"))["permission_prompt_tool"] == "mcp__s__t"
    assert "permission_prompt_tool" not in hooks_arguments(args("mcp__s__t", "none"))
    with pytest.raises(FlagError, match="MCP tool name"):
        hooks_arguments(args("Read"))
