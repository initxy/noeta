"""Copying from the transcript: a drag selects (tool calls and results too,
without their gutter) and is copied on release, ctrl+c copies a selection
instead of clearing or quitting, the clipboard is reached through OSC 52,
tmux and the local program, and ``/copy [N]`` copies a reply."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest
from rich.color import ColorType
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.selection import Selection
from textual.widget import Widget
from textual.widgets import Static

from noeta.testing import ScriptedProvider, reply
from noeta_tui import clipboard, render, selection
from noeta_tui.app import SHORTCUTS, NoetaApp
from noeta_tui.widgets import AssistantMessage, PromptInput

from support import Lines, make_app, plain, settle, shown, type_line

REPLY = """Here is the plan.

| name | value |
|------|-------|
| a    | 1     |

- first item
"""


class Copies:
    """What reached the clipboard: OSC 52's text, and the programs run."""

    def __init__(self) -> None:
        self.osc: list[str] = []
        self.ran: list[tuple[list[str], bytes]] = []


@pytest.fixture
def copies(monkeypatch: pytest.MonkeyPatch) -> Copies:
    """Inside tmux on a Linux desktop, with xclip installed; nothing really runs."""
    seen = Copies()
    monkeypatch.setattr(App, "copy_to_clipboard", lambda _app, text: seen.osc.append(text))

    def run(command: list[str], data: bytes) -> bool:
        seen.ran.append((command, data))
        return True

    monkeypatch.setattr(clipboard, "_run", run)
    monkeypatch.setattr(clipboard.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("tmux", "xclip") else None)
    for name in (*clipboard.SSH_VARIABLES, "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    monkeypatch.setenv("DISPLAY", ":0")
    return seen


def static_with(app: NoetaApp, text: str) -> Widget:
    """The first line of the screen (a ``Static``) that shows ``text``."""
    return next(w for w in app.query(Static) if text in plain(w))


async def drag(pilot: Any, start: Widget, start_at: tuple[int, int], end: Widget,
               end_at: tuple[int, int]) -> None:
    await pilot.mouse_down(start, offset=start_at)
    await pilot.hover(end, offset=end_at)
    await pilot.mouse_up(end, offset=end_at)
    await pilot.pause()


async def a_bash_call(pilot: Any, provider: ScriptedProvider) -> None:
    await type_line(pilot, "count")
    await settle(pilot, lambda: provider.steps == [] and not pilot.app.busy)


def bash_script() -> ScriptedProvider:
    return ScriptedProvider([reply("", ("Bash", {"command": "seq 3", "n": 3})), reply(REPLY)])


async def test_a_drag_over_a_call_copies_its_line_and_result(tmp_path: Path,
                                                             copies: Copies) -> None:
    provider = bash_script()
    app = make_app(tmp_path, provider, tools=[Lines()])
    async with app.run_test(size=(100, 40)) as pilot:
        await a_bash_call(pilot, provider)
        head, body = static_with(app, "Bash(seq 3)"), static_with(app, "line 1")
        await drag(pilot, head, (0, 0), body, (9, 1))  # from the bullet into the 2nd row
        want = "Bash(seq 3)\nline 1\nline"
        assert selection.selected_text(app.screen) == want
        assert copies.osc == [want]
        assert plain(app.query_one("#hints")) == f"Copied {len(want)} characters"
        await settle(pilot, lambda: len(copies.ran) == 2)
        assert {tuple(c) for c, _ in copies.ran} == {
            ("tmux", "load-buffer", "-w", "-"), ("xclip", "-selection", "clipboard")}
        assert all(data == want.encode() for _, data in copies.ran)


async def test_a_reply_copies_as_shown(tmp_path: Path, copies: Copies) -> None:
    provider = bash_script()
    app = make_app(tmp_path, provider, tools=[Lines()])
    async with app.run_test(size=(100, 40)) as pilot:
        await a_bash_call(pilot, provider)
        message = app.query_one(AssistantMessage)
        last = message.children[-1]
        await drag(pilot, static_with(app, "line 3"), (0, 2), last, (20, 0))
        # A blank line where the screen has one, the table's cells a row to a
        # line, the reply's bullet left out.
        assert copies.osc == ["line 3\n\nHere is the plan.\n\nname\tvalue\na\t1\n\n"
                              "• first item"]


async def test_a_click_copies_nothing_and_a_double_click_its_block(tmp_path: Path,
                                                                   copies: Copies) -> None:
    provider = bash_script()
    app = make_app(tmp_path, provider, tools=[Lines()])
    async with app.run_test(size=(100, 40)) as pilot:
        await a_bash_call(pilot, provider)
        body = static_with(app, "line 1")
        await pilot.click(body, offset=(8, 1))
        await pilot.pause()
        assert copies.osc == [] and app.flash == ""
        await pilot.click(body, offset=(8, 1), times=2)
        await settle(pilot, lambda: copies.osc == ["line 1\nline 2\nline 3"])
        # A click on the scrollbar keeps the selection: it is not copied again.
        app.post_message(events.TextSelected())
        await pilot.pause()
        assert len(copies.osc) == 1


async def test_ctrl_c_copies_a_selection_instead_of_clearing_or_quitting(
        tmp_path: Path, copies: Copies) -> None:
    provider = bash_script()
    app = make_app(tmp_path, provider, tools=[Lines()])
    async with app.run_test(size=(100, 40)) as pilot:
        await a_bash_call(pilot, provider)
        box = app.query_one(PromptInput)
        app.prefill("draft")
        body = static_with(app, "line 1")
        await drag(pilot, body, (5, 0), body, (10, 0))
        await pilot.press("ctrl+c")
        assert copies.osc == ["line 1", "line 1"] and box.text == "draft"
        assert not app.screen.selections and plain(app.query_one("#hints")) == \
            "Copied 6 characters"
        await drag(pilot, body, (5, 0), body, (10, 0))
        await pilot.press(*"s!")  # typing goes to the prompt (and ends the selection)
        assert box.text == "drafts!" and not app.screen.selections
        await pilot.press("ctrl+c")  # nothing selected: as before
        assert box.text == "" and app.is_running
        await pilot.press("ctrl+c")
        assert plain(app.query_one("#hints")) == "Press Ctrl-C again to exit"
        await pilot.press("ctrl+c")
        await settle(pilot, lambda: not app.is_running)
        assert len(copies.osc) == 3


@pytest.mark.parametrize("theme", ["ansi-dark", "ansi-light"])
async def test_the_selection_shows_in_both_themes(tmp_path: Path, copies: Copies,
                                                  theme: str) -> None:
    provider = bash_script()
    app = make_app(tmp_path, provider, tools=[Lines()])
    async with app.run_test(size=(100, 40)) as pilot:
        app.theme = theme
        await a_bash_call(pilot, provider)
        body = static_with(app, "line 1")
        await drag(pilot, body, (5, 0), body, (10, 0))
        cells = list(body.render_line(0))
        gutter, selected = cells[:5], cells[5].style
        assert all(c.style is None or c.style.bgcolor is None
                   or c.style.bgcolor.type is ColorType.DEFAULT for c in gutter)
        assert selected is not None and selected.bgcolor is not None
        assert selected.bgcolor.type is ColorType.STANDARD  # an ANSI colour: no blending


class Hangs(App[None]):
    def compose(self) -> ComposeResult:
        rows = [("one two three four five six seven", ""), ("", ""), ("\tx", "red")]
        yield Static(render.result_block(rows))


async def test_a_hanging_block_wraps_under_its_text_and_selects_past_its_gutter() -> None:
    app = Hangs(ansi_color=True)
    async with app.run_test(size=(20, 10)) as pilot:
        await pilot.pause()
        block = app.query_one(Static)
        rows = [block.render_line(y).text.rstrip() for y in range(block.size.height)]
        assert rows == ["  ⎿  one two three", "     four five six", "     seven", "",
                        "             x"]
        assert str(block.visual) == "one two three four five six seven\n\n        x"
        assert plain(block).startswith("  ⎿  one")  # the text as drawn keeps its gutter
        # A row's gutter points at its own text: a drag from the left edge of
        # the wrapped row starts there.
        await pilot.mouse_down(block, offset=(0, 1))
        await pilot.hover(block, offset=(9, 2))
        await pilot.mouse_up(block, offset=(9, 2))
        assert selection.selected_text(app.screen) == "four five six seven"
        assert block.get_selection(Selection(None, None)) == (str(block.visual), "\n")


def test_the_programs_besides_osc_52_follow_the_environment() -> None:
    def which(*found: str) -> Any:
        return lambda name: f"/bin/{name}" if name in found else None

    local = {"DISPLAY": ":0"}
    assert clipboard.programs(local, which("xclip", "xsel")) == [
        (["xclip", "-selection", "clipboard"],)]
    assert clipboard.programs(local, which("xsel")) == [(["xsel", "--clipboard", "--input"],)]
    assert clipboard.programs({"WAYLAND_DISPLAY": "wayland-0", **local},
                              which("wl-copy", "xclip")) == [(["wl-copy"],)]
    assert clipboard.programs({}, which("pbcopy", "xclip")) == [(["pbcopy"],)]
    assert clipboard.programs({}, which("xclip")) == []  # no display to copy to
    tmux = {"TMUX": "/tmp/tmux-1/default,1,0"}
    assert clipboard.programs({**tmux, **local}, which("xclip"))[0] == clipboard.TMUX
    over_ssh = {**tmux, **local, "SSH_CONNECTION": "10.0.0.1 22 10.0.0.2 22"}
    assert clipboard.programs(over_ssh, which("xclip")) == [clipboard.TMUX]
    assert clipboard.programs({"SSH_TTY": "/dev/pts/1"}, which("pbcopy")) == []


def test_an_older_tmux_takes_the_buffer_without_w(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    def run(command: list[str], data: bytes) -> bool:
        ran.append(command)
        return "-w" not in command

    monkeypatch.setattr(clipboard, "_run", run)
    clipboard._deliver([clipboard.TMUX, (["pbcopy"],)], "hi")
    assert ran == [["tmux", "load-buffer", "-w", "-"], ["tmux", "load-buffer", "-"], ["pbcopy"]]


def test_a_program_that_fails_or_is_missing_is_not_an_error(tmp_path: Path) -> None:
    assert not clipboard._run(["sh", "-c", "exit 3"], b"hi")
    assert not clipboard._run(["/nonexistent/clipboard-program"], b"hi")
    # As xclip does: the text read, a process left serving it.
    took = time.monotonic()
    assert clipboard._run(["sh", "-c", 'cat > "$0"; sleep 3 &', str(tmp_path / "got")], b"hi")
    assert time.monotonic() - took < clipboard.TIMEOUT
    assert (tmp_path / "got").read_bytes() == b"hi"


async def test_copy_copies_the_nth_latest_reply(tmp_path: Path, copies: Copies) -> None:
    provider = ScriptedProvider([reply("first answer"),
                                 reply("", ("Bash", {"command": "seq 1", "n": 1})),
                                 reply("second answer")])
    app = make_app(tmp_path, provider, tools=[Lines()])
    async with app.run_test(size=(100, 40)) as pilot:
        await type_line(pilot, "/copy")
        await settle(pilot, lambda: "no reply to copy yet" in shown(app))
        await type_line(pilot, "one")
        await settle(pilot, lambda: len(provider.requests) == 1 and not app.busy)
        await type_line(pilot, "two")
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        await type_line(pilot, "/copy")
        await settle(pilot, lambda: "Copied the last reply (13 characters)" in shown(app))
        await type_line(pilot, "/copy 2")  # the reply of a tool call only does not count
        await settle(pilot, lambda: "Copied the 2nd-latest reply (12 characters)" in shown(app))
        assert copies.osc == ["second answer", "first answer"]
        await type_line(pilot, "/copy 3")
        await settle(pilot, lambda: "there are only 2 replies" in shown(app))
        await type_line(pilot, "/copy last")
        await settle(pilot, lambda: "/copy N the Nth-latest" in shown(app))
        assert len(copies.osc) == 2


def test_the_shortcuts_panel_mentions_copying_in_its_columns() -> None:
    lines = Text.from_markup(SHORTCUTS).plain.splitlines()
    assert any("drag to copy" in line for line in lines)
    assert any("/copy for last reply" in line for line in lines)
    columns = {m.start(1) for line in lines for m in re.finditer(r"(?:^| {2,})(\S)", line)}
    assert columns == {0, 23, 57}
