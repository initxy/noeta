"""The footer shows the effort whenever the task has one, on a model outside
the catalog too, and ``/status`` and the ``/effort`` picker say when none is
sent; the directory left the footer: the project_status plugin shows the
project and git branch on the line under it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from noeta.core import models
from noeta.plugins.project_status import ProjectStatus
from noeta.testing import ScriptedProvider, reply
from noeta_tui import plugin_ui
from noeta_tui.widgets import Picker

from support import MODEL, make_app, plain, ready, settle, shown, type_line

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
#: Where the tests save what they show (``NOETA_TUI_SHOTS``), to look at.
SHOTS = os.environ.get("NOETA_TUI_SHOTS")


def relay_model() -> str:
    """A model as a ``config.toml`` provider row registers it: its limits,
    no ``efforts`` (so not a reasoning model)."""
    name = f"relay-{uuid.uuid4().hex[:8]}"
    models.register_models({name: models.ModelSpec(
        real_model_id=name, context_window=200_000, max_output_tokens=32_000)})
    return name


async def test_an_effort_set_on_a_model_outside_the_catalog_shows_and_is_sent(
        tmp_path: Path) -> None:
    model = relay_model()
    provider = ScriptedProvider([reply("one"), reply("two")])
    app = make_app(tmp_path, provider, settings=lambda: [("effort", "(not in the settings)")])
    app.model, app.task_config["model"] = model, model
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        status = app.query_one("#status")
        assert f"{model}  ·" in plain(status)  # none set: none shown, none sent
        await type_line(pilot, "/status")
        await settle(pilot, lambda: "none sent, the model's own default applies" in shown(app))
        assert "(not in the settings)" not in shown(app)  # the task's row, not the settings'
        await type_line(pilot, "/effort")
        await settle(pilot, lambda: bool(app.query(Picker)))
        assert "Select effort (now none sent, the model's own default applies)" in shown(app)
        await pilot.press("3")
        await settle(pilot, lambda: f"{model} ● high" in plain(status))
        await type_line(pilot, "go")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 1)
        assert f"{model} ● high" in plain(status)  # as the task recorded it
        await type_line(pilot, "/effort max")  # the config signal, mid-task
        await type_line(pilot, "again")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 2)
        assert [(r.model, r.effort) for r in provider.requests] == [(model, "high"),
                                                                    (model, "max")]
        assert f"{model} ◈ max" in plain(status)
        await type_line(pilot, "/status")
        await settle(pilot, lambda: re.search(r"effort +max\n", shown(app)) is not None)


async def test_a_catalogued_reasoning_model_shows_its_default(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        assert f"{MODEL} ◐ medium" in plain(app.query_one("#status"))
        await type_line(pilot, "/effort")
        await settle(pilot, lambda: bool(app.query(Picker)))
        assert "Select effort (now medium, the model's default)" in shown(app)


def _checkout(work: Path) -> None:
    """``work`` a git checkout on ``tui/polish`` with a change."""
    git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "tui/polish", str(work)], check=True)
    (work / "a.txt").write_text("a\n")
    subprocess.run([*git, "add", "a.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "first"], check=True)
    (work / "a.txt").write_text("b\n")


@needs_git
async def test_the_project_and_branch_show_under_the_footer_not_the_directory(
        tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    _checkout(work)
    provider = ScriptedProvider([reply("hi"), reply("again")])
    app = make_app(tmp_path, provider, ProjectStatus())
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: ready(app))
        footer = plain(app.query_one("#status"))
        assert "/w" not in footer and "…" not in footer
        line = app.query_one("#plugin-status")
        assert not line.display  # no task yet: nothing to ask
        await type_line(pilot, "hello")
        await settle(pilot, lambda: line.display and plain(line) == "w  ·  ⎇ tui/polish*")
        await settle(pilot, lambda: not app.busy)
        assert "/w" not in plain(app.query_one("#status"))
        if SHOTS:
            app.save_screenshot("project-status.svg", path=SHOTS)
        await plugin_ui.switch_task(app, "project_status", on=False)
        await type_line(pilot, "again")
        await settle(pilot, lambda: not line.display and not app.busy)
