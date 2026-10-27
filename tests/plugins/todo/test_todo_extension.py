"""TodoWrite: the durable list, 0.6's mixing rules as outcomes, and Claude
Code's todo reminder at its 10/10 cadence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.loop.state import fold
from noeta.core.runner import Runtime
from noeta.core.store.sqlite import SqliteStore
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, Text, Thinking, ToolUse, user
from noeta.plugins.todo import (
    ONCE_PER_RESPONSE,
    TODO_WRITE_ACK,
    Todo,
    reminder_due,
    todo_reminder_text,
    validate_todos,
)
from noeta.testing import ScriptedProvider, reply

ITEMS = [
    {"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"},
    {"content": "Fix bug", "status": "pending", "activeForm": "Fixing bug"},
]


class Noop:
    name = "Noop"
    description = "Does nothing."
    input_schema = {"type": "object"}
    read_only = True
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("ok")


class AskUserQuestion(Noop):
    name = "AskUserQuestion"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, provider: ScriptedProvider, agent: Agent) -> Runtime:
    return Runtime(store=store, agents={agent.name: agent}, provider_for=lambda _m: provider,
                   env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                   sleep=_no_sleep)


def _agent(**kw: Any) -> Agent:
    return Agent(name="a", model="m", tools=[Noop(), AskUserQuestion()],
                 extensions=[Todo(**kw)])


async def _results(client: Client, tid: str) -> list[tuple[str, bool]]:
    return [(r.event.result.text, r.event.result.is_error) for r in await client.events(tid)
            if isinstance(r.event, ToolFinished)]


async def _todo_state(client: Client, tid: str) -> Any:
    st = fold(await client.events(tid), {"todo": Todo().reduce}, {"todo": []})
    return st.ext["todo"]


async def test_todowrite_saves_the_list_and_acknowledges(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("TodoWrite", {"todos": ITEMS})), reply("done")])
    client = Client(store)
    tid = await client.create("a", "go")
    await _runtime(store, provider, _agent()).run_until_idle()
    assert await _results(client, tid) == [(TODO_WRITE_ACK, False)]
    assert await _todo_state(client, tid) == ITEMS
    [tool] = [t for t in provider.requests[0].tools if t.name == "TodoWrite"]
    assert tool.description.startswith("Use this tool to create and manage a structured task")
    assert tool.input_schema["properties"]["todos"]["items"]["required"] == [
        "content", "status", "activeForm"]


async def test_todowrite_beside_ordinary_tools_saves_and_they_run(store: SqliteStore) -> None:
    provider = ScriptedProvider([
        reply("", ("TodoWrite", {"todos": ITEMS}), ("Noop", {})), reply("done")])
    client = Client(store)
    tid = await client.create("a", "go")
    await _runtime(store, provider, _agent()).run_until_idle()
    assert await _results(client, tid) == [(TODO_WRITE_ACK, False), ("ok", False)]
    assert await _todo_state(client, tid) == ITEMS


async def test_two_todowrites_in_one_response_run_nothing(store: SqliteStore) -> None:
    provider = ScriptedProvider([
        reply("", ("TodoWrite", {"todos": ITEMS}), ("TodoWrite", {"todos": []}),
              ("Noop", {})),
        reply("done")])
    client = Client(store)
    tid = await client.create("a", "go")
    await _runtime(store, provider, _agent()).run_until_idle()
    assert await _results(client, tid) == [(ONCE_PER_RESPONSE, True)] * 3
    assert await _todo_state(client, tid) == []


async def test_an_invalid_list_fails_every_call_of_the_response(store: SqliteStore) -> None:
    bad = [{"content": "x", "status": "done", "activeForm": "x"}]
    provider = ScriptedProvider([
        reply("", ("TodoWrite", {"todos": bad}), ("Noop", {})), reply("done")])
    client = Client(store)
    tid = await client.create("a", "go")
    await _runtime(store, provider, _agent()).run_until_idle()
    error = "todo status must be one of pending, in_progress, completed"
    assert await _results(client, tid) == [(error, True), (error, True)]


async def test_todowrite_cannot_share_a_response_with_ask_user(store: SqliteStore) -> None:
    provider = ScriptedProvider([
        reply("", ("TodoWrite", {"todos": ITEMS}), ("AskUserQuestion", {})), reply("done")])
    client = Client(store)
    tid = await client.create("a", "go")
    await _runtime(store, provider, _agent()).run_until_idle()
    text = ("Nothing in this response ran: TodoWrite cannot share a response with "
            "AskUserQuestion, and the checklist was not saved. Re-issue the other calls "
            "in a separate response.")
    assert await _results(client, tid) == [(text, True), (text, True)]
    assert await _todo_state(client, tid) == []


def test_validation_texts() -> None:
    assert validate_todos({"todos": "x"}) == (False, "todos must be a list")
    assert validate_todos({"todos": [{}] * 51}) == (False, "too many todos (max 50)")
    assert validate_todos({"todos": [1]}) == (False, "each todo must be an object")
    long = {"content": "x" * 501, "status": "pending", "activeForm": "x"}
    assert validate_todos({"todos": [long]}) == (False, "todo content too long (max 500)")
    no_form = {"content": "x", "status": "pending", "activeForm": ""}
    assert validate_todos({"todos": [no_form]}) == (
        False, "each todo needs non-empty string activeForm")


def test_reminder_text_is_claude_codes() -> None:
    assert todo_reminder_text([]).endswith("ignore if not applicable.\n")
    assert todo_reminder_text(ITEMS).endswith(
        "\nHere are the existing contents of your todo list:\n"
        "[1. [in_progress] Run tests\n2. [pending] Fix bug]")


def _assistant(*blocks: Any) -> Message:
    return Message(role="assistant", content=list(blocks))


def test_reminder_due_counts_blocks_and_skips_thinking() -> None:
    use = ToolUse(id="1", name="Noop", input={})
    nine = [_assistant(Thinking(text="t"), use)] * 9
    assert not reminder_due(nine)
    assert reminder_due([*nine, _assistant(Text(text="x"))])
    # A TodoWrite resets the "since write" count.
    todo = _assistant(ToolUse(id="2", name="TodoWrite", input={}))
    assert not reminder_due([*nine, todo, *nine])
    # A recorded reminder resets the "since reminder" count.
    reminder = user(todo_reminder_text([]), origin="system")
    assert not reminder_due([*nine, _assistant(use), reminder, *nine])
    assert reminder_due([*nine, _assistant(use), reminder, *nine, _assistant(use)])


async def test_reminder_lands_at_the_cadence_and_keeps_its_bytes(store: SqliteStore) -> None:
    steps = [reply("", ("TodoWrite", {"todos": ITEMS}))]
    steps += [reply("", ("Noop", {})) for _ in range(10)]
    steps += [reply("done")]
    provider = ScriptedProvider(steps)
    client = Client(store)
    await client.create("a", "go")
    await _runtime(store, provider, _agent()).run_until_idle()
    # Requests 0..10 carry no reminder; the 12th (index 11) follows ten
    # Noop blocks since the TodoWrite and gets it, with the list.
    for request in provider.requests[:11]:
        assert not any(m.text.startswith("The TodoWrite tool hasn't") for m in request.messages)
    last = provider.requests[11]
    assert last.messages[-1].origin == "system"
    assert last.messages[-1].text == todo_reminder_text(ITEMS)
    assert last.cache_after[-1] == len(last.messages) - 1


async def test_reminder_is_recorded_where_it_was_sent(store: SqliteStore) -> None:
    steps = [reply("", ("Noop", {})) for _ in range(11)] + [reply("done")]
    provider = ScriptedProvider(steps)
    client = Client(store)
    tid = await client.create("a", "go")
    await _runtime(store, provider, _agent()).run_until_idle()
    sent = provider.requests[10].messages
    later = provider.requests[11].messages
    assert sent[-1].text == todo_reminder_text([])
    assert later[: len(sent)] == sent
    st = fold(await client.events(tid), {}, {})
    assert sum(m.text == todo_reminder_text([]) for m in st.messages) == 1


async def test_reminder_can_be_turned_off(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Noop", {})) for _ in range(11)] + [reply("ok")])
    client = Client(store)
    await client.create("a", "go")
    await _runtime(store, provider, _agent(reminder=False)).run_until_idle()
    assert not any(m.origin == "system" for r in provider.requests for m in r.messages)
