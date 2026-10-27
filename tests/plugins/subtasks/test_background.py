"""Background sub-agents: the ack, the cap, the completion notice that wakes
an idle parent, and a background child that would need a human."""

from __future__ import annotations

from typing import Any

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import Fatal
from noeta.core.events import Recorded, TaskCreated, TaskEnded, ToolFinished
from noeta.core.extension import Ask, Extension, Gate, TaskView
from noeta.core.store.sqlite import SqliteStore
from noeta.core.types import ToolUse, Usage
from noeta.plugins.subtasks import SubagentSpec, Subtasks, texts
from noeta.plugins.subtasks.host import pending_requests, tree_usage
from noeta.testing import ScriptedProvider, reply
from tests.plugins.subtasks.conftest import Probe, events, runtime

ROSTER = [SubagentSpec(name="worker", description="Does work.")]


def parent(**kw: Any) -> Agent:
    return Agent(name="main", model="p", extensions=[Subtasks(ROSTER, **kw)])


class AskEverything(Extension):
    name = "ask"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Ask(prompt="may I?")


def worker(*extra: Extension) -> Agent:
    return Agent(name="worker", model="c", one_shot=True, tools=[Probe()],
                 extensions=[Subtasks(), *extra])


def bg(prompt: str) -> tuple[str, dict[str, Any]]:
    return ("Task", {"description": "d", "prompt": prompt, "subagent_type": "worker",
                     "background": True})


def notice(agent: str, result: str, child: str, status: str, failed: bool = False) -> str:
    head = (texts.BACKGROUND_FAILED if failed else texts.BACKGROUND_FINISHED).format(agent=agent)
    return f'{head}\n\n{result}\n\n<background-subagent id="{child}" status="{status}"/>'


async def test_background_child_acks_then_its_notice_wakes_the_idle_parent(
        store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", bg("research")), reply("started it"), reply("got it")])
    c = ScriptedProvider([reply("found X")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    created = (await events(client, child.task_id))[0]
    assert isinstance(created, TaskCreated) and created.config["unattended"] is True
    [ack] = [e.result for e in await events(client, tid) if isinstance(e, ToolFinished)]
    assert ack.text == texts.BACKGROUND_STARTED.format(agent="worker", id=child.task_id)
    assert ack.text.startswith('Background sub-agent "worker" started (id ')
    assert not ack.is_error
    assert len(p.requests) == 3
    last = p.requests[2].messages[-1]
    assert last.origin == "system"
    assert last.text == notice("worker", "found X", child.task_id, "completed")
    assert (await client.get(tid)).waiting_on == "input"


async def test_failed_background_child_reports_did_not_complete(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", bg("x")), reply("started"), reply("sorry")])
    c = ScriptedProvider([Fatal("boom")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    text = p.requests[2].messages[-1].text
    assert text == notice("worker", "llm_error: boom", child.task_id, "failed", failed=True)


async def test_background_child_that_needs_a_human_fails_with_the_reason(
        store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", bg("x")), reply("started"), reply("ok")])
    c = ScriptedProvider([reply("", ("Probe", {}))])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker(AskEverything()))
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    end = (await events(client, child.task_id))[-1]
    assert isinstance(end, TaskEnded) and end.status == "failed" and end.error == f"needs_human: {texts.BACKGROUND_NEEDED_HUMAN}"
    assert p.requests[2].messages[-1].text == notice(
        "worker", texts.BACKGROUND_NEEDED_HUMAN, child.task_id, "failed", failed=True)


async def test_background_flag_is_ignored_in_a_batch(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", bg("a"), bg("b")), reply("both back")])
    c = ScriptedProvider([reply("A"), reply("B")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    texts_back = sorted(e.result.text for e in await events(client, tid)
                        if isinstance(e, ToolFinished))
    assert texts_back == ["A", "B"]
    for row in await client.tasks(parent_id=tid):
        created = (await events(client, row.task_id))[0]
        assert "unattended" not in created.config


async def test_background_cap(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", bg("a")), reply("one running"),
                          reply("", bg("b")), reply("ok")])
    rt = runtime(store, {"p": p}, parent(max_background=1, queue="kids"), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    await client.send(tid, "another")
    await rt.run_until_idle()
    [_, second] = [e.result for e in await events(client, tid) if isinstance(e, ToolFinished)]
    assert second.is_error and second.text == (
        "background sub-agent not started: too many background sub-agents (1/1) running "
        "for this session; let one finish before starting another")
    assert len(await client.tasks(parent_id=tid)) == 1


class LoseNotices(Subtasks):
    """Takes the child's end but 'crashes' before posting the notice."""

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        return None


async def test_a_notice_lost_to_a_crash_is_posted_on_the_next_run(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", bg("x")), reply("started")])
    c = ScriptedProvider([reply("found")])
    lossy = Agent(name="main", model="p", extensions=[LoseNotices(ROSTER)])
    rt = runtime(store, {"p": p, "c": c}, lossy, worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    assert len(p.requests) == 2
    [child] = await client.tasks(parent_id=tid)
    p.add(reply("hi"), reply("thanks for the result"))
    await client.send(tid, "hello")
    await runtime(store, {"p": p, "c": c}, parent(), worker()).run_until_idle()
    assert p.requests[2].messages[-1].text == "hello"
    assert p.requests[3].messages[-1].text == notice("worker", "found", child.task_id,
                                                     "completed")


async def test_root_sees_child_approvals_and_usage(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", ("Task", {"description": "d", "prompt": "x",
                                              "subagent_type": "worker"}))])
    c = ScriptedProvider([reply("", ("Probe", {}))])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker(AskEverything()))
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    [pending] = await pending_requests(client, tid)
    assert pending.task_id == child.task_id and pending.on == "approval"
    assert pending.detail == {"prompt": "may I?"}
    assert await tree_usage(client, tid) == Usage(uncached=20, output=10)
    assert await tree_usage(client, tid, include_children=False) == Usage(uncached=10, output=5)
