"""Claude Code's command hooks, end to end with real ``sh`` commands: what
each event receives, adds for the model, and blocks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, ToolFinished, Waiting
from noeta.core.runner import Runtime
from noeta.plugins.fs import FileTools
from noeta.plugins.hooks import Hooks, parse_hooks
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


def _hook(command: str, matcher: str = "") -> list[dict[str, Any]]:
    return [{"matcher": matcher, "hooks": [{"type": "command", "command": command}]}]


async def _run(tmp_path: Path, hooks: dict[str, Any], steps: list[Any],
               prompts: tuple[str, ...] = ("go",)) -> tuple[ScriptedProvider, Client, str]:
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    (work / "a.txt").write_text("hello\n")
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider(steps)
    agent = Agent(name="a", model="m", extensions=[Hooks(hooks=hooks), FileTools()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", prompts[0], config={"cwd": str(work)})
    await rt.run_until_idle()
    for p in prompts[1:]:
        await client.send(tid, p)
        await rt.run_until_idle()
    return provider, client, tid


def _system(provider: ScriptedProvider, i: int) -> list[str]:
    return [m.text for m in provider.requests[i].messages if m.origin == "system"]


async def test_user_prompt_submit_adds_context_and_sees_the_prompt(tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    hooks = {"UserPromptSubmit": _hook(f"cat > {seen}; echo 'The build is green.'")}
    provider, _client, _tid = await _run(tmp_path, hooks, [reply("a"), reply("b")],
                                         prompts=("first", "second"))
    assert _system(provider, 0) == ["The build is green."]
    assert _system(provider, 1) == ["The build is green.", "The build is green."]
    payload = json.loads(seen.read_text())
    assert payload["hook_event_name"] == "UserPromptSubmit"
    assert payload["prompt"] == "second"  # only the new message
    assert payload["cwd"].endswith("/w")


async def test_user_prompt_submit_can_block_the_turn(tmp_path: Path) -> None:
    hooks = {"UserPromptSubmit": _hook("echo 'no secrets in prompts' >&2; exit 2")}
    provider, client, tid = await _run(tmp_path, hooks, [])
    assert provider.requests == []
    failed = [r.event for r in await client.events(tid)
              if isinstance(r.event, ExtensionRecorded) and r.event.kind == "turn_failed"]
    assert failed and failed[0].data["detail"] == "no secrets in prompts"


async def test_pre_tool_use_blocks_matching_calls_only(tmp_path: Path) -> None:
    hooks = {"PreToolUse": _hook("echo 'reads are off today' >&2; exit 2", matcher="Read|Glob")}
    steps = [reply("", ("Read", {"file_path": "a.txt"}), ("Grep", {"pattern": "hello"})),
             reply("done")]
    _provider, client, tid = await _run(tmp_path, hooks, steps)
    results = {r.event.result.call_id: r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)}
    read, grep = results.values()
    assert read.is_error and "reads are off today" in read.content[0].text  # type: ignore[union-attr]
    assert not grep.is_error


async def test_pre_tool_use_json_can_ask(tmp_path: Path) -> None:
    out = json.dumps({"hookSpecificOutput": {"permissionDecision": "ask",
                                             "permissionDecisionReason": "Read a.txt?"}})
    told = tmp_path / "told.json"
    hooks = {"PreToolUse": _hook(f"echo '{out}'"), "Notification": _hook(f"cat > {told}")}
    _provider, client, tid = await _run(tmp_path, hooks,
                                        [reply("", ("Read", {"file_path": "a.txt"}))])
    waits = [r.event for r in await client.events(tid) if isinstance(r.event, Waiting)]
    assert waits[-1].on == "approval"
    for _ in range(100):  # the Notification hook is not waited for
        if told.exists() and told.read_text():
            break
        await asyncio.sleep(0.02)
    assert json.loads(told.read_text())["message"] == "Noeta needs your permission"


async def test_post_tool_use_feedback_follows_the_result(tmp_path: Path) -> None:
    hooks = {"PostToolUse": _hook("echo 'lint: 2 warnings' >&2; exit 2", matcher="Read")}
    provider, _c, _t = await _run(tmp_path, hooks,
                                  [reply("", ("Read", {"file_path": "a.txt"})), reply("ok")])
    second = provider.requests[1].messages
    assert second[-1].origin == "system"
    assert second[-1].text == "PostToolUse hook (Read): lint: 2 warnings"
    assert second[-2].role == "user" and second[-2].content[0].call_id  # type: ignore[union-attr]


async def test_stop_hook_keeps_the_model_going_then_lets_it_stop(tmp_path: Path) -> None:
    marker = tmp_path / "ran_once"
    script = (f"if [ -f {marker} ]; then exit 0; fi; touch {marker}; "
              "echo 'Run the tests before you stop.' >&2; exit 2")
    hooks = {"Stop": _hook(script)}
    provider, client, tid = await _run(tmp_path, hooks, [reply("done"), reply("tests pass")])
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1].text == "Run the tests before you stop."
    assert (await client.get(tid)).status == "waiting"


async def test_stop_hook_that_always_blocks_is_capped(tmp_path: Path) -> None:
    hooks = {"Stop": _hook("echo again >&2; exit 2")}
    provider, _c, _t = await _run(tmp_path, hooks, [reply(str(i)) for i in range(20)])
    assert len(provider.requests) == 9  # the first answer plus eight continuations


def test_parse_and_settings_files(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": _hook("true"), "Bogus": _hook("x")}}))
    hooks = Hooks(settings_files=[settings, tmp_path / "missing.json"],
                  hooks={"Stop": _hook("false")})
    assert [h.command for m in hooks.matchers["Stop"] for h in m.hooks] == ["true", "false"]
    with pytest.raises(ValueError):
        parse_hooks({"Stop": [{"hooks": [{"type": "command"}]}]})
    [m] = parse_hooks({"PreToolUse": _hook("x", matcher="Edit|Write")})["PreToolUse"]
    assert m.matches("Edit") and m.matches("Write") and not m.matches("EditX")
