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
