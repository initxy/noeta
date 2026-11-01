"""A task recorded under an agent's former name (a Claude Code plugin's
agent before it was named ``<plugin>:<agent>``) still runs, through
``Runtime.agent_aliases``; the model never sees the former name."""

from __future__ import annotations

from pathlib import Path

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.host.local import LocalApp, former_agent_names
from noeta.testing import ScriptedProvider, reply


def test_former_names_are_the_unambiguous_bare_plugin_agent_names() -> None:
    names = ["main", "Explore", "tools:reviewer", "tools:Explore", "a:dup", "b:dup",
             "tools:ops:pager"]
    assert former_agent_names(names) == {"reviewer": "tools:reviewer"}


async def test_a_task_recorded_under_a_plugin_agents_bare_name_wakes(tmp_path: Path) -> None:
    path = str(tmp_path / "t.db")
    before = LocalApp(SqliteStore(path), [Agent(name="reviewer", model="m")],
                      ScriptedProvider([reply("first")]), env_for=lambda _s: None)
    async with before:
        tid = await before.client.create("reviewer", "look")
        assert (await before.client.wait(tid, timeout=5)).waiting_on == "input"
    provider = ScriptedProvider([reply("second")])
    now = LocalApp(SqliteStore(path), [Agent(name="main", model="m"),
                                       Agent(name="tools:reviewer", model="m")],
                   provider, env_for=lambda _s: None)
    assert "reviewer" not in now.runtime.agents
    async with now:
        await now.adopt(tid)  # not refused as an unknown agent
        await now.client.send(tid, "again")
        row = await now.client.wait(tid, timeout=5)
    assert row.end_status is None and row.waiting_on == "input"
    assert provider.steps == []  # the renamed agent took the turn
    now.replace_agents([Agent(name="main", model="m")])
    assert now.runtime.agent("reviewer") is None and now.runtime.agent_aliases == {}
