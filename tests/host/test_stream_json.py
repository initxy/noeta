"""Claude Code's headless protocol, as a client written for Claude Code's
Agent SDK speaks it: ``noeta`` started with ``--input-format stream-json
--output-format stream-json --verbose`` (the command line the Python SDK
runs), driven over stdin, and the conversation object both ``-p`` and the
HTTP socket run on."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import noeta.cli.main as cli
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.extension import Ask, Extension, TaskView
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ToolUse, Usage
from noeta.host.local import LocalApp
from noeta.host.stream_json import HeadlessOptions, StreamJson, drive
from noeta.plugins.ask_user import AskUser
from noeta.testing import ScriptedProvider, reply

MODEL = "claude-opus-5-5"


async def _no_sleep(_: float) -> None:
    return None


class Echo:
    name = "Echo"
    description = "e"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.runs.append(input)
        return text_output("echoed")


class AskEcho(Extension):
    name = "asker"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="may I echo?") if call.name == "Echo" else None


def local(tmp_path: Path, provider: Any, *agents: Agent, **kw: Any) -> LocalApp:
    return LocalApp(SqliteStore(str(tmp_path / "t.db")), list(agents), provider,
                    env_for=lambda _s: None, sleep=_no_sleep, own_tasks_only=True, **kw)


# ------------------------------------------------------------ the command line


def test_the_sdk_command_line_over_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    provider = ScriptedProvider([reply("hello there", usage=Usage(uncached=12, output=3)),
                                 reply("second answer")])
    agent = Agent(name="main", model=MODEL)
    monkeypatch.setattr(cli, "_app", lambda s, cwd=None, **kw: local(tmp_path, provider, agent))
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    read, write = os.pipe()
    lines = [
        {"type": "control_request", "request_id": "req_1",
         "request": {"subtype": "initialize", "hooks": None}},
        {"type": "user", "session_id": "", "parent_tool_use_id": None,
         "message": {"role": "user", "content": "say hi"}},
        {"type": "user", "session_id": "", "parent_tool_use_id": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "again"}]}},
    ]
    os.write(write, "".join(json.dumps(x) + "\n" for x in lines).encode())
    os.close(write)
    monkeypatch.setattr("sys.stdin", os.fdopen(read))
    with pytest.raises(SystemExit) as done:
        cli.main(["--output-format", "stream-json", "--verbose", "--system-prompt", "",
                  "--model", MODEL, "--permission-mode", "default", "--max-turns", "5",
                  "--setting-sources=", "--input-format", "stream-json", "--cwd", str(tmp_path)])
    assert done.value.code == 0
    captured = capsys.readouterr()
    out = [json.loads(x) for x in captured.out.splitlines()]
    assert "--setting-sources is accepted for compatibility and ignored" in captured.err
    init = out[0]
    assert (init["type"], init["subtype"]) == ("system", "init")
    task_id = init["session_id"]
    assert init["model"] == MODEL and init["permissionMode"] == "default"
    assert init["cwd"] == str(tmp_path) and "main" in init["agents"]
    [ack] = [x for x in out if x["type"] == "control_response"]
    assert ack["response"]["subtype"] == "success" and ack["response"]["request_id"] == "req_1"
    assert {"commands", "models", "account", "pid"} <= set(ack["response"]["response"])
    assistants = [x for x in out if x["type"] == "assistant"]
    assert [a["message"]["content"] for a in assistants] == [
        [{"type": "text", "text": "hello there"}], [{"type": "text", "text": "second answer"}]]
    assert assistants[0]["message"]["model"] and assistants[0]["session_id"] == task_id
    assert assistants[0]["message"]["usage"]["input_tokens"] == 12
    results = [x for x in out if x["type"] == "result"]
    assert [r["result"] for r in results] == ["hello there", "second answer"]
    for r in results:
        # What the SDK's message parser requires of a result.
        assert {"subtype", "duration_ms", "duration_api_ms", "is_error", "num_turns",
                "session_id", "total_cost_usd", "usage", "modelUsage",
                "permission_denials", "uuid"} <= set(r)
        assert r["subtype"] == "success" and r["is_error"] is False and r["num_turns"] == 1
    assert results[0]["usage"]["input_tokens"] == 12 and results[1]["total_cost_usd"] > 0
    assert provider.requests[0].system == []  # --system-prompt "" as the SDK sends it


def test_print_mode_text_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    agent = Agent(name="main", model=MODEL)
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    provider = ScriptedProvider([reply("plain answer"), reply('{"n": 3}')])
    monkeypatch.setattr(cli, "_app", lambda s, cwd=None, **kw: local(tmp_path, provider, agent))
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "what?", "--model", MODEL, "--cwd", str(tmp_path)])
    assert done.value.code == 0 and capsys.readouterr().out.strip() == "plain answer"
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "--output-format", "json", "--json-schema", '{"type": "object"}',
                  "count", "--model", MODEL, "--cwd", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    assert done.value.code == 0 and result["structured_output"] == {"n": 3}
    assert provider.requests[-1].output_schema == {"type": "object"}
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "--output-format", "stream-json", "x", "--model", MODEL])
    assert done.value.code == 1 and "requires --verbose" in capsys.readouterr().err


def test_flags_become_permission_rules() -> None:
    from noeta.cli.headless import permission_arguments

    args = cli._front_parser().parse_args([
        "-p", "--allowedTools", "Bash(git *) Edit,Read", "--disallowedTools", "WebFetch",
        "Bash(rm *)", "--tools", "Read,Edit", "--add-dir", "/tmp", "--", "go"])
    assert args.prompt == "go"
    assert permission_arguments(args) == {
        "allow": ["Bash(git *)", "Edit", "Read"], "deny": ["WebFetch", "Bash(rm *)"],
        "disallowed_tools": ["WebFetch"], "allowed_tools": ["Read", "Edit"],
        "additional_directories": [str(Path("/tmp").resolve())]}
    assert cli._subcommand(["--cwd", "/x", "trust"]) and not cli._subcommand(["-p", "trust"])


# ------------------------------------------------------------ the control protocol


class Client:
    """The client side: lines in, messages out, answering what it is asked."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.out: list[dict[str, Any]] = []
        self.seen = asyncio.Event()
        self.answer: Any = None

    async def lines(self) -> AsyncIterator[dict[str, Any]]:
        while (line := await self.inbox.get()) is not None:
            yield line

    async def emit(self, message: dict[str, Any]) -> None:
        self.out.append(message)
        self.seen.set()
        if message.get("type") == "control_request" and self.answer is not None:
            await self.inbox.put({"type": "control_response", "response": {
                "subtype": "success", "request_id": message["request_id"],
                "response": self.answer(message["request"])}})

    async def until(self, check: Any, timeout: float = 10) -> dict[str, Any]:
        async def look() -> dict[str, Any]:
            while True:
                for m in self.out:
                    if check(m):
                        return m
                self.seen.clear()
                await self.seen.wait()

        return await asyncio.wait_for(look(), timeout)

    def say(self, text: str, uuid: str = "") -> None:
        self.inbox.put_nowait({"type": "user", "uuid": uuid or None,
                               "message": {"role": "user", "content": text}})

    def control(self, request_id: str, **request: Any) -> None:
        self.inbox.put_nowait({"type": "control_request", "request_id": request_id,
                               "request": request})


def results(c: Client) -> list[dict[str, Any]]:
    return [m for m in c.out if m["type"] == "result"]


async def test_permission_prompts_go_to_the_client(tmp_path: Path) -> None:
    echo = Echo()
    provider = ScriptedProvider([reply("", ("Echo", {"x": 1})), reply("echo done"),
                                 reply("", ("Echo", {"x": 2})), reply("refused, moving on"),
                                 reply("on the new model")])
    app = local(tmp_path, provider, Agent(name="main", model=MODEL, tools=[echo],
                                          extensions=[AskEcho()]))
    c = Client()
    allow = True

    def answer(request: dict[str, Any]) -> dict[str, Any]:
        assert request["subtype"] == "can_use_tool" and request["tool_name"] == "Echo"
        assert request["decision_reason"] == "may I echo?" and request["tool_use_id"]
        if allow:
            return {"behavior": "allow", "updatedInput": request["input"]}
        return {"behavior": "deny", "message": "not today"}

    c.answer = answer
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path), ask_client=True,
                                               include_partial_messages=True), c.emit)
        running = asyncio.create_task(drive(conv, lines=c.lines()))
        c.say("echo please")
        await c.until(lambda m: m["type"] == "result")
        assert echo.runs == [{"x": 1}] and results(c)[0]["result"] == "echo done"
        allow = False
        c.say("echo again")
        await c.until(lambda m: m["type"] == "result" and m["result"] == "refused, moving on")
        assert echo.runs == [{"x": 1}]
        [denied] = results(c)[1]["permission_denials"]
        assert denied["tool_name"] == "Echo" and denied["tool_input"] == {"x": 2}
        refused = [m for m in c.out if m["type"] == "user"][-1]["message"]["content"][0]
        assert refused["is_error"] and refused["content"] == "not today"

        c.control("r1", subtype="set_model", model="claude-sonnet-5-5")
        c.control("r2", subtype="set_permission_mode", mode="acceptEdits")
        c.control("r3", subtype="nonsense")
        await c.until(lambda m: m.get("response", {}).get("request_id") == "r3")
        acks = {m["response"]["request_id"]: m["response"] for m in c.out
                if m["type"] == "control_response"}
        assert acks["r1"]["subtype"] == "success"
        assert acks["r2"]["response"] == {"mode": "acceptEdits"}
        assert acks["r3"]["subtype"] == "error" and "nonsense" in acks["r3"]["error"]
        c.say("third")
        await c.until(lambda m: m["type"] == "result" and m["result"] == "on the new model")
        assert provider.requests[-1].model == "claude-sonnet-5-5"
        kinds = [m["event"]["type"] for m in c.out if m["type"] == "stream_event"]
        assert kinds[:4] == ["message_start", "content_block_start", "content_block_delta",
                             "content_block_stop"]
        assert "message_stop" in kinds
        await c.inbox.put(None)
        assert (await asyncio.wait_for(running, 10)) is not None


async def test_an_approval_with_updated_input_runs_the_updated_call(tmp_path: Path) -> None:
    from noeta.plugins.permissions import Permissions

    echo = Echo()
    provider = ScriptedProvider([reply("", ("Echo", {"x": 1})), reply("echo done")])
    app = local(tmp_path, provider, Agent(name="main", model=MODEL, tools=[echo],
                                          extensions=[Permissions(mode="default"),
                                                      AskEcho()]))
    c = Client()
    c.answer = lambda request: {"behavior": "allow",
                                "updatedInput": {**request["input"], "x": 42}}
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path), ask_client=True),
                          c.emit)
        running = asyncio.create_task(drive(conv, lines=c.lines()))
        c.say("echo please")
        await c.until(lambda m: m["type"] == "result")
        assert echo.runs == [{"x": 42}] and results(c)[0]["result"] == "echo done"
        asks = [m for m in c.out if m["type"] == "control_request"]
        assert len(asks) == 1  # the person's approval covers the changed call
        await c.inbox.put(None)
        await asyncio.wait_for(running, 10)


async def test_without_a_prompt_host_asks_are_refused_and_the_turn_goes_on(
        tmp_path: Path) -> None:
    echo = Echo()
    provider = ScriptedProvider([reply("", ("Echo", {})), reply("could not echo")])
    app = local(tmp_path, provider, Agent(name="main", model=MODEL, tools=[echo],
                                          extensions=[AskEcho()]))
    c = Client()
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path)), c.emit)
        from noeta.core.types import Message, Text
        result = await asyncio.wait_for(drive(conv, prompt=Message(
            role="user", content=[Text(text="go")], origin="human")), 10)
    assert result is not None and result["result"] == "could not echo"
    assert not echo.runs and result["permission_denials"][0]["tool_name"] == "Echo"


async def test_questions_interrupts_max_turns_and_rewind(tmp_path: Path) -> None:
    question = {"questions": [{"question": "Which database?", "header": "DB",
                               "multiSelect": False,
                               "options": [{"label": "SQLite", "description": "a file"},
                                           {"label": "Postgres", "description": "a server"}]}]}
    echo = Echo()
    provider = ScriptedProvider([
        reply("", ("AskUserQuestion", question)), reply("sqlite it is"),
        reply("", ("Echo", {})), reply("", ("Echo", {})), reply("never sent")])
    app = local(tmp_path, provider, Agent(name="main", model=MODEL, tools=[echo],
                                          extensions=[AskUser()]))
    c = Client()
    c.answer = lambda r: {"behavior": "allow", "updatedInput": {
        **r["input"], "answers": {"Which database?": "SQLite"}}}
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path), ask_client=True,
                                               max_turns=2, replay_user_messages=True), c.emit)
        running = asyncio.create_task(drive(conv, lines=c.lines()))
        c.say("set up storage", uuid="u-1")
        await c.until(lambda m: m["type"] == "result")
        assert results(c)[0]["result"] == "sqlite it is"
        tool_result = next(m for m in c.out if m["type"] == "user" and not m.get("isReplay"))
        assert '="SQLite"' in tool_result["message"]["content"][0]["content"]
        assert next(m for m in c.out if m.get("isReplay"))["message"]["content"] == [
            {"type": "text", "text": "set up storage"}]
        c.say("echo twice", uuid="u-2")
        last = await c.until(lambda m: m["type"] == "result" and m["subtype"] != "success")
        assert last["subtype"] == "error_max_turns" and last["is_error"]
        assert last["errors"] == ["Reached maximum number of turns (2)"]
        c.control("rw", subtype="rewind_files", user_message_id="u-1", dry_run=True)
        c.control("rw2", subtype="rewind_files", user_message_id="nope")
        c.control("ctx", subtype="get_context_usage")
        c.control("cost", subtype="get_session_cost")
        await c.until(lambda m: m.get("response", {}).get("request_id") == "cost")
        acks = {m["response"]["request_id"]: m["response"].get("response") for m in c.out
                if m["type"] == "control_response"}
        assert acks["rw"] == {"canRewind": True, "filesChanged": [], "insertions": 0,
                              "deletions": 0}
        assert acks["rw2"]["canRewind"] is False
        assert acks["ctx"]["maxTokens"] > acks["ctx"]["totalTokens"] > 0
        assert "Total cost" in acks["cost"]["text"]
        await c.inbox.put(None)
        await asyncio.wait_for(running, 10)
