"""A long conversation costs what a short one does: the transcript lays out
only its last items (``tail.TailScroll``), brings older ones back as the
person scrolls up to them, and never pulls the person back down from what
they scrolled up to read."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.testing import ScriptedProvider, reply
from noeta_tui import tail
from noeta_tui.widgets import AssistantMessage

from support import Lines, make_app, settle, type_line


@pytest.fixture(autouse=True)
def small_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tail, "SHOWN", 6)
    monkeypatch.setattr(tail, "SLACK", 2)
    monkeypatch.setattr(tail, "FIRST", 4)
    monkeypatch.setattr(tail, "REVEAL", 3)


def steps(n: int) -> list[Any]:
    """``n`` replies, each said and with a call: two items each."""
    return [reply(f"Step {i}.", ("Bash", {"command": f"echo {i}", "n": 1})) for i in range(n)] \
        + [reply("All done.")]


def said(app: Any) -> list[str]:
    """The replies laid out (mounted and shown), in order."""
    return [m.source for m in app.query(AssistantMessage)
            if m.parent is not None and m.parent.display]


async def test_a_long_live_transcript_lays_out_only_its_end(tmp_path: Path) -> None:
    provider = ScriptedProvider(steps(40))
    app = make_app(tmp_path, provider, tools=[Lines()])
    async with app.run_test(size=(100, 20)) as pilot:
        await type_line(pilot, "go")
        await settle(pilot, lambda: not provider.steps and not app.busy)
        transcript = app.query_one("#transcript", tail.TailScroll)
        await pilot.pause(0.2)
        # Laid out: what fills the screen and two screens above it.
        shown = transcript.displayed_children
        assert len(shown) < len(transcript.children)
        assert sum(c.outer_size.height for c in shown) >= 2 * transcript.size.height
        assert said(app)[-1] == "All done."
        assert "Step 0." not in said(app)
        # Scrolled to the top, what was hidden comes back, oldest last.
        for _ in range(12):
            transcript.scroll_home(animate=False)
            await pilot.pause(0.05)
        await settle(pilot, lambda: not transcript.has_older)
        assert said(app)[:2] == ["Step 0.", "Step 1."]


async def test_a_reopened_long_task_mounts_its_last_items_and_the_rest_on_scroll_up(
        tmp_path: Path) -> None:
    provider = ScriptedProvider(steps(30))
    live = make_app(tmp_path, provider, tools=[Lines()])
    async with live.run_test(size=(100, 20)) as pilot:
        await type_line(pilot, "go")
        await settle(pilot, lambda: not provider.steps and not live.busy)
        task_id = live.task_id

    again = make_app(tmp_path, ScriptedProvider([]), tools=[Lines()], resume=task_id)
    async with again.run_test(size=(100, 20)) as pilot:
        await settle(pilot, lambda: bool(again.query(AssistantMessage)))
        transcript = again.query_one("#transcript", tail.TailScroll)
        assert transcript.older
        # It brings back what fills two screens above the view, no more.
        await pilot.pause(0.3)
        mounted = len(transcript.children)
        assert transcript.older
        # Scrolled up near the top, more come in above, and what was on
        # the screen stays where it was (lower by what was scrolled).
        assert transcript.scroll_y > 2 * transcript.size.height
        first = transcript.children[0]
        before = first.virtual_region.y
        transcript.scroll_to(y=2 * transcript.size.height, animate=False)
        await settle(pilot, lambda: len(transcript.children) > mounted)
        await pilot.pause(0.2)
        assert first.virtual_region.y - transcript.scroll_y == \
            before - 2 * transcript.size.height
        for _ in range(20):
            transcript.scroll_home(animate=False)
            await pilot.pause(0.05)
        await settle(pilot, lambda: not transcript.has_older)
        assert said(again)[0] == "Step 0."


async def test_scrolled_up_the_transcript_stays_put_as_the_task_goes_on(
        tmp_path: Path) -> None:
    lines = Lines()
    provider = ScriptedProvider([reply("\n\n".join(f"Paragraph {i}." for i in range(40))),
                                 reply("", ("Bash", {"command": "x", "n": 3})),
                                 reply("Done."), reply("Again.")])
    app = make_app(tmp_path, provider, tools=[lines])
    async with app.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "a")
        await settle(pilot, lambda: len(provider.steps) == 3 and not app.busy)
        transcript = app.query_one("#transcript", tail.TailScroll)
        await pilot.pause(0.2)
        assert transcript.following
        lines.release.clear()
        await type_line(pilot, "b")
        await settle(pilot, lambda: app.busy and bool(app.calls))
        await pilot.pause(0.2)
        transcript.scroll_to(y=5, animate=False)
        await pilot.pause(0.2)
        assert not transcript.following
        lines.release.set()
        await settle(pilot, lambda: not app.busy)
        await pilot.pause(0.2)
        assert transcript.scroll_y == 5
        # Back at the end, it follows again.
        transcript.scroll_end(animate=False)
        await pilot.pause(0.2)
        await type_line(pilot, "c")
        await pilot.pause(0.3)
        assert transcript.scroll_y == transcript.max_scroll_y
