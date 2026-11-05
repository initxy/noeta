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
from noeta.plugins.structured_output import StructuredOutput
from noeta.plugins.title import Title
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
                  "--setting-sources=", "--betas", "x", "--input-format", "stream-json",
                  "--cwd", str(tmp_path)])
    assert done.value.code == 0
    captured = capsys.readouterr()
    out = [json.loads(x) for x in captured.out.splitlines()]
    assert "--betas is accepted for compatibility and ignored" in captured.err
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
    agent = Agent(name="main", model=MODEL, extensions=[StructuredOutput()])
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    provider = ScriptedProvider([reply("plain answer"),
                                 reply("", ("StructuredOutput", {"n": 3})),
                                 reply("counted", ("StructuredOutput", {"n": 4}))])
    monkeypatch.setattr(cli, "_app", lambda s, cwd=None, **kw: local(tmp_path, provider, agent))
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "what?", "--model", MODEL, "--cwd", str(tmp_path)])
    assert done.value.code == 0 and capsys.readouterr().out.strip() == "plain answer"
    assert "StructuredOutput" not in [t.name for t in provider.requests[0].tools]
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "--output-format", "json", "--json-schema", json.dumps(schema),
                  "count", "--model", MODEL, "--cwd", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    assert done.value.code == 0 and result["structured_output"] == {"n": 3}
    # Claude Code's: the reply's text, else the value's compact JSON.
    assert result["result"] == '{"n":3}' and result["subtype"] == "success"
    [tool] = [t for t in provider.requests[-1].tools if t.name == "StructuredOutput"]
    assert tool.input_schema == schema and provider.requests[-1].output_schema is None
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "--json-schema", json.dumps(schema), "count again", "--model", MODEL,
                  "--cwd", str(tmp_path)])
    assert done.value.code == 0 and capsys.readouterr().out.strip() == "counted"
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "--json-schema", "{not json", "x", "--model", MODEL])
    assert done.value.code == 1
    assert "--json-schema is not a valid JSON Schema" in capsys.readouterr().err
    # A root that is no object would make every request fail: refused at start.
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "--json-schema", '{"type": "array"}', "x", "--model", MODEL])
    assert done.value.code == 1 and '"type": "object"' in capsys.readouterr().err
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


async def test_a_lost_event_stream_reports_no_raw_error_text(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import noeta.host.stream_json as sj

    async def broken(*_a: Any, **_k: Any) -> AsyncIterator[Any]:
        json.loads("{")  # a library's error, naming nothing of noeta's
        yield None

    monkeypatch.setattr(sj, "_follow_tree", broken)
    app = local(tmp_path, ScriptedProvider([reply("hi")]), Agent(name="main", model=MODEL))
    c = Client()
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path)), c.emit)
        from noeta.core.types import Message, Text
        result = await asyncio.wait_for(drive(conv, prompt=Message(
            role="user", content=[Text(text="go")], origin="human")), 10)
    assert result is not None and result["subtype"] == "error_during_execution"
    assert "Expecting property name" not in json.dumps(result)


async def test_a_result_slow_to_put_together_is_still_reported(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Its usage is read from the store after the turn closes; a read slower
    # than ``settled``'s poll once ended the run with no result (exit 1).
    import noeta.host.stream_json as sj

    real = sj.tree_usage

    async def slow(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(1.2)
        return await real(*a, **k)

    monkeypatch.setattr(sj, "tree_usage", slow)
    app = local(tmp_path, ScriptedProvider([reply("all done")]), Agent(name="main", model=MODEL))
    c = Client()
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path)), c.emit)
        from noeta.core.types import Message, Text
        result = await asyncio.wait_for(drive(conv, prompt=Message(
            role="user", content=[Text(text="go")], origin="human")), 10)
    assert result is not None and result["result"] == "all done"
    assert [r["result"] for r in results(c)] == ["all done"]


async def test_a_prompt_sent_while_a_result_is_put_together_waits_for_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import noeta.host.stream_json as sj

    real = sj.tree_usage

    async def slow(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(1.0)
        return await real(*a, **k)

    monkeypatch.setattr(sj, "tree_usage", slow)
    app = local(tmp_path, ScriptedProvider([reply("a1"), reply("a2")]),
                Agent(name="main", model=MODEL))
    c = Client()
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path),
                                               replay_user_messages=True), c.emit)
        run = asyncio.create_task(drive(conv, lines=c.lines()))
        c.say("first")
        await c.until(lambda m: m["type"] == "assistant")
        while conv._turn is not None:  # closed; its result not out for a second yet
            await asyncio.sleep(0.01)
        c.say("second")
        await c.until(lambda m: m["type"] == "result" and m["result"] == "a2")
        c.inbox.put_nowait(None)
        await asyncio.wait_for(run, 10)
    kinds = [m["type"] for m in c.out if m["type"] in ("user", "assistant", "result")]
    assert kinds == ["user", "assistant", "result", "user", "assistant", "result"]


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


class Busy:
    name = "Busy"
    description = "b"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.progress is not None
        ctx.progress("working")
        return text_output("busy done")


async def test_a_running_tool_reports_claude_codes_tool_progress(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Busy", {})), reply("all done")])
    app = local(tmp_path, provider, Agent(name="main", model=MODEL, tools=[Busy()]))
    c = Client()
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path)), c.emit)
        running = asyncio.create_task(drive(conv, lines=c.lines()))
        c.say("be busy")
        await c.until(lambda m: m["type"] == "result")
        [progress] = [m for m in c.out if m["type"] == "tool_progress"]
        [call] = [b for m in c.out if m["type"] == "assistant"
                  for b in m["message"]["content"] if b["type"] == "tool_use"]
        assert progress["tool_use_id"] == call["id"] and progress["tool_name"] == "Busy"
        assert progress["parent_tool_use_id"] is None
        assert progress["elapsed_time_seconds"] == 0 and progress["session_id"]
        assert not [m for m in c.out if m["type"] == "stream_event"]  # no partials asked
        await c.inbox.put(None)
        await running


# ------------------------------------------------------------ not interactive


def _tool_names(request: Any) -> list[str]:
    return [t.name for t in request.tools]


async def test_a_headless_task_is_not_interactive(tmp_path: Path) -> None:
    """As Claude Code's print and SDK sessions: no title side request, and
    ``AskUserQuestion`` only when the client answers prompts."""
    from noeta.core.types import Message, Text

    provider = ScriptedProvider([reply("first"), reply("second")])
    agent = Agent(name="main", model=MODEL, extensions=[AskUser(), Title()])
    app = local(tmp_path, provider, agent)
    prompt = Message(role="user", content=[Text(text="name this task please")],
                     origin="human")
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path)), Client().emit)
        await asyncio.wait_for(drive(conv, prompt=prompt), 10)
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path), ask_client=True),
                          Client().emit)
        await asyncio.wait_for(drive(conv, prompt=prompt), 10)
        # It runs no ``!`` commands: the main prompt's line suggesting one is left out.
        for row in await app.client.tasks():
            assert "bash_mode" not in (await app.client.events(row.task_id))[0].event.config
    # Two main requests and no title request.
    assert len(provider.requests) == 2
    assert "AskUserQuestion" not in _tool_names(provider.requests[0])
    assert "AskUserQuestion" in _tool_names(provider.requests[1])


async def test_resuming_headless_sets_this_runs_config(tmp_path: Path) -> None:
    from noeta.core.types import Message, Text

    provider = ScriptedProvider([reply("in the terminal"),
                                 reply("", ("StructuredOutput", {"a": 1})), reply("plain")])
    agent = Agent(name="main", model=MODEL, extensions=[AskUser(), StructuredOutput()])
    app = local(tmp_path, provider, agent)
    prompt = Message(role="user", content=[Text(text="go")], origin="human")
    async with app:
        tid = await app.client.create("main", "start", config={"cwd": str(tmp_path)})
        await app.client.wait(tid, timeout=10)
        options = HeadlessOptions(model=MODEL, cwd=str(tmp_path), resume=tid,
                                  output_schema={"type": "object"})
        result = await asyncio.wait_for(drive(StreamJson(app, options, Client().emit),
                                              prompt=prompt), 10)
        assert result is not None and result["structured_output"] == {"a": 1}
        options = HeadlessOptions(model=MODEL, cwd=str(tmp_path), resume=tid, ask_client=True)
        result = await asyncio.wait_for(drive(StreamJson(app, options, Client().emit),
                                              prompt=prompt), 10)
        assert result is not None and "structured_output" not in result
    names = [_tool_names(r) for r in provider.requests]
    assert "AskUserQuestion" in names[0] and "StructuredOutput" not in names[0]
    assert "AskUserQuestion" not in names[1] and "StructuredOutput" in names[1]
    assert "AskUserQuestion" in names[2] and "StructuredOutput" not in names[2]


async def test_structured_output_over_the_protocol(tmp_path: Path) -> None:
    """The client's ``initialize`` ``jsonSchema`` (the SDK's ``outputFormat``),
    and Claude Code's result when the model keeps failing the schema."""
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    bad = [reply("", ("StructuredOutput", {"n": "x"})) for _ in range(5)]
    provider = ScriptedProvider([reply("ok", ("StructuredOutput", {"n": 1})), *bad])
    app = local(tmp_path, provider, Agent(name="main", model=MODEL,
                                          extensions=[StructuredOutput()]))
    c = Client()
    async with app:
        conv = StreamJson(app, HeadlessOptions(model=MODEL, cwd=str(tmp_path)), c.emit)
        running = asyncio.create_task(drive(conv, lines=c.lines()))
        c.control("init", subtype="initialize", jsonSchema=schema)
        c.say("count")
        first = await c.until(lambda m: m["type"] == "result")
        assert first["structured_output"] == {"n": 1} and first["result"] == "ok"
        c.say("count badly")
        last = await c.until(lambda m: m["type"] == "result" and m["is_error"])
        assert last["subtype"] == "error_max_structured_output_retries"
        assert last["errors"][0].startswith("StructuredOutput was called 5 times")
        assert "structured_output" not in last
        c.control("bad", subtype="initialize", jsonSchema=[1])
        await c.until(lambda m: m.get("response", {}).get("request_id") == "bad")
        c.control("array", subtype="initialize", jsonSchema={"type": "array"})
        await c.until(lambda m: m.get("response", {}).get("request_id") == "array")
        await c.inbox.put(None)
        await asyncio.wait_for(running, 10)
    [refused] = [m["response"] for m in c.out if m["type"] == "control_response"
                 and m["response"]["request_id"] == "bad"]
    assert refused["subtype"] == "error" and "jsonSchema" in refused["error"]
    [refused] = [m["response"] for m in c.out if m["type"] == "control_response"
                 and m["response"]["request_id"] == "array"]
    assert refused["subtype"] == "error" and '"type": "object"' in refused["error"]
    assert len(provider.requests) == 6
