"""Claude Code's command hooks, end to end with real ``sh`` commands: what
each event receives, adds for the model, and blocks."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import ContextOverflow
from noeta.core.events import Compacted, ExtensionRecorded, ToolFinished, Waiting
from noeta.core.extension import Extension, TaskView
from noeta.core.loop.state import fold
from noeta.core.runner import Runtime
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.plugins.fs import FileTools
from noeta.plugins.hooks import Hooks, parse_hooks
from noeta.plugins.hooks.config import HookCommand
from noeta.plugins.hooks.runners import OUTPUT_CAP, run_command
from noeta.plugins.markdown_commands import MarkdownCommands
from noeta.plugins.skills import Skills
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


async def test_a_blocked_prompt_is_erased_from_later_requests(tmp_path: Path) -> None:
    hooks = {"UserPromptSubmit": _hook("grep -q SECRET && { echo blocked >&2; exit 2; }; exit 0")}
    provider, client, tid = await _run(tmp_path, hooks, [reply("ok")],
                                       prompts=("my SECRET is 42", "hello"))
    assert len(provider.requests) == 1
    texts = [m.text for m in provider.requests[0].messages]
    assert any("hello" in t for t in texts)
    assert not any("SECRET" in t for t in texts)
    state = fold(await client.events(tid), {})
    assert not any("SECRET" in m.text for m in state.history)


async def _blocked_expansion(tmp_path: Path, extension: Any, prompt: str) -> list[str]:
    hooks = {"UserPromptSubmit": _hook("grep -q SECRET && { echo blocked >&2; exit 2; }; "
                                       "exit 0")}
    work = tmp_path / "w"
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("ok")])
    agent = Agent(name="a", model="m", extensions=[extension, Hooks(hooks=hooks), FileTools()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", prompt, config={"cwd": str(work)})
    await rt.run_until_idle()
    await client.send(tid, "hello")
    await rt.run_until_idle()
    assert len(provider.requests) == 1
    return [m.text for m in provider.requests[0].messages]


async def test_a_blocked_command_loses_its_expansion_too(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    commands = MarkdownCommands([{"name": "deploy", "body": "Deploy with key $ARGUMENTS now."}])
    texts = await _blocked_expansion(tmp_path, commands, "/deploy SECRET42")
    assert any("hello" in t for t in texts)
    assert not any("SECRET" in t for t in texts)


async def test_a_blocked_skill_command_loses_its_expansion_too(tmp_path: Path) -> None:
    skill = tmp_path / "w" / ".noeta" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy\ndescription: Ship it.\n---\n"
                                    "Deploy with key $ARGUMENTS now.\n")
    skills = Skills(global_dir=None, global_agents_dir=None)
    texts = await _blocked_expansion(tmp_path, skills, "/deploy SECRET42")
    assert any("hello" in t for t in texts)
    assert not any("SECRET" in t for t in texts)


class _MakeRoom(Extension):
    """Compacts the whole conversation when the model reports an overflow."""

    name = "room"

    async def on_context_overflow(self, view: TaskView) -> bool:
        view.compacted(Compacted(summary="(summary)", through=view.state.last_seq,
                                 lead=await view.residents(lead=True, keeps_tail=False),
                                 after=await view.residents(keeps_tail=False)))
        return True


async def test_a_blocked_skill_command_leaves_no_activation_behind(tmp_path: Path) -> None:
    skill = tmp_path / "w" / ".noeta" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy\ndescription: Ship it.\n"
                                    "allowed-tools: Bash(rm:*)\n---\n"
                                    "Deploy with key $ARGUMENTS now.\n")
    hooks = {"UserPromptSubmit": _hook("grep -q SECRET && { echo blocked >&2; exit 2; }; "
                                       "exit 0")}
    skills = Skills(global_dir=None, global_agents_dir=None)
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        ContextOverflow("too long"), reply("fits"),
        reply("", ("Skill", {"skill": "deploy", "args": "SECRET42"})), reply("done")])
    agent = Agent(name="a", model="m",
                  extensions=[skills, Hooks(hooks=hooks), FileTools(), _MakeRoom()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(tmp_path / "w")), sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "/deploy SECRET42", config={"cwd": str(tmp_path / "w")})
    await rt.run_until_idle()
    state = fold(await client.events(tid), {"skills": skills.reduce})
    assert state.ext["skills"]["active"] == {}
    await client.send(tid, "hello")
    await rt.run_until_idle()
    # The request rebuilt after the compaction re-attaches no blocked skill.
    assert not any("SECRET" in m.text for m in provider.requests[1].messages)
    await client.send(tid, "use it")
    await rt.run_until_idle()
    kinds = [r.event.kind for r in await client.events(tid)
             if isinstance(r.event, ExtensionRecorded) and r.event.extension == "skills"]
    # The model's own call with the same arguments loads the skill in full.
    assert "reinvoked" not in kinds and kinds[-1] == "activated"
    loaded = provider.requests[-1].messages[-1].text
    assert "Deploy with key SECRET42 now." in loaded and "earlier" not in loaded


async def test_a_blocked_command_leaves_no_pre_approval_behind(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    commands = MarkdownCommands([{"name": "go", "body": "go $ARGUMENTS",
                                  "allowed_tools": "Bash(rm:*)"}])
    await _blocked_expansion(tmp_path, commands, "/go SECRET99")
    store = SqliteStore(str(tmp_path / "t.db"))
    client = Client(store)
    [tid] = [t.task_id for t in await client.tasks()]
    state = fold(await client.events(tid), {"markdown_commands": commands.reduce})
    assert state.ext["markdown_commands"]["allow_rules"] == []


class _Interject(Extension):
    """A ``Say`` tool: the person's ``input["text"]`` arrives mid-turn."""

    name = "interject"

    def __init__(self, client: Client) -> None:
        self.client = client

    def tools(self, view: TaskView) -> list[Tool]:
        return [_SayTool(self.client)]


class _SayTool:
    name = "Say"
    description = "Say."
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = True
    clearable = False

    def __init__(self, client: Client) -> None:
        self.client = client

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        await self.client.send(ctx.task_id, input["text"], when="now")
        return text_output("said")


async def _mid_turn(tmp_path: Path, steps: list[Any],
                    prompts: tuple[str, ...]) -> ScriptedProvider:
    hooks = {"UserPromptSubmit": _hook("grep -q SECRET && { echo blocked >&2; exit 2; }; "
                                       "exit 0")}
    work = tmp_path / "w"
    work.mkdir()
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider(steps)
    client = Client(store)
    agent = Agent(name="a", model="m", extensions=[_Interject(client), Hooks(hooks=hooks)])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)
    tid = await client.create("a", prompts[0], config={"cwd": str(work)})
    await rt.run_until_idle()
    for p in prompts[1:]:
        await client.send(tid, p)
        await rt.run_until_idle()
    return provider


async def test_a_message_sent_mid_turn_goes_through_the_hook(tmp_path: Path) -> None:
    provider = await _mid_turn(tmp_path, [reply("", ("Say", {"text": "my SECRET is 42"})),
                                          reply("ok")], ("go", "hello"))
    assert all("SECRET" not in m.text for r in provider.requests for m in r.messages)
    assert any(m.text == "hello" for m in provider.requests[-1].messages)


async def test_a_blocked_prompt_leaves_what_the_model_already_saw(tmp_path: Path) -> None:
    provider = await _mid_turn(tmp_path, [reply("", ("Say", {"text": "also check docs"})),
                                          reply("done"), reply("ok")],
                               ("go", "my SECRET is 42", "hello"))
    assert len(provider.requests) == 3
    texts = [m.text for m in provider.requests[-1].messages]
    assert "also check docs" in texts and "hello" in texts
    assert not any("SECRET" in t for t in texts)


async def test_a_prompt_hook_added_later_checks_only_what_the_model_has_not_seen(
        tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("noted"), reply("ok")])
    client = Client(store)

    def runtime(hooks: dict[str, Any]) -> Runtime:
        agent = Agent(name="a", model="m", extensions=[Hooks(hooks=hooks)])
        return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                       env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)

    tid = await client.create("a", "my SECRET is 42", config={"cwd": str(work)})
    await runtime({}).run_until_idle()
    seen = tmp_path / "seen.json"
    blocking = {"UserPromptSubmit": _hook(f"tee {seen} | grep -q SECRET && "
                                          "{ echo blocked >&2; exit 2; }; exit 0")}
    await client.send(tid, "hello")
    await runtime(blocking).run_until_idle()
    assert json.loads(seen.read_text())["prompt"] == "hello"
    assert len(provider.requests) == 2
    assert [m.text for m in provider.requests[1].messages
            if m.role == "user"][-2:] == ["my SECRET is 42", "hello"]


async def test_pre_tool_use_blocks_matching_calls_only(tmp_path: Path) -> None:
    hooks = {"PreToolUse": _hook("echo 'reads are off today' >&2; exit 2", matcher="Read|Glob")}
    steps = [reply("", ("Read", {"file_path": "a.txt"}), ("Write", {"file_path": "b.txt", "content": "x"})),
             reply("done")]
    _provider, client, tid = await _run(tmp_path, hooks, steps)
    results = {r.event.result.call_id: r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)}
    read, write = results.values()
    assert read.is_error and "reads are off today" in read.content[0].text  # type: ignore[union-attr]
    assert not write.is_error


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
    told_json = json.loads(told.read_text())
    assert told_json["message"] == "Noeta needs your permission to use Read"
    assert told_json["notification_type"] == "permission_prompt"


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


async def test_a_cancelled_command_hook_kills_its_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    hook = HookCommand(command=f"sleep 30 & echo $! > {pid_file}; wait", timeout=60)
    run = asyncio.create_task(run_command(hook, {}, str(tmp_path)))
    for _ in range(500):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.01)
    pid = int(pid_file.read_text())
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    for _ in range(200):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail("the hook's child outlived the cancelled hook")


async def test_a_command_hook_keeps_only_the_head_of_a_flood(tmp_path: Path) -> None:
    hook = HookCommand(command="head -c 3000000 /dev/zero | tr '\\0' x; echo oops >&2")
    result = await run_command(hook, {"big": "x" * 200_000}, str(tmp_path))
    assert result.exit_code == 0
    assert result.stdout == "x" * OUTPUT_CAP and result.stderr == "oops\n"
