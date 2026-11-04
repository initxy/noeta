"""More of Claude Code's terminal UI: the footer's "N% until auto-compact",
prompt commands from the host, ``/mcp`` and
``/hooks``, history across sessions, completion inside a command word."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.types import Usage
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.window import derive_window
from noeta.plugins.hooks import Hooks
from noeta.plugins.markdown_commands import REVIEW_PROMPT, MarkdownCommands
from noeta.testing import ScriptedProvider, reply

from support import MODEL, AskBash, Lines, make_app, plain, ready, settle, shown, type_line


async def test_the_footer_counts_down_to_auto_compact(tmp_path: Path) -> None:
    window = derive_window(MODEL, auto_compact_window=60_000)
    near = window.threshold - 5_000
    provider = ScriptedProvider([reply("far", usage=Usage(uncached=100, output=5)),
                                 reply("near", usage=Usage(uncached=near, output=0))])
    app = make_app(tmp_path, provider, Compaction(auto_compact_window=60_000))
    async with app.run_test(size=(160, 30)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "hi")
        await settle(pilot, lambda: len(provider.requests) == 1 and not app.busy)
        assert "until auto-compact" not in plain(app.query_one("#status"))
        await type_line(pilot, "again")
        left = round(5_000 / window.threshold * 100)
        await settle(pilot, lambda: f"{left}% until auto-compact"
                     in plain(app.query_one("#status")))


async def test_review_is_a_prompt_command_shown_as_typed(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("reviewed")])
    app = make_app(tmp_path, provider, MarkdownCommands())
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app) and app.plugin_commands)
        assert {"init", "review"} <= {c.name for c in app.plugin_commands}
        await type_line(pilot, "/review 42")
        await settle(pilot, lambda: len(provider.requests) == 1 and not app.busy)
        sent = "\n".join(m.text for m in provider.requests[0].messages)
        assert REVIEW_PROMPT in sent and "ARGUMENTS: 42" in sent
        assert "❯ /review 42" in shown(app) and "<command-name>" not in shown(app)


async def test_mcp_and_hooks_are_panels_of_what_the_plugins_say(tmp_path: Path) -> None:
    hooks = Hooks(hooks={"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "./check.sh"}]}]})
    app = make_app(tmp_path, ScriptedProvider([]), hooks)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "/hooks")
        await settle(pilot, lambda: "Bash · command · ./check.sh" in shown(app))
        await type_line(pilot, "/mcp")
        await settle(pilot, lambda: "No MCP servers configured." in shown(app))


async def test_prompt_history_is_kept_per_project_across_sessions(tmp_path: Path) -> None:
    from noeta_tui import history
    from noeta_tui.widgets import Completions, PromptInput

    provider = ScriptedProvider([reply("a"), reply("b")])
    first = make_app(tmp_path, provider)
    async with first.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(first))
        for n, prompt in enumerate(["fix the add test", "write docs"], 1):
            await type_line(pilot, prompt)
            await settle(pilot, lambda n=n: not first.busy and len(provider.requests) == n)
    again = make_app(tmp_path, ScriptedProvider([]))
    async with again.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(again))
        box = again.query_one(PromptInput)
        await pilot.press("up")
        assert box.text == "write docs"
        box.text = ""
        await pilot.press("ctrl+r", *"add")
        assert [v for v, _, _ in again.query_one(Completions).items] == ["fix the add test"]
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert history.load(history.history_file(str(tmp_path / "home"), str(other))) == []


def test_the_history_file_is_bounded(tmp_path: Path) -> None:
    from noeta_tui import history

    path = history.history_file(str(tmp_path), "/w")
    assert path is not None
    for n in range(2 * history.HISTORY_MAX + 1):
        history.append(path, f"prompt {n}")
    kept = history.load(path)
    assert len(kept) == history.HISTORY_MAX and kept[-1] == f"prompt {2 * history.HISTORY_MAX}"
    assert len(path.read_text().splitlines()) == history.HISTORY_MAX
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    path.write_text(path.read_text() + '{"text": "cut sh')
    assert history.load(path)[-1] == f"prompt {2 * history.HISTORY_MAX}"
    assert history.load(None) == [] and history.history_file("", "/w") is None


def test_a_history_trim_uses_its_own_temp_file(tmp_path: Path) -> None:
    """Two TUIs trimming one project's history at once must not share a
    temp file: here another's is in the way (a directory, so writing it fails)."""
    from noeta_tui import history

    path = history.history_file(str(tmp_path), "/w")
    assert path is not None
    path.parent.mkdir(parents=True)
    (path.parent / "history.tmp").mkdir()
    for n in range(2 * history.HISTORY_MAX + 1):
        history.append(path, f"prompt {n}")
    assert len(path.read_text().splitlines()) == history.HISTORY_MAX
    assert sorted(p.name for p in path.parent.iterdir()) == ["history.jsonl", "history.tmp"]


async def test_tab_amends_a_yes_or_a_no_with_what_to_do(tmp_path: Path) -> None:
    from noeta.core.types import Text, ToolResult
    from noeta_tui.widgets import ApprovalCard

    provider = ScriptedProvider([reply("", ("Bash", {"command": "rm -rf build", "n": 1})),
                                 reply("", ("Bash", {"command": "ls", "n": 1})),
                                 reply("ok")])
    app = make_app(tmp_path, provider, AskBash(), tools=[Lines()])
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "clean")
        await settle(pilot, lambda: len(app.query(ApprovalCard)) == 1)
        card = app.query_one(ApprovalCard)
        assert "Tab to amend" in shown(app)
        await pilot.press("down", "down", "tab", *"use make clean", "enter")  # No, amended
        await settle(pilot, lambda: card.done and not app.busy)
        assert "No: use make clean" in plain(card.query_one(".approval-result"))
        await type_line(pilot, "list it then")
        await settle(pilot, lambda: len(app.query(ApprovalCard)) == 2)
        second = list(app.query(ApprovalCard))[1]
        await pilot.press("tab", "enter")  # an empty line closes the amend
        assert not second.done and not second.query("Input")
        await pilot.press("tab", *"then stop", "enter")  # Yes, amended
        await settle(pilot, lambda: second.done and provider.steps == [] and not app.busy)
    asked = provider.requests[1].messages
    results = [b for m in asked for b in m.content if isinstance(b, ToolResult)]
    assert "use make clean" in results[0].text
    last = provider.requests[2].messages
    i = next(i for i, m in enumerate(last) if any(isinstance(b, ToolResult) for b in m.content))
    after = [b.text for m in last[i:] for b in m.content if isinstance(b, Text)]
    assert "then stop" in after  # the yes's words come after the call's result


async def test_esc_in_the_amend_line_closes_it_and_keeps_the_turn(tmp_path: Path) -> None:
    from noeta_tui.widgets import ApprovalCard, Choices

    provider = ScriptedProvider([reply("", ("Bash", {"command": "rm -rf build", "n": 1})),
                                 reply("ok")])
    app = make_app(tmp_path, provider, AskBash(), tools=[Lines()])
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "clean")
        await settle(pilot, lambda: len(app.query(ApprovalCard)) == 1)
        card = app.query_one(ApprovalCard)
        await pilot.press("tab", *"half a thought", "escape")
        await pilot.pause()
        assert not card.query("Input") and not card.amending and not card.done
        assert app.busy and isinstance(app.focused, Choices)
        await pilot.press("escape")  # Esc on the choices still says no
        await settle(pilot, lambda: card.done and not app.busy)
        assert "No" in plain(card.query_one(".approval-result"))


async def test_tab_completes_the_command_word_with_the_cursor_anywhere_in_it(
        tmp_path: Path) -> None:
    from noeta_tui.widgets import Completions, PromptInput

    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        box, menu = app.query_one(PromptInput), app.query_one(Completions)
        await pilot.press(*"/mo", "tab")
        assert box.text == "/model " and not menu.display
        box.text = ""
        await pilot.press(*"/mod", "left", "tab")
        assert box.text == "/model " and box.cursor_location == (0, 7)
        box.text = ""
        await pilot.press(*"/sta x", "left", "left", "t")
        assert menu.display  # the cursor is back in the command word
        await pilot.press("tab")
        assert box.text == "/status x" and box.cursor_location == (0, 8)
        box.text = ""
        await pilot.press(*"say /mo")
        assert not menu.display  # a command is only the first word


def test_the_front_end_exits_with_the_apps_code(monkeypatch: Any) -> None:
    import pytest

    import noeta_tui
    from noeta.host.app import FrontEndStart
    from noeta_tui.app import NoetaApp

    def ran(self: NoetaApp, **_: Any) -> None:
        self._return_code = 1

    monkeypatch.setattr(NoetaApp, "run", ran)
    with pytest.raises(SystemExit) as exited:
        noeta_tui.run(object(), FrontEndStart(agent="main", cwd="/w", model=MODEL,
                                              continue_last=True))
    assert exited.value.code == 1
