"""Claude Code's limits on what tool results put in the context, applied by
the runner to every tool: a text result over 50,000 characters (or the
tool's own smaller limit) is saved and previewed, and one reply's results
together stay within 200,000 characters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.events import ToolFinished
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ToolResult
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider, reply


class Big:
    name = "Big"
    description = "Print a lot."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output(str(input.get("fill", "x")) * int(input["size"]))


class Bounded(Big):
    name = "Bounded"
    max_result_chars = None


class Small(Big):
    name = "Small"
    max_result_chars = 1_000


async def _results(tmp_path: Path, *calls: tuple[str, dict[str, Any]]) -> list[ToolResult]:
    provider = ScriptedProvider([reply("", *calls), reply("done")])
    agent = Agent(name="main", model="m", tools=[Big(), Bounded(), Small()])
    async with LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                        env_for=lambda _s: LocalExecEnv(str(tmp_path)),
                        spill_root=str(tmp_path / "spill")) as app:
        tid = await app.client.create("main", "go")
        await app.client.wait(tid, timeout=10)
        results = [r.event.result for r in await app.client.events(tid)
                   if isinstance(r.event, ToolFinished)]
    # In call order (calls that run together finish in any order).
    return sorted(results, key=lambda r: int(r.call_id.rsplit("_", 1)[1]))


async def test_a_result_over_the_ceiling_is_saved(tmp_path: Path) -> None:
    big, bounded, small, short = await _results(
        tmp_path, ("Big", {"size": 60_000}), ("Bounded", {"size": 60_000}),
        ("Small", {"size": 1_500}), ("Big", {"size": 40_000}))
    assert big.text.startswith("<persisted-output>\nOutput too large (58.6KB).")
    saved = big.text.split("Full output saved to: ", 1)[1].split("\n", 1)[0]
    assert Path(saved).read_text() == "x" * 60_000
    assert bounded.text == "x" * 60_000  # bounds itself (Read)
    assert small.text.startswith("<persisted-output>")
    assert short.text == "x" * 40_000


async def test_one_replys_results_stay_within_the_budget(tmp_path: Path) -> None:
    results = await _results(tmp_path, *(("Big", {"size": 45_000, "fill": str(i)})
                                         for i in range(6)))
    inline = [r for r in results if not r.text.startswith("<persisted-output>")]
    assert sum(len(r.text) for r in results) <= 200_000
    assert len(inline) == 4  # 4 x 45,000 fits; the fifth and sixth are saved
