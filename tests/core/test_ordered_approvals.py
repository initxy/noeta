"""Approvals are asked one call at a time, in the reply's order: a call
after an unanswered ask neither runs nor is refused, and a wake before the
answer does not gate the asked call again."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ToolFinished, Waiting
from noeta.core.extension import Ask, Extension, Gate, TaskView
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ToolUse
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, events, runtime


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


class AsksForCommits(Extension):
    """Asks before ``git commit``; counts every gate it is asked (a
    PreToolUse hook would run each time)."""

    name = "asks_for_commits"

    def __init__(self) -> None:
        self.gated: list[str] = []

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        command = str(call.input.get("command", ""))
        self.gated.append(command)
        return Ask(prompt="commit?") if command.startswith("git commit") else None


class Together(Fake):
    """A read-only tool whose two runs must overlap to finish."""

    read_only = True

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.both = asyncio.Event()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.runs.append(input)
        if len(self.runs) == 2:
            self.both.set()
        await asyncio.wait_for(self.both.wait(), 5)
        return text_output("read")


def bash(command: str) -> tuple[str, dict[str, Any]]:
    return ("Bash", {"command": command})


async def test_a_call_after_an_unanswered_ask_waits_for_it(store: SqliteStore) -> None:
    shell, gate = Fake("Bash"), AsksForCommits()
    provider = ScriptedProvider([
        reply("", bash("git commit -m x"), bash("git push")), reply("done")])
    rt = runtime(store, provider, agent([gate], [shell]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    evs = await events(client, tid)
    assert shell.runs == [] and not any(isinstance(e, ToolFinished) for e in evs)
    last = evs[-1]
    assert isinstance(last, Waiting) and last.on == "approval" and len(last.ids) == 1
    assert gate.gated == ["git commit -m x"]  # the push is not gated yet
    await client.answer(tid, last.ids[0], "allow")
    await rt.run_until_idle()
    assert [r["command"] for r in shell.runs] == ["git commit -m x", "git push"]
    assert (await client.get(tid)).waiting_on == "input"


async def test_calls_before_the_first_ask_run_together(store: SqliteStore) -> None:
    read, shell, gate = Together("Read"), Fake("Bash"), AsksForCommits()
    provider = ScriptedProvider([reply(
        "", ("Read", {"file_path": "a"}), ("Read", {"file_path": "b"}),
        bash("git commit -m x"), ("Read", {"file_path": "c"}))])
    rt = runtime(store, provider, agent([gate], [read, shell]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert [r["file_path"] for r in read.runs] == ["a", "b"]
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "approval"


async def test_a_wake_before_the_answer_does_not_gate_again(store: SqliteStore) -> None:
    shell, gate = Fake("Bash"), AsksForCommits()
    provider = ScriptedProvider([reply("", bash("git commit -m x")), reply("done")])
    rt = runtime(store, provider, agent([gate], [shell]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await client.signal(tid, "unrelated")
    await rt.run_until_idle()
    evs = await events(client, tid)
    assert gate.gated == ["git commit -m x"]
    [waiting] = [e for e in evs if isinstance(e, Waiting)]
    row = await client.get(tid)
    assert (row.status, row.waiting_on) == ("waiting", "approval")
    await client.answer(tid, waiting.ids[0], "allow")
    await rt.run_until_idle()
    assert [r["command"] for r in shell.runs] == ["git commit -m x"]
