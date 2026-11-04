"""More of Claude Code's terminal UI: the footer's "N% until auto-compact",
prompt commands from the host, ``/mcp`` and
``/hooks``, history across sessions, completion inside a command word."""

from __future__ import annotations

from pathlib import Path

from noeta.core.types import Usage
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.window import derive_window
from noeta.plugins.hooks import Hooks
from noeta.plugins.markdown_commands import REVIEW_PROMPT, MarkdownCommands
from noeta.testing import ScriptedProvider, reply

from support import MODEL, make_app, plain, ready, settle, shown, type_line


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
