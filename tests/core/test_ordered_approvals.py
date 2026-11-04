"""Approvals are asked one call at a time, in the reply's order: a call
after an unanswered ask neither runs nor is refused, and a wake before the
answer does not gate the asked call again."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import Event, ToolFinished, Waiting
from noeta.core.extension import Ask, Extension, Gate, TaskView
from noeta.core.runner import texts
from noeta.core.store import Appended, Claim, NewTask, Patch, Post
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ToolUse, user
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


async def test_a_message_now_before_the_answer_does_not_gate_again(store: SqliteStore) -> None:
    """A ``Say(now)`` (a job's end, a Monitor event) clears the standing wait;
    the asked call still waits ungated and no second approval is written."""
    shell, gate = Fake("Bash"), AsksForCommits()
    provider = ScriptedProvider([reply("", bash("git commit -m x")), reply("done")])
    rt = runtime(store, provider, agent([gate], [shell]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await client.send(tid, user("job 1 ended", origin="system"), when="now")
    await rt.run_until_idle()
    evs = await events(client, tid)
    assert gate.gated == ["git commit -m x"]
    [waiting] = [e for e in evs if isinstance(e, Waiting)]
    row = await client.get(tid)
    assert (row.status, row.waiting_on) == ("waiting", "approval")
    await client.answer(tid, waiting.ids[0], "allow")
    await rt.run_until_idle()
    assert [r["command"] for r in shell.runs] == ["git commit -m x"]


async def test_a_declined_ask_refuses_the_rest_of_the_reply(store: SqliteStore) -> None:
    """Declining one call stops the turn: the reply's later calls get the
    refusal without being gated or run (Claude Code aborts the rest)."""
    shell, gate = Fake("Bash"), AsksForCommits()
    provider = ScriptedProvider([reply(
        "", bash("git commit -m x"), bash("ls"), bash("git commit -m y"))])
    rt = runtime(store, provider, agent([gate], [shell]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [waiting] = [e for e in await events(client, tid) if isinstance(e, Waiting)]
    await client.answer(tid, waiting.ids[0], "deny")
    await rt.run_until_idle()
    evs = await events(client, tid)
    assert shell.runs == [] and gate.gated == ["git commit -m x"]
    assert len([e for e in evs if isinstance(e, Waiting) and e.on == "approval"]) == 1
    finished = [e for e in evs if isinstance(e, ToolFinished)]
    assert len(finished) == 3 and all(f.result.is_error for f in finished)
    assert all(texts.REJECTED_BY_USER in str(f.result.content) for f in finished)
    assert (await client.get(tid)).waiting_on == "input"


class Crash(BaseException):
    """Stands in for the process dying."""


class DiesAfterTheDecline(SqliteStore):
    """Dies right after the declined call's result commits."""

    async def append(self, claim: Claim, events: Sequence[Event], *, expected_seq: int,
                     patch: Patch = Patch(), posts: Sequence[Post] = (),
                     creates: Sequence[NewTask] = ()) -> Appended:
        done = await super().append(claim, events, expected_seq=expected_seq, patch=patch,
                                   posts=posts, creates=creates)
        if any(isinstance(e, ToolFinished) and e.note == texts.REJECTED_MARKER
               for e in events):
            raise Crash()
        return done


async def test_a_crash_after_the_decline_still_refuses_the_rest(tmp_path: Path) -> None:
    """The claimer after a crash between the decline's result and the later
    calls' refusals refuses them too (from the log), ungated and unrun."""
    path = str(tmp_path / "t.db")
    shell, gate = Fake("Bash"), AsksForCommits()
    provider = ScriptedProvider([reply("", bash("git commit -m x"), bash("ls"))])
    dying = DiesAfterTheDecline(path)
    client = Client(dying)
    tid = await client.create("a", "go")
    await runtime(dying, provider, agent([gate], [shell])).run_until_idle()
    [waiting] = [e for e in await events(client, tid) if isinstance(e, Waiting)]
    await client.answer(tid, waiting.ids[0], "deny")
    with pytest.raises(Crash):
        await runtime(dying, provider, agent([gate], [shell])).run_until_idle()
    await dying.close()

    store = SqliteStore(path)
    try:
        def expire(conn: Any) -> None:
            conn.execute("UPDATE tasks SET lease_until = 0 WHERE task_id = ?", (tid,))

        await store._run(expire)
        await runtime(store, provider, agent([gate], [shell])).run_until_idle()
        finished = [e for e in await events(Client(store), tid) if isinstance(e, ToolFinished)]
        assert shell.runs == [] and gate.gated == ["git commit -m x"]
        assert len(finished) == 2 and all(
            texts.REJECTED_BY_USER in str(f.result.content) for f in finished)
    finally:
        await store.close()
