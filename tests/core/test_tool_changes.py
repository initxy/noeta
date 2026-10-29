"""``Extension.tool_changes``: tools computed from the others', withheld
tools and request-only entries, the same whatever the plugin order."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished, ToolStarted
from noeta.core.extension import Deny, Extension, Gate, TaskView, ToolChanges, ToolEntry
from noeta.core.runner import Runtime
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import ToolSpec, ToolUse
from noeta.testing import ScriptedProvider, reply


class Fake:
    read_only = True
    clearable = True
    input_schema: dict[str, Any] = {"type": "object"}

    def __init__(self, name: str, *, last: bool = False) -> None:
        self.name = name
        self.listed_last = last
        self.description = f"The {name} tool."

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output(f"{self.name} ran")


class Offers(Extension):
    name = "offers"

    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools = list(tools)

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools


class Counter(Extension):
    """Adds ``Count`` naming how many tools the others offer, and keeps
    ``Hidden`` out of the request."""

    name = "counter"

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def tool_changes(self, view: TaskView, offered: Sequence[Tool]) -> ToolChanges:
        self.seen.append([t.name for t in offered])
        return ToolChanges(add=[Fake("Count")], withhold={"Hidden": "Hidden is not here."},
                           entries=[ToolEntry(spec=ToolSpec(name="Mark", description="m",
                                                            input_schema={}), before="Read")])


class Tail(Extension):
    """Adds a ``listed_last`` tool and an entry with no anchor."""

    name = "tail"

    def tool_changes(self, view: TaskView, offered: Sequence[Tool]) -> ToolChanges:
        return ToolChanges(add=[Fake("Zed", last=True)], entries=[
            ToolEntry(spec=ToolSpec(name="Gap", description="g", input_schema={}))])


class Refuses(Extension):
    """Denies every call: a withheld call must be answered before it."""

    name = "refuses"

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        self.asked.append(call.name)
        return Deny(reason="no")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _run(store: SqliteStore, exts: list[Extension],
               provider: ScriptedProvider) -> str:
    agent = Agent(name="a", model="m", extensions=exts)
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    tid = await Client(store).create("a", "go")
    await rt.run_until_idle()
    return tid


@pytest.mark.parametrize("flip", [False, True])
async def test_changes_apply_whatever_the_plugin_order(store: SqliteStore, flip: bool) -> None:
    counter = Counter()
    offers = Offers([Fake("Write"), Fake("Read"), Fake("Hidden"), Fake("Srv", last=True)])
    exts: list[Extension] = [counter, Tail(), offers]
    provider = ScriptedProvider([reply("done")])
    await _run(store, list(reversed(exts)) if flip else exts, provider)
    [request] = provider.requests
    assert [t.name for t in request.tools] == ["Count", "Mark", "Read", "Write", "Gap",
                                               "Srv", "Zed"]
    # Each extension sees the offered tools only, never another's changes.
    assert counter.seen and all(names == ["Write", "Read", "Hidden", "Srv"]
                                for names in counter.seen)


async def test_a_call_to_a_withheld_tool_gets_its_error_before_any_gate(
        store: SqliteStore) -> None:
    refuses = Refuses()
    provider = ScriptedProvider([reply("", ("Hidden", {})), reply("done")])
    tid = await _run(store, [Offers([Fake("Hidden")]), Counter(), refuses], provider)
    events = [r.event for r in await Client(store).events(tid)]
    [finished] = [e for e in events if isinstance(e, ToolFinished)]
    assert finished.result.is_error and finished.result.text == "Hidden is not here."
    assert not any(isinstance(e, ToolStarted) for e in events) and refuses.asked == []


async def test_an_added_tool_runs(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Count", {})), reply("done")])
    tid = await _run(store, [Offers([Fake("Read")]), Counter()], provider)
    [finished] = [r.event for r in await Client(store).events(tid)
                  if isinstance(r.event, ToolFinished)]
    assert finished.result.text == "Count ran"
