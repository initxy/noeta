"""The screens ``screenshots/README.md`` compares with Claude Code's, saved as
SVG: into ``tui/screenshots`` with ``NOETA_TUI_SCREENSHOTS=1`` (``make
tui-screenshots``), else into the test's temporary directory."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from noeta.plugins.permissions import Permissions
from noeta.plugins.todos import Todos
from noeta.testing import ScriptedProvider, reply
from noeta_tui.app import NoetaApp
from noeta_tui.widgets import ApprovalCard, ToolCall

from support import AskBash, Lines, make_app, settle, shown, type_line

SIZE = (100, 34)


@pytest.fixture
def shots(tmp_path: Path) -> Path:
    if os.environ.get("NOETA_TUI_SCREENSHOTS"):
        return Path(__file__).parents[1] / "screenshots"
    return tmp_path / "shots"


def save(app: NoetaApp, shots: Path, name: str) -> None:
    shots.mkdir(exist_ok=True)
    app.save_screenshot(name, path=str(shots))
    assert (shots / name).stat().st_size > 1000


async def test_welcome(tmp_path: Path, shots: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]), Permissions(mode="acceptEdits"))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.mode == "acceptEdits")
        await pilot.press("question_mark")
        await pilot.pause(0.2)
        save(app, shots, "welcome.svg")


async def test_transcript(tmp_path: Path, shots: Path) -> None:
    target = tmp_path / "w" / "calc.py"
    provider = ScriptedProvider([
        reply("Let me run the tests first.", ("Bash", {"command": "pytest -q", "n": 12})),
        reply("", ("Read", {"file_path": str(target)})),
        reply("", ("Edit", {"file_path": str(target), "old_string": "a - b",
                            "new_string": "a + b"})),
        reply("Fixed: `add` subtracted instead of adding. The tests pass now.")])
    app = make_app(tmp_path, provider, tools=[Lines()])
    target.write_text("def add(a, b):\n    return a - b\n")
    async with app.run_test(size=SIZE) as pilot:
        await type_line(pilot, "the add test fails, fix it")
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        await pilot.pause(0.3)
        save(app, shots, "transcript.svg")


async def test_working_with_a_task_list(tmp_path: Path, shots: Path) -> None:
    lines = Lines()
    lines.release.clear()
    provider = ScriptedProvider([
        reply("", ("TaskCreate", {"subject": "Reproduce the failure", "description": "d"}),
              ("TaskCreate", {"subject": "Fix add()", "description": "d",
                              "activeForm": "Fixing add()"}),
              ("TaskCreate", {"subject": "Run the suite", "description": "d"})),
        reply("", ("TaskUpdate", {"taskId": "1", "status": "completed"}),
              ("TaskUpdate", {"taskId": "2", "status": "in_progress"})),
        reply("", ("Bash", {"command": "pytest -q tests/test_calc.py", "n": 3})),
        reply("done")])
    app = make_app(tmp_path, provider, Todos(all_models=True), tools=[lines])
    async with app.run_test(size=SIZE) as pilot:
        await type_line(pilot, "fix the add test")
        await settle(pilot, lambda: any(c.state == "running" for c in app.query(ToolCall)))
        await type_line(pilot, "also check sub()")
        await pilot.pause(0.5)
        save(app, shots, "working.svg")
        lines.release.set()
        await settle(pilot, lambda: not app.busy)


async def test_approval(tmp_path: Path, shots: Path) -> None:
    provider = ScriptedProvider([reply("", ("Bash", {"command": "rm -rf build/", "n": 1})),
                                 reply("ok")])
    app = make_app(tmp_path, provider, AskBash(), tools=[Lines()])
    async with app.run_test(size=SIZE) as pilot:
        await type_line(pilot, "clean the build")
        await settle(pilot, lambda: len(app.query(ApprovalCard)) == 1)
        await pilot.pause(0.2)
        save(app, shots, "approval.svg")


async def test_slash_menu_and_context(tmp_path: Path, shots: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([reply("hi")]))
    async with app.run_test(size=SIZE) as pilot:
        await type_line(pilot, "hello")
        await settle(pilot, lambda: not app.busy and bool(app.plugin_commands))
        await type_line(pilot, "/context")
        await settle(pilot, lambda: "Context Usage" in shown(app))
        await pilot.pause(0.2)
        save(app, shots, "context.svg")
        await pilot.press("/", "c")
        await pilot.pause(0.2)
        save(app, shots, "slash-menu.svg")


