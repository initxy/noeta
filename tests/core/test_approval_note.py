"""What a person adds to their yes at an approval prompt follows that call's
result in its message, as Claude Code's accept feedback does: no person
message is posted, and the words are in the log, so they survive a crash
and reach a sub-agent's call answered at the root."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.providers.anthropic import AnthropicProvider
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import Event, InputTaken, Say, ToolFinished, ToolStarted, Waiting
from noeta.core.extension import Ask, Extension, Gate, TaskView
from noeta.core.runner import texts
from noeta.core.store import Appended, Claim, NewTask, Patch, Post
from noeta.core.tool import Park, ToolContext, ToolOutput, text_output
from noeta.core.types import ModelRequest, ToolUse
from noeta.host.pending import answer_choice, pending_tree
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, events, runtime


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


class Asks(Extension):
    name = "asks"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Ask(prompt="?") if call.name == "T" else None


def last_user_blocks(provider: ScriptedProvider) -> list[dict[str, Any]]:
    """The newest request's last message, on Anthropic's wire."""
    body = AnthropicProvider(api_key="k").build_body(ModelRequest(
        model="m", messages=provider.requests[-1].messages, max_tokens=1))
    content: list[dict[str, Any]] = body["messages"][-1]["content"]
    return content


async def asked(client: Client, tid: str) -> str:
    [rid] = [e for e in await events(client, tid) if isinstance(e, Waiting)][-1].ids
    return rid


async def test_a_yes_with_words_puts_them_after_the_calls_result(store: SqliteStore) -> None:
    tool = Fake("T")
    provider = ScriptedProvider([reply("", ("T", {})), reply("done")])
    rt = runtime(store, provider, agent([Asks()], [tool]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await answer_choice(client, tid, await asked(client, tid), "allow", "then run the tests")
    await rt.run_until_idle()
    assert tool.runs == [{}]
    evs = await events(client, tid)
    [finished] = [e for e in evs if isinstance(e, ToolFinished)]
    assert finished.result.text == "ran T" and finished.note == "then run the tests"
    last = last_user_blocks(provider)
    assert [b["type"] for b in last] == ["tool_result", "text"]
    assert last[1]["text"] == "then run the tests"
    says = [i.body for e in evs if isinstance(e, InputTaken) for i in e.items
            if isinstance(i.body, Say)]
    assert len(says) == 1  # only the first prompt: no person message for the words


async def test_a_yes_without_words_adds_nothing(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("T", {})), reply("done")])
    rt = runtime(store, provider, agent([Asks()], [Fake("T")]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await answer_choice(client, tid, await asked(client, tid), "allow", "")
    await rt.run_until_idle()
    assert [b["type"] for b in last_user_blocks(provider)] == ["tool_result"]


class Crash(BaseException):
    """Stands in for the process dying."""


class DiesAtTheStart(SqliteStore):
    """Dies as the approved call starts: before the start commits
    (``before``) or right after."""

    def __init__(self, path: str, *, before: bool) -> None:
        super().__init__(path)
        self.before = before

    async def append(self, claim: Claim, events: Sequence[Event], *, expected_seq: int,
                     patch: Patch = Patch(), posts: Sequence[Post] = (),
                     creates: Sequence[NewTask] = ()) -> Appended:
        starts = any(isinstance(e, ToolStarted) for e in events)
        if starts and self.before:
            raise Crash()
        done = await super().append(claim, events, expected_seq=expected_seq, patch=patch,
                                   posts=posts, creates=creates)
        if starts:
            raise Crash()
        return done


@pytest.mark.parametrize("before", [True, False])
async def test_the_words_survive_a_crash(tmp_path: Path, before: bool) -> None:
    """Taken from the log by the next claimer: the call then runs (the crash
    came before it started) or is answered "outcome unknown" (after), and
    the words follow its result either way."""
    path = str(tmp_path / "t.db")
    tool = Fake("T")
    provider = ScriptedProvider([reply("", ("T", {})), reply("done")])
    dying = DiesAtTheStart(path, before=before)
    client = Client(dying)
    tid = await client.create("a", "go")
    await runtime(dying, provider, agent([Asks()], [tool])).run_until_idle()
    await answer_choice(client, tid, await asked(client, tid), "allow", "careful")
    with pytest.raises(Crash):
        await runtime(dying, provider, agent([Asks()], [tool])).run_until_idle()
    await dying.close()

    store = SqliteStore(path)
    try:
        def expire(conn: Any) -> None:
            conn.execute("UPDATE tasks SET lease_until = 0 WHERE task_id = ?", (tid,))

        await store._run(expire)
        await runtime(store, provider, agent([Asks()], [tool])).run_until_idle()
        [finished] = [e for e in await events(Client(store), tid)
                      if isinstance(e, ToolFinished)]
        assert finished.note == "careful"
        if before:
            assert tool.runs == [{}] and finished.result.text == "ran T"
        else:
            assert tool.runs == [] and finished.result.text == texts.INTERRUPTED_CALL_ERROR
        last = last_user_blocks(provider)
        assert [b["type"] for b in last] == ["tool_result", "text"]
        assert last[1]["text"] == "careful"
    finally:
        await store.close()


class Spawn:
    name = "Spawn"
    description = "spawn"
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.spawn is not None
        return ToolOutput(park=Park(on="children", keys=[ctx.spawn("child", "work")]))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        return text_output("child done")


async def test_a_sub_agents_call_answered_at_the_root_gets_the_words(
        store: SqliteStore) -> None:
    tool = Fake("T")
    provider = ScriptedProvider([reply("", ("Spawn", {})), reply("", ("T", {})),
                                 reply("did it"), reply("all done")])
    parent = agent([], [Spawn()])
    child = agent([Asks()], [tool], name="child", one_shot=True)
    rt = runtime(store, provider, parent, child)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [item] = await pending_tree(client, tid)
    assert item.task_id != tid
    await answer_choice(client, item.task_id, item.request_id, "allow", "keep it short")
    await rt.run_until_idle()
    assert tool.runs == [{}]
    [finished] = [e for e in await events(client, item.task_id)
                  if isinstance(e, ToolFinished)]
    assert finished.note == "keep it short"
    child_request = provider.requests[2].messages
    answered = [m for m in child_request if m.role == "user"][-1]
    assert [type(b).__name__ for b in answered.content] == ["ToolResult", "Text"]
    assert answered.text == "keep it short"
