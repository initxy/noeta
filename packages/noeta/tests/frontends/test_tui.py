"""The TUI driven headless: type a prompt, see the tool call and the answer,
approve with a key, stop with Esc."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from noeta.agent import Agent  # noqa: E402
from noeta.ext.base import Ask, Extension, TaskView  # noqa: E402
from noeta.local import LocalApp  # noqa: E402
from noeta.store.sqlite import SqliteStore  # noqa: E402
from noeta.testing import ScriptedProvider, reply  # noqa: E402
from noeta.tools.base import ToolContext, ToolOutput, text_output  # noqa: E402
from noeta.tui.app import NoetaApp  # noqa: E402
from noeta.tui.widgets import (  # noqa: E402
    ApprovalCard,
    AssistantMessage,
    ToolCall,
    UserMessage,
)
from noeta.types import ToolUse  # noqa: E402

SHOTS = Path(__file__).parent / "_shots"


class Look:
    name = "Read"
    description = "Read."
    input_schema = {"type": "object"}  # noqa: RUF012
    read_only = True
    clearable = True

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("1\tdef add(a, b):\n2\t    return a - b")


class AskReads(Extension):
    name = "ask"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="read a file?")


def make_app(tmp_path: Path, provider: ScriptedProvider, *exts: Extension) -> NoetaApp:
    agent = Agent(name="main", model="claude-sonnet-4-5", tools=[Look()], extensions=list(exts))
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None)
    return NoetaApp(local, agent="main", model="claude-sonnet-4-5", cwd=str(tmp_path))


async def settle(pilot: Any, app: NoetaApp, cond: Any, tries: int = 100) -> None:
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return
    raise AssertionError("UI did not settle")


async def test_prompt_tool_and_answer(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("Let me look.", ("Read", {"file_path": "calc.py"})),
                                 reply("`add` subtracts; it should add.")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"what is wrong?")
        await pilot.press("enter")
        await settle(pilot, app, lambda: len(app.query(AssistantMessage)) >= 2 and not app.busy)
        assert [w.render().plain for w in app.query(UserMessage)] == ["what is wrong?"]
        [call] = list(app.query(ToolCall))
        assert call.state == "done" and "calc.py" in call.title
        SHOTS.mkdir(exist_ok=True)
        app.save_screenshot(str(SHOTS / "answer.svg"))


async def test_approval_with_a_key(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Read", {"file_path": "calc.py"})), reply("ok")])
    app = make_app(tmp_path, provider, AskReads())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"read it", "enter")
        await settle(pilot, app, lambda: len(app.query(ApprovalCard)) == 1)
        SHOTS.mkdir(exist_ok=True)
        app.save_screenshot(str(SHOTS / "approval.svg"))
        await pilot.press("y")
        await settle(pilot, app, lambda: not app.busy and len(app.query(AssistantMessage)) == 1)
        [call] = list(app.query(ToolCall))
        assert call.state == "done"
