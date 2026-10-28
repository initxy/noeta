"""``SendMessage(notify_when_idle=true)``: one notice when another open
session next goes idle or ends, its refusals, and the expiry."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.types import ModelRequest
from noeta.plugins.subtasks import SubagentSpec, Subtasks, idle, texts
from noeta.plugins.subtasks.tool import SendMessageTool
from noeta.testing import ScriptedProvider, reply
from tests.plugins.subtasks.conftest import events, runtime

ROSTER = [SubagentSpec(name="general-purpose", description="Does work.", agent="worker")]


@pytest.fixture(autouse=True)
def _send_message_loaded() -> Iterator[None]:
    SendMessageTool.deferred = False  # type: ignore[misc]
    yield
    SendMessageTool.deferred = True  # type: ignore[misc]


def main() -> Agent:
    return Agent(name="main", model="p", extensions=[Subtasks(ROSTER, queue="kids")])


def other(*, subtasks: bool = True) -> Agent:
    return Agent(name="other", model="o", extensions=[Subtasks()] if subtasks else [])


def notify(to: str, message: str | None = None) -> tuple[str, dict[str, Any]]:
    args: dict[str, Any] = {"to": to, "notify_when_idle": True}
    if message is not None:
        args["message"] = message
    return ("SendMessage", args)


async def results(client: Client, tid: str) -> list[tuple[str, bool]]:
    return [(e.result.text, e.result.is_error) for e in await events(client, tid)
            if isinstance(e, ToolFinished)]


def notices(request: ModelRequest) -> list[str]:
    return [m.text for m in request.messages
            if m.origin == "system" and "[Cross-session idle notice]" in m.text]


def test_schema_offers_the_flag_and_makes_the_message_optional() -> None:
    schema = SendMessageTool.input_schema
    assert schema["required"] == ["to"]
    assert schema["properties"]["notify_when_idle"]["type"] == "boolean"


async def test_a_bare_subscription_to_an_idle_session_answers_at_once(
        store: SqliteStore) -> None:
    o = ScriptedProvider([reply("heard you\nmore")])
    p = ScriptedProvider()
    rt = runtime(store, {"p": p, "o": o}, main(), other())
    client = Client(store)
    peer = await client.create("other", "hello")
    await rt.run_until_idle()
    p.add(reply("", notify(peer)), reply("waiting"), reply("it is idle"))
    tid = await client.create("main", "tell me when it is idle")
    await rt.run_until_idle()
    [(text, failed)] = await results(client, tid)
    assert not failed and text == texts.IDLE_SUBSCRIBED.format(to=peer)
    assert len(o.requests) == 1  # a bare subscription costs the other session no turn
    [notice] = notices(p.requests[2])
    assert notice.startswith(f'[Cross-session idle notice] Session "{peer}"')
    assert "«heard you»" in notice and texts.IDLE_SOURCE_PEER in notice
    # One-shot: the other session's next turn sends nothing more.
    o.add(reply("again"))
    await client.send(peer, "still there?")
    await rt.run_until_idle()
    assert len(p.requests) == 3


async def test_a_message_goes_now_and_the_notice_follows_its_turn(store: SqliteStore) -> None:
    o = ScriptedProvider([reply("hi")])
    p = ScriptedProvider()
    rt = runtime(store, {"p": p, "o": o}, main(), other())
    client = Client(store)
    peer = await client.create("other", "hello")
    await rt.run_until_idle()
    o.add(reply("did the thing"))
    p.add(reply("", notify(peer, "please do the thing")), reply("asked"), reply("done there"))
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [(text, _)] = await results(client, tid)
    assert text == "\n".join([texts.SENT.format(to=peer), texts.IDLE_SUBSCRIBED.format(to=peer)])
    assert "please do the thing" in o.requests[1].messages[-1].text
    [notice] = notices(p.requests[2])
    assert "finished its turn (at " in notice and "«did the thing»" in notice


async def test_a_session_cancelled_before_going_idle_sends_an_exited_notice(
        store: SqliteStore) -> None:
    p = ScriptedProvider()
    rt = runtime(store, {"p": p}, main(), other())
    client = Client(store)
    peer = await client.create("other", "hello", queue="slow")
    p.add(reply("", notify(peer)), reply("waiting"), reply("it ended"))
    await client.create("main", "go")
    await rt.run_until_idle()
    await client.cancel(peer)
    # The other session takes the subscription and the cancel together.
    await runtime(store, {"o": ScriptedProvider()}, other(), queues=("slow",)).run_until_idle()
    await rt.run_until_idle()
    [notice] = notices(p.requests[-1])
    assert notice.startswith(f'[Cross-session idle notice] Session "{peer}" ended at ')


async def test_refusals(store: SqliteStore) -> None:
    o = ScriptedProvider([reply("hi")])
    p = ScriptedProvider([reply("", ("Agent", {"description": "d", "prompt": "x"})),
                          reply("launched")])
    rt = runtime(store, {"p": p, "o": o}, main(), other())
    client = Client(store)
    peer = await client.create("other", "hello")
    await rt.run_until_idle()
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    p.add(reply("", notify(child.task_id, "hi"), notify("main"), notify(tid),
                notify("nobody"), ("SendMessage", {"to": peer})), reply("ok"))
    await client.send(tid, "more")
    await rt.run_until_idle()
    out = (await results(client, tid))[1:]
    assert out == [
        (texts.IDLE_ROOT_TASKS_ONLY + texts.IDLE_NOT_SENT, True),
        (texts.IDLE_ROOT_TASKS_ONLY, True),
        (texts.IDLE_SELF.format(to=tid), True),
        (texts.UNKNOWN_RECIPIENT.format(to="nobody"), True),
        (texts.EMPTY_MESSAGE, True)]
    assert not [m for m in o.requests[-1].messages if "hi" == m.text]


async def test_a_sub_agent_cannot_subscribe(store: SqliteStore) -> None:
    c = ScriptedProvider([reply("", notify("someone", "hello")), reply("ok")])
    p = ScriptedProvider([reply("", ("Agent", {"description": "d", "prompt": "x"})),
                          reply("launched"), reply("fine")])
    worker = Agent(name="worker", model="c", extensions=[Subtasks()])
    rt = runtime(store, {"p": p, "c": c},
                 Agent(name="main", model="p", extensions=[Subtasks(ROSTER)]), worker)
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    [(text, failed)] = await results(client, child.task_id)
    assert failed and text == texts.IDLE_MAIN_ONLY + texts.IDLE_NOT_SENT


async def test_a_subscription_nobody_answers_expires(store: SqliteStore,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(idle, "EXPIRY_SECONDS", -1)
    o = ScriptedProvider([reply("hi")])
    p = ScriptedProvider()
    rt = runtime(store, {"p": p, "o": o}, main(), other(subtasks=False))
    client = Client(store)
    peer = await client.create("other", "hello")
    await rt.run_until_idle()
    p.add(reply("", notify(peer)), reply("waiting"), reply("gave up"))
    await client.create("main", "go")
    await rt.run_until_idle()
    [notice] = notices(p.requests[2])
    assert "12 hours went by with no report" in notice
    assert notice.startswith("[Cross-session idle notice] 12 hours went by with no report "
                             f'from session "{peer}"')
    assert texts.IDLE_SOURCE_OWN in notice
