"""The prompt takes a paste once and gets what is typed while the transcript
has focus; a reply starts beside its bullet; CJK text breaks between
characters, never before a comma or a full stop."""

from __future__ import annotations

from pathlib import Path

from rich._wrap import divide_line
from textual import events
from textual.content import Content

from noeta.testing import ScriptedProvider, reply
from noeta_tui.widgets import AssistantMessage, PromptInput

from support import make_app, plain, ready, settle, type_line


async def test_a_paste_is_inserted_once(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: ready(app))
        box = app.query_one(PromptInput)
        app.post_message(events.Paste("hello"))  # as the terminal's driver does
        await settle(pilot, lambda: box.text == "hello")
        await pilot.pause(0.1)
        assert box.text == "hello"


async def test_typing_or_pasting_with_the_transcript_focused_goes_to_the_prompt(
        tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: ready(app))
        box = app.query_one(PromptInput)
        app.query_one("#transcript").focus()
        await pilot.press("h", "i")
        await settle(pilot, lambda: box.has_focus and box.text == "hi")
        app.query_one("#transcript").focus()
        await pilot.pause()
        app.post_message(events.Paste(" there"))
        await settle(pilot, lambda: box.text == "hi there")


async def test_a_reply_starts_beside_its_bullet(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([reply("# Title\n\nOne.\n\nTwo.")]))
    async with app.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "go")
        await settle(pilot, lambda: not app.busy and app.query(AssistantMessage))
        message = app.query_one(AssistantMessage)
        await settle(pilot, lambda: len(message.children) == 1)
        [text] = message.children
        bullet = message.parent.children[0]  # type: ignore[union-attr]
        await pilot.pause(0.1)
        assert text.region.y == bullet.region.y  # no blank line above the title
        # Blocks one blank line apart, the title bold.
        assert plain(text).split("\n") == ["Title", "", "One.", "", "Two."]


def test_cjk_text_breaks_between_characters() -> None:
    import noeta_tui.app  # noqa: F401 — installs the CJK line breaks

    text = "一句话总结：这是个纯词汇表 + 事件读取工具模块，本身不做权限裁决，只负责让插件达成一致。"
    lines = [line.plain for line in Content(text).wrap(30)]
    assert lines == ["一句话总结：这是个纯词汇表 +", "事件读取工具模块，本身不做权限",
                     "裁决，只负责让插件达成一致。"]
    assert not any(line.startswith(("，", "。")) for line in lines)
    assert [line.plain for line in Content("plain words wrap at spaces").wrap(12)] == [
        "plain words", "wrap at", "spaces"]
    # What a reply or a notice is drawn with (Rich's wrapping).
    cuts = divide_line(text, 30)
    ends = zip([0, *cuts], [*cuts, len(text)], strict=True)
    assert [text[a:b].rstrip() for a, b in ends] == lines
    assert divide_line("plain words wrap at spaces", 12) == [12, 20]
