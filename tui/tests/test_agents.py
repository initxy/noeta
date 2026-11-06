"""Sub-agents as Claude Code shows them: the list under the footer while they
run, ↓ from an empty prompt (or a click) to pick one, and its view — what
it did — in the transcript's place, Esc back."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.types import ModelRequest, ModelResponse
from noeta.host.app import FrontEndStart
from noeta.host.local import LocalApp
from noeta.plugins.fs import FileTools
from noeta.plugins.subtasks import SubagentSpec, Subtasks
from noeta.testing import reply
from noeta_tui.agents import AgentsPanel, AgentView
from noeta_tui.app import NoetaApp

from support import MODEL, plain, ready, settle, shown, type_line

CHILD = "Look around the repo"


class Routed:
    """The main task's replies in order; a sub-agent's (its prompt is
    :data:`CHILD`) wait for ``release``."""

    family = "scripted"

    def __init__(self, main: list[ModelResponse], child: list[ModelResponse]) -> None:
        self.main, self.child = main, child
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        if any(CHILD in m.text for m in request.messages):
            if len(self.child) == 1:  # its last reply waits: it is still running
                await self.release.wait()
            return self.child.pop(0)
        return self.main.pop(0)


def listed(panel: AgentsPanel) -> str:
    """The agents' list as text, a line per row."""
    return "\n".join(f"{r.name} {r.extra} {r.activity} {r.stats}" for r in panel.rows)


def make_app(tmp_path: Path, provider: Routed) -> NoetaApp:
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    (work / "a.py").write_text("x = 1\n")
    roster = [SubagentSpec(name="Explore", description="Looks around.", agent="worker")]
    main = Agent(name="main", model=MODEL, extensions=[FileTools(), Subtasks(roster)])
    worker = Agent(name="worker", model=MODEL, extensions=[FileTools()])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [main, worker], provider,
                     env_for=lambda _s: LocalExecEnv(str(work)), own_tasks_only=True)
    front = FrontEndStart(agent="main", cwd=str(work), model=MODEL, version="1.2.3",
                          home=str(tmp_path / "home"), settings=lambda: [])
    return NoetaApp(local, agent="main", model=MODEL, cwd=str(work), start=front)


def script(tmp_path: Path) -> Routed:
    agent = {"description": "explore the repo", "subagent_type": "Explore", "prompt": CHILD}
    return Routed(
        main=[reply("", ("Agent", agent)), reply("It is looking."), reply("All done.")],
        child=[reply("Reading the code first.",
                     ("Read", {"file_path": str(tmp_path / "w" / "a.py")})),
               reply("Found one file: a.py")])


async def test_a_running_agent_is_listed_and_its_view_shows_what_it_did(
        tmp_path: Path) -> None:
    provider = script(tmp_path)
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 34)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "explore it")
        panel = app.query_one(AgentsPanel)
        await settle(pilot, lambda: panel.display and "explore the repo" in listed(panel))
        await settle(pilot, lambda: "Read(" in listed(panel))
        assert "main" in listed(panel) and "↓ to view" in plain(app.query_one("#hints"))

        await pilot.press("down")  # from the empty prompt to the list
        assert panel.has_focus and panel.rows[panel.index].key != "main"
        await pilot.press("enter")
        await settle(pilot, lambda: bool(app.query(AgentView)))
        view = app.query_one(AgentView)
        assert not app.query_one("#transcript").display
        await settle(pilot, lambda: "Read" in shown(app) and "Reading the code first."
                     in shown(app))
        assert "explore the repo" in plain(view.query_one(".agent-head"))

        provider.release.set()  # the agent answers and ends
        await settle(pilot, lambda: "Found one file: a.py" in shown(app))
        await settle(pilot, lambda: "Done" in plain(view.query_one(".agent-head")))

        await pilot.press("escape")
        await settle(pilot, lambda: not app.query(AgentView))
        assert app.query_one("#transcript").display
        assert app.focused is app.query_one("#prompt")
        # Ended in this turn: still listed, to look at, until the next message.
        assert "Done" in listed(panel)


async def test_a_click_opens_an_agent_and_main_goes_back(tmp_path: Path) -> None:
    provider = script(tmp_path)
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 34)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "explore it")
        panel = app.query_one(AgentsPanel)
        await settle(pilot, lambda: panel.display and len(panel.rows) == 2)
        await pilot.click(AgentsPanel, offset=(4, 1))  # the agent's line
        await settle(pilot, lambda: bool(app.query(AgentView)))
        await pilot.click(AgentsPanel, offset=(4, 0))  # main
        await settle(pilot, lambda: not app.query(AgentView))
        provider.release.set()
        await settle(pilot, lambda: "All done." in shown(app) or "It is looking." in shown(app))


async def test_a_new_message_drops_the_agents_that_ended(tmp_path: Path) -> None:
    provider = script(tmp_path)
    provider.main.append(reply("ok"))
    provider.release.set()
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 34)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "explore it")
        panel = app.query_one(AgentsPanel)
        await settle(pilot, lambda: "Done" in listed(panel))
        await settle(pilot, lambda: not app.busy)
        await type_line(pilot, "thanks")
        await settle(pilot, lambda: not panel.display)
