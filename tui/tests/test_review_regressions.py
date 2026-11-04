"""Regressions from the front-end reviews, in the terminal UI: questions
(the real AskUserQuestion's, a plugin's, a sub-agent's) answered by typing,
a stop withdraws the pending question, a retry drops the failed attempt's
output, sub-agents' approvals reach the TUI, the footer's cost counts the
whole tree, a task whose agent is missing opens read-only, and plugins'
commands run through the app."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.errors import Transient
from noeta.core.events import Answer, Say, ToolFinished
from noeta.core.extension import Ask, Command, CommandContext, Extension, TaskView
from noeta.core.questions import ask, read_answer
from noeta.core.tool import Park, Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import Delta, ModelRequest, ModelResponse, ToolUse
from noeta.host.local import LocalApp, UnknownAgent
from noeta.plugins.ask_user import AskUser
from noeta.testing import ScriptedProvider, reply
from noeta_tui.app import NoetaApp
from noeta_tui.widgets import ApprovalCard, AssistantMessage, Notice, QuestionCard, UserMessage

from support import settle, shown

MODEL = "claude-sonnet-4-5"
DB_QUESTION = {"questions": [{
    "question": "Which database?", "header": "DB", "multiSelect": False,
    "options": [{"label": "SQLite", "description": "a file"},
                {"label": "Postgres", "description": "a server"}]}]}


async def _no_sleep(_: float) -> None:
    return None


def local(tmp_path: Path, provider: Any, *agents: Agent, **kw: Any) -> LocalApp:
    return LocalApp(SqliteStore(str(tmp_path / "t.db")), list(agents), provider,
                    env_for=lambda _s: None, sleep=_no_sleep, **kw)


def tui(tmp_path: Path, provider: Any, *tools: Any) -> NoetaApp:
    agent = Agent(name="main", model=MODEL, tools=list(tools))
    return NoetaApp(local(tmp_path, provider, agent), agent="main", model=MODEL,
                    cwd=str(tmp_path))


def on(app: LocalApp) -> NoetaApp:
    return NoetaApp(app, agent="main", model=MODEL, cwd="/tmp")


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


WHERE = ask(
    {"question": "Which environment?",
     "options": [{"label": "staging"}, {"label": "prod", "description": "careful"}]},
    {"question": "Any note for the changelog?"})


class Deploy:
    name = "Deploy"
    description = "Deploy the service."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ctx.notify("deploy: asking where to")
        return ToolOutput(park=WHERE)

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        answers = read_answer(outcome, WHERE)
        if answers is None:
            return text_output("not deployed", is_error=True)
        where = answers["0"]["selected"] or [answers["0"]["other"]]
        return text_output(f"deployed to {where[0]}: {answers['1']['other']}")


class Deployer(Extension):
    name = "deployer"

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return [Deploy()]


class Greeter(Extension):
    name = "greeter"

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return [Command(name="greet", description="Say hello", template="Say hello to {args}."),
                Command(name="shout", description="Shout", argument_hint="<word>")]

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        if args == "boom":
            raise RuntimeError("no voice")
        return f"{args.upper()}! (in {ctx.cwd})"


class Broken(Extension):
    name = "broken"

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        raise RuntimeError("down")


class AskTool:
    name = "Ask"
    description = "Ask the user."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return ToolOutput(park=Park(on="question", detail={"questions": [
            {"question": "Which database?", "options": [{"label": "sqlite"}]}]}))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        if outcome is None:
            return text_output("withdrawn", is_error=True)
        return text_output(f"user said: {outcome.data}")


class RetryOnce:
    """The first attempt streams a fragment and fails; the retry succeeds."""

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


async def test_tui_question_card_and_stop_clear_the_pending_question(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Ask", {})), reply("fresh start")])
    app = tui(tmp_path, provider, AskTool())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"set it up", "enter")
        await settle(pilot, lambda: bool(app.waiting_question))
        [card] = list(app.query(QuestionCard))
        assert card.detail["questions"][0]["question"] == "Which database?"
        await pilot.press("escape")
        await settle(pilot, lambda: not app.waiting_question and not app.busy)
        await pilot.press(*"something else", "enter")
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        inbox = await app.local.store.inbox(app.task_id)
        assert not any(isinstance(i.body, Answer) for i in inbox)
        assert isinstance(inbox[-1].body, Say)
        # Re-opening replays the log; the withdrawn question stays withdrawn.
        await app.open_task(app.task_id)
        await settle(pilot, lambda: len(app.query(QuestionCard)) == 1)
        await pilot.pause(0.3)
        assert app.waiting_question == ""


async def test_tui_drops_a_failed_attempts_output(tmp_path: Path) -> None:
    app = tui(tmp_path, RetryOnce())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: not app.busy and len(app.query(AssistantMessage)) >= 1)
        await pilot.pause(0.2)
        assert [w.source for w in app.query(AssistantMessage)] == ["good answer"]


async def test_tui_survives_a_stream_stopped_before_its_task_ran(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a loaded loop a live-output stream can be stopped (the attempt
    failed, or the reply arrived) before Textual's stream task first ran;
    its ``stop()`` then raises the cancellation into the caller, which used
    to end the follower and the delta pump: the TUI froze for the task."""
    from textual.widgets.markdown import MarkdownStream

    run = MarkdownStream._run

    def late_start(self: MarkdownStream) -> None:
        async def late() -> None:
            await asyncio.sleep(0.3)  # the loop runs the stream's task late
            await run(self)

        if self._task is None:
            self._task = asyncio.create_task(late())

    monkeypatch.setattr(MarkdownStream, "start", late_start)
    app = tui(tmp_path, RetryOnce())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: not app.busy and len(app.query(AssistantMessage)) >= 1)
        await pilot.pause(0.5)
        assert [w.source for w in app.query(AssistantMessage)] == ["good answer"]


async def test_tui_is_not_left_busy_when_the_turn_ends_before_the_send_returns(
        tmp_path: Path) -> None:
    """Under load the task can take the message, reply and wait again before
    ``client.send`` returns; the front end must not mark itself busy after
    the turn it sent was already shown ending."""
    app = tui(tmp_path, ScriptedProvider([reply("done already")]))
    async with app.run_test(size=(100, 30)) as pilot:
        client = app.local.client
        send = client.send

        async def slow_send(task_id: str, *args: Any, **kw: Any) -> Any:
            out = await send(task_id, *args, **kw)
            for _ in range(400):  # until the follower has shown the turn end
                await asyncio.sleep(0.05)
                if app.query(AssistantMessage) and not app.busy:
                    break
            return out

        client.send = slow_send  # type: ignore[method-assign]
        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: len(app.query(AssistantMessage)) >= 1)
        await pilot.pause(0.3)
        assert not app.busy


async def test_tui_shows_only_the_cores_retries(tmp_path: Path) -> None:
    """A plugin's record named like a core one (the hooks plugin's
    ``retry``) is not shown as the model being retried."""
    from noeta.core.extension import Extension

    class Recorder(Extension):
        name = "hooks"

        async def on_start(self, view: TaskView) -> None:
            view.record(self.name, "retry", {"call_id": "c1"})
            view.record(self.name, "notice", {"text": "not the core's"})

    agent = Agent(name="main", model="claude-sonnet-4-5", extensions=[Recorder()])
    app = NoetaApp(local(tmp_path, ScriptedProvider([reply("hi")]), agent), agent="main",
                   model="claude-sonnet-4-5", cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: not app.busy and len(app.query(AssistantMessage)) >= 1)
        await pilot.pause(0.2)
        shown = [w.render().plain for w in app.query(Notice)]
        assert not any("retrying" in s or "not the core's" in s for s in shown), shown


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


async def results(app: LocalApp, task_id: str) -> list[ToolFinished]:
    return [r.event for r in await app.client.events(task_id)
            if isinstance(r.event, ToolFinished)]


async def test_tui_answers_the_real_ask_user_with_a_label(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("AskUserQuestion", DB_QUESTION)),
                                 reply("using sqlite")])
    agent = Agent(name="main", model=MODEL, extensions=(AskUser(),))
    app = on(local(tmp_path, provider, agent))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"set it up", "enter")
        await settle(pilot, lambda: bool(app.waiting_question))
        await pilot.press(*"sqlite", "enter")
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        [done] = await results(app.local, app.task_id)
        assert not done.result.is_error
        assert done.result.text.startswith("The user answered your questions: ")
        assert '="SQLite"' in done.result.text


async def test_tui_shows_and_answers_a_sub_agents_approval(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Task", {})), reply("", ("Echo", {})),
                                 reply("child done"), reply("final")])
    app = on(local(tmp_path, provider, *main_and_worker(AskEcho())))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: len(app.query(ApprovalCard)) == 1)
        [card] = list(app.query(ApprovalCard))
        assert card.title_text == "Echo · from the worker agent"
        await card.choose("allow")
        # Two waits, so an intermittent stall says which side stopped.
        await settle(pilot, lambda: provider.steps == [])
        await settle(pilot, lambda: not app.busy)
        [child] = await app.local.client.tasks(parent_id=app.task_id)
        [echoed] = await results(app.local, child.task_id)
        assert echoed.result.text == "echoed"


async def test_tui_status_cost_counts_the_whole_tree(tmp_path: Path) -> None:
    """The status bar's cost is ``/cost``'s: sub-agents' calls count too."""
    from textual.widgets import Static

    from noeta.core.types import Usage
    from noeta.host.usage import tree_usage

    million = Usage(uncached=1_000_000, output=0)
    provider = ScriptedProvider([reply("", ("Task", {}), usage=million),
                                 reply("child done", usage=million),
                                 reply("final", usage=million)])
    priced = "claude-sonnet-4-6"
    agents = [Agent(name="main", model=priced, tools=[Delegate()]),
              Agent(name="worker", model=priced, one_shot=True, tools=[Echo()])]
    app = NoetaApp(local(tmp_path, provider, *agents), agent="main", model=priced, cwd="/tmp")
    async with app.run_test(size=(140, 30)) as pilot:
        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        total = await tree_usage(app.local.client, app.task_id)
        assert total.tasks == 2
        want = f"${total.cost_usd:.2f}"
        await settle(pilot, lambda: want in str(app.query_one("#status", Static).content))


async def test_tui_shows_and_answers_a_sub_agents_question(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Task", {})),
                                 reply("", ("AskUserQuestion", DB_QUESTION)),
                                 reply("child done"), reply("final")])
    app = on(local(tmp_path, provider, *main_and_worker(AskUser())))
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


async def test_the_tui_asks_for_a_plugin(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Deploy", {})), reply("done")])
    agent = Agent(name="main", model="m", extensions=(Deployer(),))
    app = NoetaApp(local(tmp_path, provider, agent), agent="main", model="m", cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"ship it", "enter")
        await settle(pilot, lambda: bool(app.waiting_question))
        assert "deploy: asking where to" in shown(app)
        await pilot.press(*"2", "enter")  # an option by its number
        await pilot.press(*"first release", "enter")
        await settle(pilot, lambda: provider.steps == [])
        await settle(pilot, lambda: not app.busy)
        [done] = await results(app.local, app.task_id)
        assert done.result.text == "deployed to prod: first release"
        assert all("deploy: asking" not in m.text for m in provider.requests[-1].messages)


async def test_tui_help_and_plugin_commands(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("hi bob"), reply("ok")])
    agent = Agent(name="main", model="m", extensions=[Broken(), Greeter()])
    app = NoetaApp(local(tmp_path, provider, agent), agent="main", model="m", cwd=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: bool(app.task_id and app.plugin_commands))
        await app.submit("/help")
        await pilot.pause(0.1)
        assert "From plugins" in shown(app) and "/shout" in shown(app)
        assert "Shout <word>" in shown(app)
        await app.submit("/greet bob")
        await settle(pilot, lambda: bool(provider.requests))
        assert provider.requests[0].messages[-1].text.endswith("Say hello to bob.")
        await app.submit("/nope")
        await app.submit("/shout boom")
        await pilot.pause(0.2)
        assert "unknown command /nope" in shown(app)
        assert "/shout failed: no voice" in shown(app)
