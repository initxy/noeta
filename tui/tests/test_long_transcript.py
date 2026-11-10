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

from support import Lines, make_app, settle, shown, type_line


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


async def test_page_keys_scroll_the_transcript_while_the_prompt_has_the_keys(
        tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("\n\n".join(f"Paragraph {i}." for i in range(60)))])
    app = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "a")
        await settle(pilot, lambda: not provider.steps and not app.busy)
        await pilot.pause(0.2)
        transcript = app.query_one("#transcript", tail.TailScroll)
        assert app.query_one("#prompt").has_focus
        end = transcript.scroll_y
        await pilot.press("pageup")
        await pilot.pause(0.1)
        assert transcript.scroll_y < end and app.query_one("#prompt").has_focus
        await pilot.press("pagedown")
        await pilot.pause(0.1)
        assert transcript.scroll_y == end


async def test_esc_shows_the_turn_stopped_at_once_and_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    lines = Lines()
    lines.release.clear()
    provider = ScriptedProvider([reply("", ("Bash", {"command": "x", "n": 1})), reply("late")])
    app = make_app(tmp_path, provider, tools=[lines])
    async with app.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "go")
        await settle(pilot, lambda: app.busy and bool(app.calls))
        cancel = app.local.client.cancel
        sent = asyncio.Event()

        async def slow(*a: Any, **kw: Any) -> Any:
            await sent.wait()  # the task hears of it a while later
            return await cancel(*a, **kw)

        monkeypatch.setattr(app.local.client, "cancel", slow)
        await pilot.press("escape")
        await settle(pilot, lambda: not app.busy and "Interrupted" in shown(app))
        assert not app.query_one("#spinner").display
        sent.set()
        lines.release.set()
        await settle(pilot, lambda: app.local is not None and not app.busy
                     and app._stopped_here is False)
        await pilot.pause(0.3)
        assert shown(app).count("Interrupted") == 1


async def test_a_streamed_reply_is_appended_once_a_frame_not_once_a_fragment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta_tui.widgets import ReplyStream

    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: bool(app.query("#prompt")))
        widget = AssistantMessage("")
        await app.transcript().mount(widget)
        appends: list[str] = []
        append = widget.append

        def counted(text: str) -> Any:
            appends.append(text)
            return append(text)

        monkeypatch.setattr(widget, "append", counted)
        stream = ReplyStream(widget)
        for n in range(200):
            stream.write(f"w{n} ")
        await stream.stop()
        assert widget.source == "".join(f"w{n} " for n in range(200))
        assert len(appends) <= 2
