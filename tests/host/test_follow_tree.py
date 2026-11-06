"""``follow_tree`` surfaces a sub-agent's approval however the root came to
wait for it: a workflow starts its next agent on taking the last one's end,
and the root's log then ends in plugin records, not a ``Waiting``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import Waiting
from noeta.core.extension import Ask, Extension, Gate, TaskView
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ModelRequest, ModelResponse, ToolUse
from noeta.host.pending import Pending, follow_tree
from noeta.plugins.workflow import Orchestration
from noeta.testing import reply

SCRIPT = """export const meta = { name: 'two-step', description: 'one then the other' }
const a = await agent('first step')
const b = await agent('second step')
return [a, b]
"""


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


class AskBash(Extension):
    name = "ask"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Ask(prompt="run it?") if call.name == "Bash" else None


class Bash:
    name = "Bash"
    description = "Run."
    input_schema = {"type": "object"}
    read_only = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("ran")


class Model:
    family = "scripted"

    def __init__(self) -> None:
        self.root = 0
        self.second = 0

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        first = request.messages[0].text if request.messages else ""
        if first.startswith("first step"):
            return reply("a")
        if first.startswith("second step"):
            self.second += 1
            return reply("", ("Bash", {})) if self.second == 1 else reply("b")
        self.root += 1
        return reply("", ("Workflow", {"script": SCRIPT})) if self.root == 1 else reply("ok")


async def _no_sleep(_: float) -> None:
    return None


async def test_a_later_workflow_agents_approval_surfaces(store: SqliteStore) -> None:
    main = Agent(name="main", model="m", extensions=[Orchestration()])
    worker = Agent(name="general-purpose", model="m", description="worker",
                   tools=[Bash()], extensions=[AskBash()])
    model = Model()
    rt = Runtime(store=store, agents={"main": main, "general-purpose": worker},
                 provider_for=lambda _m: model, env_for=lambda _s: None, sleep=_no_sleep)
    client = Client(store)
    root = await client.create("main", "go")
    await rt.run_until_idle()
    [second] = [t for t in await client.tasks(parent_id=root) if t.status != "ended"]
    assert second.waiting_on == "approval"
    # The root took the first agent's end and started the second: no
    # ``Waiting`` follows in its log.
    assert not isinstance((await client.events(root))[-1].event, Waiting)

    async def first_pending() -> list[Pending]:
        async for item in follow_tree(client, root, poll=0.01):
            if isinstance(item, list):
                return item
        return []

    [found] = await asyncio.wait_for(first_pending(), 5)
    assert (found.task_id, found.kind, found.tool) == (second.task_id, "approval", "Bash")
