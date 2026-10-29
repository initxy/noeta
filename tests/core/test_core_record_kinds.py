"""A plugin cannot write a ``core`` record of a kind the core does not know:
folding one leaves the task to a newer worker, so this one would never
run it again."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.core.records import CORE
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.testing import ScriptedProvider, reply


class BogusRecord:
    name = "Bogus"
    description = "Write a core record of an unknown kind."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ctx.record(CORE, "not_a_core_kind", {})
        return text_output("recorded")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def test_a_view_refuses_an_unknown_core_kind() -> None:
    view = TaskView(state=TaskState())
    with pytest.raises(ValueError, match="not_a_core_kind"):
        view.record(CORE, "not_a_core_kind", {})
    assert view.pending == []
    view.record("mine", "not_a_core_kind", {})  # an extension's own kinds are its own
    assert len(view.pending) == 1


async def test_a_tool_writing_an_unknown_core_kind_fails_and_the_task_goes_on(
        store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Bogus", {})), reply("done")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", tools=[BogusRecord()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [result] = [r.event.result for r in await client.events(tid)
                if isinstance(r.event, ToolFinished)]
    assert result.is_error and "not_a_core_kind" in result.text
    assert (await client.get(tid)).waiting_on == "input"
