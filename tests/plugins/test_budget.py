"""The budget plugin: lifetime caps on model calls, tool calls, cost and
sub-agents, with or without the permissions plugin, and the counts a log
from before the split keeps under the permissions plugin's name."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, Recorded, ToolFinished
from noeta.core.extension import Extension
from noeta.core.loop.state import fold
from noeta.plugins.budget import BudgetCaps, BudgetState
from noeta.plugins.permissions import Permissions
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, events, runtime


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def results(evs: list[Any]) -> list[tuple[str, bool]]:
    return [(e.result.text, e.result.is_error) for e in evs if isinstance(e, ToolFinished)]


async def _run(store: SqliteStore, provider: ScriptedProvider, exts: list[Extension],
               tools: list[Any], config: dict[str, Any] | None = None) -> list[Any]:
    rt = runtime(store, provider, agent(exts, tools))
    client = Client(store)
    tid = await client.create("a", "go", config=config or {})
    await rt.run_until_idle()
    return await events(client, tid)


@pytest.mark.parametrize("with_permissions", [True, False])
async def test_budget_caps_tool_calls_and_iterations(store: SqliteStore,
                                                     with_permissions: bool) -> None:
    read = Fake("Read")
    provider = ScriptedProvider([reply("", ("Read", {}), ("Read", {"n": 2})),
                                 reply("", ("Read", {"n": 3})), reply("ok")])
    exts: list[Extension] = [BudgetCaps(max_tool_calls=1, max_iterations=1)]
    if with_permissions:
        exts.insert(0, Permissions(mode="bypassPermissions"))
    assert sorted(results(await _run(store, provider, exts, [read]))) == [
        ("max_iterations=1 exceeded", True), ("max_tool_calls=1 reached", True),
        ("ran Read", False)]


async def test_the_usd_cap_counts_calls_outside_the_conversation(store: SqliteStore) -> None:
    """``max_cost_usd`` counts what ``/cost`` counts: a compaction's
    summarize calls (``spent`` records) too, not only the replies."""
    from noeta.core import records as core_records
    from noeta.core.types import Usage

    class Summarize(Extension):
        name = "summarize"

        async def before_model(self, view: Any, request: Any) -> Any:
            if not view.state.steps_in_turn:
                spent = core_records.Spent(model="claude-opus-5-5",
                                           usage=Usage(uncached=1_000_000), purpose="compaction")
                view.record(core_records.CORE, spent.kind, spent.data())
            return request

    read = Fake("Read")
    provider = ScriptedProvider([reply("", ("Read", {})), reply("ok")])
    evs = await _run(store, provider, [Summarize(), BudgetCaps(max_cost_usd=0.5)], [read])
    assert results(evs) == [("max_cost_usd=0.5 reached", True)]


async def test_budget_caps_spawn_depth_by_default(store: SqliteStore) -> None:
    task = Fake("Task")
    provider = ScriptedProvider([reply("", ("Task", {})), reply("ok")])
    evs = await _run(store, provider, [BudgetCaps()], [task], config={"subtask_depth": 3})
    assert results(evs) == [("max_subtask_depth=3 reached", True)]


async def test_without_the_plugin_nothing_is_capped(store: SqliteStore) -> None:
    task = Fake("Task")
    provider = ScriptedProvider([reply("", ("Task", {})), reply("ok")])
    evs = await _run(store, provider, [Permissions(mode="bypassPermissions")], [task],
                     config={"subtask_depth": 3})
    assert results(evs) == [("ran Task", False)]
    assert not any(isinstance(e, ExtensionRecorded) and e.kind in ("step", "tool_call", "cost")
                   for e in evs)


def test_bad_configuration_raises() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        BudgetCaps(max_iterations=2, max_turns=3)
    assert BudgetCaps(max_turns=3).caps.max_iterations == 3


def test_a_log_from_before_the_split_keeps_its_counts() -> None:
    old = [("step", None), ("step", None), ("tool_call", None), ("cost", {"usd": 0.25}),
           ("mode", {"mode": "auto"})]
    log = [Recorded(seq=i + 1, at=0.0, event=ExtensionRecorded(
        extension="permissions", kind=kind, data=data)) for i, (kind, data) in enumerate(old)]
    log.append(Recorded(seq=9, at=0.0, event=ExtensionRecorded(extension="budget", kind="step")))
    caps = BudgetCaps()
    st = fold(log, {caps.name: caps.reduce}).ext["budget"]
    assert st == BudgetState(steps=3, tool_calls=1, cost_usd=0.25)
