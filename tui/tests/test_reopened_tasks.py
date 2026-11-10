"""A reopened task's history is rendered first and mounted in one go (one
by one, Textual lays the screen out again for each record: seconds for a
long task), looks as it did live, and what the task records next follows."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static
from textual.widgets.markdown import MarkdownBlock

from noeta.testing import ScriptedProvider, reply
from noeta_tui.widgets import AssistantMessage

from support import Lines, make_app, plain, settle, type_line


def transcript_texts(app: Any) -> list[tuple[str, list[tuple[str, bool]]]]:
    """Each widget of the transcript after the welcome box: its type and
    what it and its parts show (and whether each is on the screen)."""
    transcript = app.query_one("#transcript")

    def seen(node: Any) -> bool:
        return node is transcript or (node.display and seen(node.parent))

    out = []
    for widget in transcript.children[1:]:
        nodes = [widget, *widget.query("*")]
        parts = [(m.source, seen(m)) for m in nodes if isinstance(m, Markdown)]
        parts += [(re.sub(r"Worked for \S+", "Worked for …", plain(s)), seen(s))
                  for s in nodes if isinstance(s, Static) and not isinstance(s, MarkdownBlock)]
        out.append((type(widget).__name__, parts))
    return out


async def test_a_reopened_task_is_shown_at_once_as_it_was_live(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "w"
    work.mkdir()
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    provider = ScriptedProvider([
        reply("Looking.", ("Read", {"file_path": str(work / "calc.py")}),
              ("Bash", {"command": "pytest", "n": 4})),
        reply("", ("Edit", {"file_path": str(work / "calc.py"), "old_string": "a - b",
                            "new_string": "a + b"})),
        reply("Fixed."),
        reply("Bye.")])
    live = make_app(tmp_path, provider, tools=[Lines()])
    async with live.run_test(size=(120, 50)) as pilot:
        await type_line(pilot, "fix it")
        await settle(pilot, lambda: len(provider.steps) == 1 and not live.busy)
        await type_line(pilot, "thanks")
        await settle(pilot, lambda: not provider.steps and not live.busy)
        task_id, shown_live = live.task_id, transcript_texts(live)

    mounts: list[int] = []
    mount = VerticalScroll.mount

    def counted(self: VerticalScroll, *widgets: Any, **kw: Any) -> Any:
        if self.id == "transcript":
            mounts.append(len(widgets))
        return mount(self, *widgets, **kw)

    monkeypatch.setattr(VerticalScroll, "mount", counted)
    provider = ScriptedProvider([reply("Again.")])
    again = make_app(tmp_path, provider, tools=[Lines()], resume=task_id)
    async with again.run_test(size=(120, 50)) as pilot:
        await settle(pilot, lambda: len(again.query(AssistantMessage)) == 3)
        transcript = again.query_one("#transcript")
        # The welcome box and the whole history in one mount.
        assert mounts == [len(transcript.children)]
        assert transcript_texts(again) == shown_live
        await type_line(pilot, "once more")
        await settle(pilot, lambda: not provider.steps and not again.busy
                     and len(again.query(AssistantMessage)) == 4)
        last = [w for w in transcript.children if w.query(AssistantMessage)][-1]
        assert last.query_one(AssistantMessage).source == "Again."
        assert len(mounts) > 2  # what came next was mounted as it came


async def test_a_reopened_turn_says_how_long_it_worked_from_its_log(tmp_path: Path) -> None:
    import sqlite3

    provider = ScriptedProvider([reply("Done.")])
    live = make_app(tmp_path, provider)
    async with live.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "do it")
        await settle(pilot, lambda: not provider.steps and not live.busy)
        task_id = live.task_id
    with sqlite3.connect(tmp_path / "t.db") as db:  # the turn took 3m 5s
        db.execute("UPDATE events SET at = at + 185 WHERE task_id = ? AND seq > (SELECT min(seq)"
                   " FROM events WHERE task_id = ? AND type = 'InputTaken')", (task_id, task_id))
    again = make_app(tmp_path, ScriptedProvider([]), resume=task_id)
    async with again.run_test(size=(120, 40)) as pilot:
        await settle(pilot, lambda: again.query(".worked"))
        assert plain(again.query_one(".worked")) == "✻ Worked for 3m 5s"


async def test_a_reply_streamed_while_the_log_is_behind_comes_after_the_calls_before_it(
        tmp_path: Path) -> None:
    from noeta.core.events import ModelReplied
    from noeta_tui.widgets import ToolGroup

    provider = ScriptedProvider([
        reply("Looking.", ("Read", {"file_path": "a.txt"}), ("Bash", {"command": "ls", "n": 2})),
        reply("Fixed.")])
    app = make_app(tmp_path, provider, tools=[Lines()])
    (tmp_path / "w" / "a.txt").write_text("a\n")
    render = app.render_event
    slowed = False

    async def behind(rec: Any) -> None:  # the follower lags while the next reply streams
        nonlocal slowed
        if isinstance(rec.event, ModelReplied) and not slowed:
            slowed = True
            await asyncio.sleep(0.5)
        await render(rec)

    app.render_event = behind  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await type_line(pilot, "fix it")
        await settle(pilot, lambda: not provider.steps and not app.busy
                     and len(app.query(AssistantMessage)) == 2)
        kinds = [type(w).__name__ for w in app.query_one("#transcript").children[1:]]
        assert kinds[:3] == ["UserMessage", "Horizontal", "ToolGroup"], kinds
        assert len(app.query_one(ToolGroup).calls) == 2
        assert kinds.index("ToolGroup") < max(i for i, k in enumerate(kinds) if k == "Horizontal")
