"""Claude Code's terminal UI where v1-finish left it short: the footer's
"N% until auto-compact", prompt commands from the host, ``/mcp`` and
``/hooks``, history across sessions, completion inside a command word."""

from __future__ import annotations

from pathlib import Path

from noeta.core.types import Usage
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.window import derive_window
from noeta.testing import ScriptedProvider, reply

from support import MODEL, make_app, plain, ready, settle, type_line


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
