"""Regressions from the re-review (front ends): a typed answer to the real
AskUserQuestion is accepted, sub-agents' approvals and questions reach the
TUI and ``noeta -p`` (which exits instead of hanging), a task whose agent
this front end lacks opens read-only, and ``-p --json`` tags each fragment
with its model call and attempt."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

import noeta.cli.main as cli
from noeta.backends.stores.sqlite import SqliteStore
from noeta.cli.tui.app import NoetaApp
from noeta.cli.tui.widgets import ApprovalCard, QuestionCard, UserMessage
from noeta.core.agent import Agent
from noeta.core.errors import Transient
from noeta.core.events import Answer, ToolFinished
from noeta.core.extension import Ask, Extension, TaskView
from noeta.core.tool import Park, ToolContext, ToolOutput, text_output
from noeta.core.types import Delta, ModelRequest, ModelResponse, ToolUse
from noeta.host.local import LocalApp, UnknownAgent
from noeta.plugins.ask_user import AskUser
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


def tui(app: LocalApp) -> NoetaApp:
    return NoetaApp(app, agent="main", model=MODEL, cwd="/tmp")


async def settle(pilot: Any, cond: Any, tries: int = 400) -> None:
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return
    raise AssertionError("UI did not settle")


async def results(app: LocalApp, task_id: str) -> list[ToolFinished]:
    return [r.event for r in await app.client.events(task_id)
            if isinstance(r.event, ToolFinished)]


# ------------------------------------------------------------ P1-1 typed answers


async def test_tui_answers_the_real_ask_user_with_a_label(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("AskUserQuestion", DB_QUESTION)),
                                 reply("using sqlite")])
    agent = Agent(name="main", model=MODEL, extensions=(AskUser(),))
    app = tui(local(tmp_path, provider, agent))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"set it up", "enter")
        await settle(pilot, lambda: bool(app.waiting_question))
        await pilot.press(*"sqlite", "enter")
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        [done] = await results(app.local, app.task_id)
        assert not done.result.is_error
        assert json.loads(done.result.text)["answers"] == {
            "0": {"selected": ["SQLite"], "other": None}}


# ------------------------------------------------------------ P1-2 sub-agent requests


async def test_print_mode_exits_when_a_sub_agent_waits_for_approval(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = ScriptedProvider([reply("", ("Task", {})), reply("", ("Echo", {}))])
    agents = main_and_worker(AskEcho())
    monkeypatch.setattr(cli, "_app", lambda s, cwd=None, **kw: local(tmp_path, provider, *agents, **kw))
    code = await asyncio.wait_for(cli._print_mode(
        cli.Settings(model="m"), "go", cwd=str(tmp_path), as_json=False,
        permission_mode=None), 10)
    assert code == 2
    err = capsys.readouterr().err
    assert "'worker'" in err and "may the worker echo?" in err


async def test_tui_shows_and_answers_a_sub_agents_approval(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Task", {})), reply("", ("Echo", {})),
                                 reply("child done"), reply("final")])
    app = tui(local(tmp_path, provider, *main_and_worker(AskEcho())))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: len(app.query(ApprovalCard)) == 1)
        [card] = list(app.query(ApprovalCard))
        assert "[worker]" in card.title_text
        await card.choose("allow")
        # Two waits, so an intermittent stall says which side stopped.
        await settle(pilot, lambda: provider.steps == [])
        await settle(pilot, lambda: not app.busy)
        [child] = await app.local.client.tasks(parent_id=app.task_id)
        [echoed] = await results(app.local, child.task_id)
        assert echoed.result.text == "echoed"


async def test_tui_shows_and_answers_a_sub_agents_question(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Task", {})),
                                 reply("", ("AskUserQuestion", DB_QUESTION)),
                                 reply("child done"), reply("final")])
    app = tui(local(tmp_path, provider, *main_and_worker(AskUser())))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: len(app.query(QuestionCard)) == 1)
        [card] = list(app.query(QuestionCard))
        assert card.who == "worker"
        await pilot.press(*"postgres", "enter")
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        [child] = await app.local.client.tasks(parent_id=app.task_id)
        [answer] = [i.body for i in await app.local.store.inbox(child.task_id)
                    if isinstance(i.body, Answer)]
        assert answer.data == {"answers": {"0": {"selected": ["Postgres"]}}}
        [done] = await results(app.local, child.task_id)
        assert not done.result.is_error


# ------------------------------------------------------------ P2-4 unknown agent


async def test_a_task_whose_agent_is_missing_opens_read_only(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.db")
    host = LocalApp(SqliteStore(path), [Agent(name="host-agent", model="m")],
                    ScriptedProvider([reply("host answer")]), env_for=lambda _s: None)
    async with host:
        theirs = await host.client.create("host-agent", "made by a host")
        assert (await host.client.wait(theirs, timeout=5)).waiting_on == "input"
    mine = LocalApp(SqliteStore(path), [Agent(name="main", model=MODEL)], ScriptedProvider([]),
                    env_for=lambda _s: None, own_tasks_only=True)
    with pytest.raises(UnknownAgent, match="host-agent"):
        await mine.adopt(theirs)
    app = NoetaApp(mine, agent="main", model=MODEL, cwd="/tmp", resume=theirs)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: app.read_only and len(app.query(UserMessage)) == 1)
        await pilot.press(*"hello", "enter")
        await pilot.pause(0.5)
    check = SqliteStore(path)
    row = await check.get(theirs)
    assert row is not None and row.status == "waiting" and row.end_status is None
    assert len(await check.inbox(theirs)) == 1  # only the host's own message
    await check.close()


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


async def test_print_json_tags_fragments_with_their_attempt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agents = [Agent(name="main", model=MODEL)]
    monkeypatch.setattr(cli, "_app", lambda s, cwd=None, **kw: local(tmp_path, RetryOnce(), *agents, **kw))
    code = await cli._print_mode(cli.Settings(model="m"), "go", cwd=str(tmp_path),
                                 as_json=True, permission_mode=None)
    assert code == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    deltas = [x for x in lines if "delta" in x]
    assert [(d["text"], d["attempt"]) for d in deltas] == [("broken par", 1), ("good answer", 2)]
    assert deltas[0]["call"] == deltas[1]["call"] > 0
    assert lines[-1]["answer"] == "good answer"


async def test_print_mode_waits_for_background_agents_and_surfaces_their_approvals(
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
                        lambda s, cwd=None, **kw: local(tmp_path, provider, *agents, **kw))
    code = await asyncio.wait_for(cli._print_mode(
        cli.Settings(model="m"), "go", cwd=str(tmp_path), as_json=False,
        permission_mode=None), 10)
    assert code == 0
    assert capsys.readouterr().out.strip() == "final from the report"
    provider = ScriptedProvider([
        reply("", ("Agent", {"description": "d", "prompt": "sub job"})),
        reply("launched"), reply("", ("Echo", {}))])
    agents[1] = Agent(name="worker", model=MODEL, tools=[Echo()],
                      extensions=[Subtasks(progress_labels=False), AskEcho()])
    (tmp_path / "t.db").unlink()
    code = await asyncio.wait_for(cli._print_mode(
        cli.Settings(model="m"), "go", cwd=str(tmp_path), as_json=False,
        permission_mode=None), 10)
    assert code == 2 and "may the worker echo?" in capsys.readouterr().err
