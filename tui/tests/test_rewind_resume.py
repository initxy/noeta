"""A rewound conversation is one conversation: the screen (and a resume of
it later) shows what came before the point it went back to, and its task
is made only when something is sent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.events import TaskCreated
from noeta.testing import ScriptedProvider, reply
from noeta_tui.widgets import AssistantMessage, UserMessage

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
