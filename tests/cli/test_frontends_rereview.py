"""Regressions from the re-review (front ends): a typed answer to the real
AskUserQuestion is accepted, sub-agents' approvals and questions reach the
TUI and ``noeta -p`` (which refuses them, as Claude Code's headless mode does), a task whose agent
this front end lacks opens read-only, and ``-p`` streams partial messages
without a failed attempt's fragments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import noeta.cli.main as cli
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.errors import Transient
from noeta.core.events import ToolFinished
from noeta.core.extension import Ask, Extension, TaskView
from noeta.core.tool import Park, ToolContext, ToolOutput, text_output
from noeta.core.types import Delta, ModelRequest, ModelResponse, ToolUse
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider, reply

MODEL = "claude-sonnet-4-5"
DB_QUESTION = {"questions": [{
    "question": "Which database?", "header": "DB", "multiSelect": False,
    "options": [{"label": "SQLite", "description": "a file"},
                {"label": "Postgres", "description": "a server"}]}]}


async def _no_sleep(_: float) -> None:
    return None


class Delegate:
    name = "Task"
    description = "d"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.spawn is not None
        return ToolOutput(park=Park(on="children", keys=[ctx.spawn("worker", "sub job")]))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        [ended] = outcome.values()
        return text_output(ended.answer)


class Echo:
    name = "Echo"
    description = "e"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("echoed")


class AskEcho(Extension):
    name = "asker"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="may the worker echo?") if call.name == "Echo" else None


def main_and_worker(*worker_ext: Extension) -> list[Agent]:
    return [Agent(name="main", model=MODEL, tools=[Delegate()]),
            Agent(name="worker", model=MODEL, one_shot=True, tools=[Echo()],
                  extensions=worker_ext)]


def local(tmp_path: Path, provider: Any, *agents: Agent, **kw: Any) -> LocalApp:
    return LocalApp(SqliteStore(str(tmp_path / "t.db")), list(agents), provider,
                    env_for=lambda _s: None, sleep=_no_sleep, **kw)


async def results(app: LocalApp, task_id: str) -> list[ToolFinished]:
    return [r.event for r in await app.client.events(task_id)
            if isinstance(r.event, ToolFinished)]


# ------------------------------------------------------------ P1-1 typed answers


# ------------------------------------------------------------ P1-2 sub-agent requests


def test_print_mode_refuses_a_sub_agents_approval_and_goes_on(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Headless, nobody answers permission prompts (no ``--permission-prompt-tool
    stdio``): a sub-agent's ask is refused, as Claude Code refuses it, and the
    run goes on to its answer."""
    provider = ScriptedProvider([reply("", ("Task", {})), reply("", ("Echo", {})),
                                 reply("child gave up"), reply("final")])
    agents = main_and_worker(AskEcho())
    monkeypatch.setattr(cli, "_app", lambda s, cwd=None, **kw: local(tmp_path, provider, *agents))
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "--output-format", "json", "go", "--model", "m",
                  "--cwd", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    assert done.value.code == 0 and result["result"] == "final"
    [denied] = result["permission_denials"]
    assert denied["tool_name"] == "Echo"


# ------------------------------------------------------------ P2-4 unknown agent


# ------------------------------------------------------------ P3-8 -p --json


class RetryOnce:
    family = "x"

    def __init__(self) -> None:
        self.n = 0

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        self.n += 1
        if self.n == 1:
            if on_delta is not None:
                on_delta(Delta(kind="text", text="broken par"))
            raise Transient("stream cut")
        if on_delta is not None:
            on_delta(Delta(kind="text", text="good answer"))
        return reply("good answer")


def test_partial_messages_drop_a_failed_attempt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agents = [Agent(name="main", model=MODEL)]
    monkeypatch.setattr(cli, "_app", lambda s, cwd=None, **kw: local(tmp_path, RetryOnce(),
                                                                     *agents))
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "--output-format", "stream-json", "--verbose",
                  "--include-partial-messages", "go", "--model", "m", "--cwd", str(tmp_path)])
    assert done.value.code == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    events = [x["event"] for x in lines if x["type"] == "stream_event"]
    texts = [e["delta"]["text"] for e in events if e["type"] == "content_block_delta"]
    assert texts == ["broken par", "good answer"]
    starts = [i for i, e in enumerate(events) if e["type"] == "message_start"]
    assert len(starts) == 2 and events[starts[1] - 1]["type"] == "message_stop"
    [assistant] = [x for x in lines if x["type"] == "assistant"]
    assert assistant["message"]["content"] == [{"type": "text", "text": "good answer"}]
    assert lines[-1]["type"] == "result" and lines[-1]["result"] == "good answer"


def test_print_mode_waits_for_background_agents(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from noeta.plugins.subtasks import SubagentSpec, Subtasks

    roster = [SubagentSpec(name="general-purpose", description="Works.", agent="worker")]
    agents = [Agent(name="main", model=MODEL, extensions=[Subtasks(roster)]),
              Agent(name="worker", model=MODEL, tools=[Echo()],
                    extensions=[Subtasks(progress_labels=False)])]
    provider = ScriptedProvider([
        reply("", ("Agent", {"description": "d", "prompt": "sub job"})),
        reply("launched"),
        reply("", ("SubagentHandback", {"message": "the report"})),
        reply("final from the report")])
    monkeypatch.setattr(cli, "_app",
                        lambda s, cwd=None, **kw: local(tmp_path, provider, *agents))
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "go", "--model", "m", "--cwd", str(tmp_path)])
    assert done.value.code == 0
    assert capsys.readouterr().out.strip() == "final from the report"
