"""A rewound conversation is one conversation: the screen (and a resume of
it later) shows what came before the point it went back to, its task is
made only when something is sent, and the resume list shows it once, in
place of what it continues. ``--resume`` lists before it opens anything,
and lists this directory's conversations first."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.events import TaskCreated
from noeta.testing import ScriptedProvider, reply
from noeta_tui.widgets import AssistantMessage, Choices, Picker, UserMessage

from support import make_app, settle, shown, type_line


def said(app: Any) -> list[str]:
    return [m.text for m in app.query(UserMessage)]


async def roots(app: Any) -> list[Any]:
    return list(await app.local.client.tasks(roots_only=True, limit=100))


async def test_a_rewind_shows_the_conversation_before_its_point_and_makes_its_task_late(
        tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("one"), reply("two"), reply("three"), reply("again")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 40)) as pilot:
        for line in ("first", "second", "third"):
            await type_line(pilot, line)
            await settle(pilot, lambda line=line: not app.busy and bool(app.task_id)
                         and line in said(app) and len(app.query(AssistantMessage)) >= 1)
        await settle(pilot, lambda: "three" in shown(app))
        original = app.task_id
        await type_line(pilot, "/rewind 3 conversation")
        await settle(pilot, lambda: "rewound to before prompt 3" in shown(app))
        # Before the point, as it was; the prompt rewound to, to edit.
        assert said(app) == ["first", "second"] and "two" in shown(app)
        assert "three" not in shown(app)
        assert app.query_one("#prompt").text == "third"
        # Nothing made yet: a rewind nobody goes on with leaves no task.
        assert app.task_id == "" and [r.task_id for r in await roots(app)] == [original]
        await type_line(pilot, "third, again")
        await settle(pilot, lambda: bool(app.task_id) and not provider.steps and not app.busy)
        fork = app.task_id
        created = (await app.local.client.events(fork))[0].event
        assert isinstance(created, TaskCreated) and created.history_from == original
        assert said(app) == ["first", "second", "third, again"]

    # Resumed later, it shows the whole conversation, not only its own part.
    again = make_app(tmp_path, ScriptedProvider([]), resume=fork)
    async with again.run_test(size=(100, 40)) as pilot:
        await settle(pilot, lambda: said(again) == ["first", "second", "third, again"])
        assert "again" in shown(again) and "three" not in shown(again)


async def test_the_resume_list_shows_a_rewound_conversation_once(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("one"), reply("two"), reply("again")])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 40)) as pilot:
        await type_line(pilot, "first")
        await settle(pilot, lambda: not app.busy and "one" in shown(app))
        await type_line(pilot, "second")
        await settle(pilot, lambda: not app.busy and "two" in shown(app))
        original = app.task_id
        await type_line(pilot, "/rewind 2 conversation")
        await settle(pilot, lambda: app.fork_from is not None)
        await type_line(pilot, "second, again")
        await settle(pilot, lambda: bool(app.task_id) and not provider.steps and not app.busy)
        fork = app.task_id
        await type_line(pilot, "/resume")
        await settle(pilot, lambda: bool(app.query(Picker)))
        options = app.query_one(Choices).options
        assert [o.id for o in options] == [fork]
        assert options[0].label == "first" and f"rewound from {original[:8]}" in options[0].detail


async def test_resume_with_no_task_lists_before_opening_one(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("one"), reply("two")])
    first = make_app(tmp_path, provider)
    async with first.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "older")
        await settle(pilot, lambda: not first.busy and "one" in shown(first))
        await type_line(pilot, "/new")
        await settle(pilot, lambda: not first.task_id)
        await type_line(pilot, "newer")
        await settle(pilot, lambda: not first.busy and "two" in shown(first))
        before = {r.task_id: r.updated_at for r in await roots(first)}

    picking = make_app(tmp_path, ScriptedProvider([]), resume="")
    async with picking.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: bool(picking.query(Picker)))
        # Nothing opened, nothing told it was resumed: the list is as it was.
        assert picking.task_id == "" and not said(picking)
        assert {r.task_id: r.updated_at for r in await roots(picking)} == before
        labels = [o.label for o in picking.query_one(Choices).options]
        assert labels == ["newer", "older"]
        await pilot.press("down", "enter")
        await settle(pilot, lambda: said(picking) == ["older"])


async def test_the_resume_list_is_this_directorys_with_a_way_to_every_one(
        tmp_path: Path) -> None:
    here = make_app(tmp_path, ScriptedProvider([reply("ok")]))
    async with here.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "made here")
        await settle(pilot, lambda: not here.busy and "ok" in shown(here))
        elsewhere = str(tmp_path / "elsewhere")
        await here.local.client.create("main", None, config={"cwd": elsewhere})
        await type_line(pilot, "/resume")
        await settle(pilot, lambda: bool(here.query(Picker)))
        options = here.query_one(Choices).options
        assert [o.label for o in options] == [
            "made here", "Show conversations from every directory (1 more)"]
        await pilot.press("down", "enter")
        await settle(pilot, lambda: len(here.query_one(Choices).options) == 2
                     and "every directory" not in here.query_one(Choices).options[-1].label)
        assert any(elsewhere in o.detail for o in here.query_one(Choices).options)
