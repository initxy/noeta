"""The Agent tool and its companions as Claude Code has them: background
launch, the sub-agent's SubagentHandback, the agent-message and the
completion notice, SendMessage (resume, steer, reply), ListAgents, forks,
worktrees and the refusals."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import Fatal
from noeta.core.events import Recorded, TaskCreated, TaskEnded, ToolFinished
from noeta.core.extension import Ask, Extension, Gate, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext
from noeta.core.types import Message, ModelRequest, ToolUse, Usage
from noeta.host.pending import pending_tree
from noeta.host.usage import tree_usage
from noeta.plugins.shell import Registry, Shell
from noeta.plugins.subtasks import SubagentSpec, Subtasks, texts
from noeta.plugins.subtasks import extension as subtasks_extension
from noeta.plugins.subtasks.tool import (
    LIST_WINDOW,
    TRANSCRIPT_KEY,
    AgentTool,
    ListAgentsTool,
    SendMessageTool,
    agent_message,
    agent_schema,
    handback_sender,
)
from noeta.plugins.subtasks.worktree import make_agent_worktree
from noeta.plugins.tool_search import ToolSearch
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.notices import NOTICE_GUARD
from tests.plugins.subtasks.conftest import Probe, events, runtime

ROSTER = [
    SubagentSpec(name="general-purpose", description="Does work.", agent="worker"),
    SubagentSpec(name="scout", description="Looks around.", agent="worker"),
]


@pytest.fixture(autouse=True)
def _send_message_loaded() -> Iterator[None]:
    """These tests are about SendMessage itself, so it is offered loaded;
    ``test_send_message_is_deferred`` covers the deferral."""
    SendMessageTool.deferred = False  # type: ignore[misc]
    yield
    SendMessageTool.deferred = True  # type: ignore[misc]


def parent(*extra: Extension, **kw: Any) -> Agent:
    tools = kw.pop("tools", ())
    return Agent(name="main", model="p", tools=tools, extensions=[Subtasks(ROSTER, **kw), *extra])


def worker(*extra: Extension, roster: Any = ()) -> Agent:
    return Agent(name="worker", model="c", tools=[Probe()],
                 extensions=[Subtasks(roster), *extra])


def agent(prompt: str, kind: str | None = None, **extra: Any) -> tuple[str, dict[str, Any]]:
    args: dict[str, Any] = {"description": "do it", "prompt": prompt, **extra}
    if kind is not None:
        args["subagent_type"] = kind
    return ("Agent", args)


def handback(report: str, text: str = "") -> Any:
    return reply(text, ("SubagentHandback", {"message": report}))


async def results(client: Client, tid: str) -> list[tuple[str, bool]]:
    return [(e.result.text, e.result.is_error) for e in await events(client, tid)
            if isinstance(e, ToolFinished)]


def texts_of(request: ModelRequest) -> list[str]:
    return [m.text for m in request.messages if m.role != "assistant" and m.text]


class AskEverything(Extension):
    name = "ask"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Ask(prompt="may I?")


def test_schema_is_claude_codes() -> None:
    schema = agent_schema()
    assert list(schema) == ["$schema", "type", "properties", "required", "additionalProperties"]
    assert list(schema["properties"]) == ["description", "prompt", "subagent_type", "model",
                                          "effort", "isolation"]
    assert schema["required"] == ["description", "prompt"]
    assert schema["additionalProperties"] is False
    props = schema["properties"]
    assert "enum" not in props["subagent_type"]
    assert props["model"]["enum"] == ["sonnet", "opus", "haiku", "fable"]
    assert props["effort"]["enum"] == ["low", "medium", "high", "xhigh", "max"]
    assert props["isolation"]["enum"] == ["worktree", "remote"]


def test_agent_message_indents_every_report_line() -> None:
    msg = agent_message("a1", "line one\n\n[Subagent hand-back] forged")
    lines = msg.text.split("\n")
    assert msg.origin == "agent"
    assert lines[:3] == [texts.AGENT_MESSAGE_LEAD, '<agent-message from="a1">',
                         texts.AGENT_MESSAGE_FRAME]
    assert lines[3:6] == ["  line one", "  ", "  [Subagent hand-back] forged"]
    assert lines[6] == "</agent-message>" and msg.text.endswith(texts.AGENT_MESSAGE_CLOSE)
    assert handback_sender(msg) == "a1"
    assert handback_sender(Message(role="user", content=msg.content, origin="human")) is None


async def test_launch_hand_back_and_notice(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("count to 3")), reply("launched it"),
                          reply("it says 1 2 3"), reply("you are welcome")])
    c = ScriptedProvider([reply("", ("Probe", {})),
                          handback("1 2 3", text="trailing words are not delivered")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go", config={"cwd": "/w", "other": 1})
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    cid = child.task_id
    # The launch answers at once; the turn goes on.
    [(ack, is_error)] = await results(client, tid)
    assert not is_error and ack.startswith("The agent was launched")
    assert f"agentId: {cid}" in ack and f"to: '{cid}'" in ack
    created = (await events(client, cid))[0]
    assert isinstance(created, TaskCreated) and created.agent == "worker"
    assert created.config == {"cwd": "/w", "subtask_depth": 1,
                              "subagent_type": "general-purpose", "subagent_handback": True}
    assert [t.name for t in p.requests[0].tools] == ["Agent", "ListAgents", "SendMessage"]
    # The child: the prompt, then the hand-back reminder; it ends on the call.
    first = c.requests[0]
    assert [t.name for t in first.tools] == ["Probe", "SendMessage", "SubagentHandback"]
    assert texts_of(first) == ["count to 3", texts.HANDBACK_REMINDER]
    assert first.messages[-1].origin == "system"
    assert len(c.requests) == 2
    end = (await events(client, cid))[-1]
    assert isinstance(end, TaskEnded) and end.answer == "1 2 3"
    # The report opens a parent turn of its own, without the notice.
    report = p.requests[2].messages[-1]
    assert report.origin == "agent" and handback_sender(report) == cid
    assert "  1 2 3" in report.text and "trailing words" not in report.text
    assert all("<task-notification>" not in t for t in texts_of(p.requests[2]))
    # The notice rides along with the next prompt, after the tools-used note.
    await client.send(tid, "thanks")
    await rt.run_until_idle()
    msgs = p.requests[3].messages
    assert msgs[-3].text == (
        "<tools-used-before-summary>SubagentHandback</tools-used-before-summary>")
    notice = msgs[-2].text
    assert msgs[-2].origin == "system" and msgs[-1].text == "thanks"
    assert notice.startswith(NOTICE_GUARD)
    for part in (f"<task-id>{cid}</task-id>", "<status>completed</status>",
                 '<summary>Agent "do it" finished</summary>',
                 f"<result>{texts.RESULT_POINTER.format(id=cid)}</result>",
                 # The newest reply's input plus what both replies wrote.
                 "<usage><subagent_tokens>20</subagent_tokens><tool_uses>2</tool_uses>"):
        assert part in notice
    call_id = p.requests[1].messages[-2].tool_uses[0].id
    assert f"<tool-use-id>{call_id}</tool-use-id>" in notice


async def test_an_agent_that_should_hand_back_and_did_not_delivers_no_report(
        store: SqliteStore) -> None:
    # Offered SubagentHandback, it ended with a plain reply instead: as in
    # Claude Code, no report was delivered, its text is not passed on, and
    # the caller is told how to ask for the report.
    p = ScriptedProvider([reply("", agent("x", "scout")), reply("launched"), reply("hm")])
    c = ScriptedProvider([reply("my final words")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    assert "SubagentHandback" in [t.name for t in c.requests[0].tools]
    notice = p.requests[2].messages[-1]
    no_report = texts.RESULT_NO_REPORT.format(hint=texts.RESULT_NO_REPORT_HINT)
    assert notice.origin == "system" and f"<result>{no_report}</result>" in notice.text
    assert "my final words" not in notice.text
    assert (await client.get(tid)).waiting_on == "input"


async def test_explore_and_plan_get_no_hint_to_ask_for_the_report(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("x", "Explore")), reply("launched"), reply("hm")])
    c = ScriptedProvider([reply("my final words")])
    roster = [*ROSTER, SubagentSpec(name="Explore", description="Looks.", agent="worker")]
    main = Agent(name="main", model="p", extensions=[Subtasks(roster)])
    rt = runtime(store, {"p": p, "c": c}, main, worker())
    client = Client(store)
    await client.create("main", "go")
    await rt.run_until_idle()
    notice = p.requests[2].messages[-1].text
    assert f"<result>{texts.RESULT_NO_REPORT.format(hint='')}</result>" in notice


class DefaultMode(Extension):
    """Passes a non-auto permission mode down, as the permission plugin does."""

    name = "mode"

    def inherit(self, state: Any) -> dict[str, Any]:
        return {"permission_mode": "default"}


async def test_outside_auto_mode_the_last_reply_is_the_report(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("x", "scout")), reply("launched"), reply("ok")])
    c = ScriptedProvider([reply("made the file")])
    rt = runtime(store, {"p": p, "c": c}, parent(DefaultMode()), worker())
    client = Client(store)
    await client.create("main", "go")
    await rt.run_until_idle()
    child = c.requests[0]
    assert "SubagentHandback" not in [t.name for t in child.tools]
    assert not any("SubagentHandback" in t for t in texts_of(child))
    notice = p.requests[2].messages[-1].text
    # Claude Code's form: the reply as it is, no newline added.
    assert "<result>made the file</result>" in notice


async def test_a_child_without_the_handback_tool_reports_its_last_reply(
        store: SqliteStore) -> None:
    # In auto mode, but the child's agent definition has no subtasks plugin:
    # it cannot hand back, so its final message is its report (Claude Code's).
    p = ScriptedProvider([reply("", agent("x", "scout")), reply("launched"), reply("ok")])
    c = ScriptedProvider([reply("made the file")])
    bare = Agent(name="worker", model="c", tools=[Probe()])
    rt = runtime(store, {"p": p, "c": c}, parent(), bare)
    client = Client(store)
    await client.create("main", "go")
    await rt.run_until_idle()
    assert "SubagentHandback" not in [t.name for t in c.requests[0].tools]
    notice = p.requests[2].messages[-1].text
    assert "<result>made the file</result>" in notice
    assert "no report was delivered" not in notice


async def test_a_child_whose_tools_leave_out_the_handback_reports_its_last_reply(
        store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("x", "scout")), reply("launched"), reply("ok")])
    c = ScriptedProvider([reply("made the file")])
    narrow = Agent(name="worker", model="c", tools=[Probe()], extensions=[Subtasks(())],
                   allowed_tools=["Probe"])
    rt = runtime(store, {"p": p, "c": c}, parent(), narrow)
    client = Client(store)
    await client.create("main", "go")
    await rt.run_until_idle()
    assert not any("SubagentHandback" in t for t in texts_of(c.requests[0]))
    assert "<result>made the file</result>" in p.requests[2].messages[-1].text


async def test_failed_agent_notice_wakes_the_parent(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("x")), reply("launched"), reply("sorry")])
    c = ScriptedProvider([Fatal("boom")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    await client.create("main", "go")
    await rt.run_until_idle()
    text = p.requests[2].messages[-1].text
    assert "<status>failed</status>" in text
    assert '<summary>Agent "do it" failed: llm_error: boom</summary>' in text


async def test_send_message_resumes_a_finished_agent(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("find X")), reply("launched"), reply("noted")])
    c = ScriptedProvider([handback("X is here"), handback("Y is there")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [first] = await client.tasks(parent_id=tid)
    p.add(reply("", ("SendMessage", {"to": first.task_id, "message": "now find Y",
                                     "summary": "ask for Y"})), reply("asked"), reply("got Y"))
    await client.send(tid, "and Y?")
    await rt.run_until_idle()
    rows = await client.tasks(parent_id=tid)
    assert len(rows) == 2
    second = next(r for r in rows if r.task_id != first.task_id)
    created = (await events(client, second.task_id))[0]
    assert isinstance(created, TaskCreated) and created.history_from == first.task_id
    assert created.config["agent_id"] == first.task_id
    resumed = c.requests[1]
    assert texts_of(resumed)[:2] == ["find X", texts.HANDBACK_REMINDER]
    assert texts.CALLER_MESSAGE.split("\n")[0] in texts_of(resumed)[-1]
    assert "now find Y" in texts_of(resumed)[-1]
    [(sent, _)] = [r for r in await results(client, tid) if "resumed" in r[0]]
    assert sent == texts.RESUMED.format(to=first.task_id)
    # The second report comes from the same agent id.
    assert handback_sender(p.requests[5].messages[-1]) == first.task_id


async def test_send_message_reaches_a_running_agent_and_its_caller(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("slow")), reply("launched")])
    rt = runtime(store, {"p": p}, parent(queue="kids"), worker())  # the child is not run here
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    p.add(reply("", ("SendMessage", {"to": child.task_id, "message": "hurry"})), reply("sent"))
    await client.send(tid, "tell it to hurry")
    await rt.run_until_idle()
    assert (texts.SENT.format(to=child.task_id), False) in await results(client, tid)
    c = ScriptedProvider([reply("", ("SendMessage", {"to": "main", "message": "almost"})),
                          handback("done")])
    p.add(reply("ok"), reply("great"))
    kids = Runtime(store=store, agents={"worker": worker()}, provider_for=lambda _m: c,  # type: ignore[arg-type,return-value]
                   env_for=lambda _s: None, queues=("kids",))  # type: ignore[arg-type,return-value]
    await kids.run_until_idle()
    assert any("hurry" in t for t in texts_of(c.requests[0]))
    await rt.run_until_idle()
    said = [m for r in p.requests for m in r.messages if m.origin == "agent"]
    assert any(texts.AGENT_NOTE_FRAME in m.text and "  almost" in m.text for m in said)


async def test_send_message_refusals_and_other_tasks(store: SqliteStore) -> None:
    other_p = ScriptedProvider([reply("heard you")])
    p = ScriptedProvider([
        reply("", ("SendMessage", {"to": "nobody", "message": "hi"}),
              ("SendMessage", {"to": "main", "message": "hi"}),
              ("SendMessage", {"to": "x", "message": " "}),
              ("SendMessage", {"to": "*", "message": "all of you"})),
        reply("ok")])
    rt = runtime(store, {"p": p, "o": other_p}, parent(),
                 Agent(name="other", model="o"))
    client = Client(store)
    other = await client.create("other", "hello")
    await rt.run_until_idle()
    other_p.add(reply("got it"))
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    assert await results(client, tid) == [
        (texts.UNKNOWN_RECIPIENT.format(to="nobody"), True), (texts.NO_CALLER, True),
        (texts.EMPTY_MESSAGE, True), (texts.NO_BROADCAST, True)]
    p.add(reply("", ("SendMessage", {"to": other, "message": "ping"}), ("ListAgents", {})),
          reply("done"))
    await client.send(tid, "ping the other one")
    await rt.run_until_idle()
    out = await results(client, tid)
    assert out[4] == (texts.SENT.format(to=other), False)
    assert out[5][0].startswith(texts.LIST_HEAD) and f"- {other} — session" in out[5][0]
    got = other_p.requests[-1].messages[-1]
    assert got.origin == "agent" and '<cross-session-message from="' in got.text


async def test_list_agents_says_when_it_looked_at_the_newest_tasks_only(
        store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole listing, not the runner's cut at ListAgents' result limit.
    monkeypatch.setattr(ListAgentsTool, "max_result_chars", None)
    client = Client(store)
    for _ in range(LIST_WINDOW + 1):  # sessions no worker here runs
        await client.create("worker", "later", queue="elsewhere")
    p = ScriptedProvider([reply("", ("ListAgents", {})), reply("listed")])
    rt = runtime(store, {"p": p}, parent(), worker())
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [(listed, _)] = await results(client, tid)
    assert listed.count("— session") == LIST_WINDOW - 1
    assert listed.endswith("\n" + texts.LIST_WINDOW_FULL.format(n=LIST_WINDOW))


async def test_an_agent_mention_asks_the_model_to_start_that_agent(
        store: SqliteStore) -> None:
    from noeta.plugins.subtasks.mentions import AGENT_MENTION

    p = ScriptedProvider([reply("on it"), reply("ok")])
    rt = runtime(store, {"p": p}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", 'ask @agent-scout and @agent-nobody, and @"scout (agent)"')
    await rt.run_until_idle()
    note = AGENT_MENTION.format(type="scout")
    assert [m.text for m in p.requests[0].messages].count(note) == 1
    assert not any('"nobody"' in m.text for m in p.requests[0].messages)
    await client.send(tid, "thanks")
    await rt.run_until_idle()
    assert [m.text for m in p.requests[1].messages].count(note) == 1  # not again


async def test_an_agent_mention_is_noted_once_per_prompt(store: SqliteStore) -> None:
    """Not again when the call is made again (an empty reply asked to go
    on), nor after a turn that ended without a reply."""
    from noeta.plugins.subtasks.mentions import AGENT_MENTION

    p = ScriptedProvider([reply(""), reply(""), reply("ok")])
    rt = runtime(store, {"p": p}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "ask @agent-scout please")
    await rt.run_until_idle()
    await client.send(tid, "and now?")
    await rt.run_until_idle()
    note = AGENT_MENTION.format(type="scout")
    assert [[m.text for m in r.messages].count(note) for r in p.requests] == [1, 1, 1]


async def test_an_agent_mention_in_a_forks_prompt_is_noted(store: SqliteStore) -> None:
    """A task continuing another's conversation counts its own prompts: the
    source's count is a seq of the source's log."""
    from noeta.plugins.subtasks.mentions import AGENT_MENTION

    p = ScriptedProvider([reply("", ("ListAgents", {})), reply("", ("ListAgents", {})),
                          reply("on it"), reply("forked")])
    rt = runtime(store, {"p": p}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "ask @agent-scout please")
    await rt.run_until_idle()
    await client.create("main", "now @agent-scout again", history_from=tid)
    await rt.run_until_idle()
    note = AGENT_MENTION.format(type="scout")
    texts_ = [m.text for m in p.requests[-1].messages]
    at = next(i for i, t in enumerate(texts_) if "now @agent-scout again" in t)
    assert any(note in t for t in texts_[at:])


async def test_list_agents_shows_launched_agents(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", ("ListAgents", {})), reply("", agent("dig", "scout")),
                          reply("", ("ListAgents", {})), reply("listed")])
    rt = runtime(store, {"p": p}, parent(queue="kids"), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    empty, _, listed = await results(client, tid)
    assert empty == (texts.LIST_EMPTY, False)
    assert listed[0] == f'{texts.LIST_HEAD}\n- {child.task_id} — sub-agent (scout) "do it", running'


async def test_refusals(store: SqliteStore) -> None:
    probe = Probe()
    p = ScriptedProvider([
        reply("", agent("  "), ("Probe", {}), agent("fine", "scout")),
        reply("", agent("x", model="gpt")),
        reply("", agent("x", "nobody"), ("Probe", {})),
        reply("", agent("x", isolation="remote")),
        reply("", ("Agent", {"description": "d"})),
        reply("ok")])
    rt = runtime(store, {"p": p}, parent(tools=[probe], queue="kids"), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    out = await results(client, tid)
    # A bad Agent call is refused by itself: the reply's other calls run.
    assert out[0] == (texts.EMPTY_PROMPT, True)
    assert out[1] == ("seen", False) and out[2][0].startswith("The agent was launched")
    assert out[3] == (texts.BAD_CHOICE.format(
        field="model", choices="sonnet, opus, haiku, fable", value="gpt"), True)
    unknown = texts.SPAWN_DENIED.format(reason=texts.UNKNOWN_AGENT.format(
        name="nobody", available="fork, general-purpose, scout"))
    assert out[4:6] == [(unknown, True), ("seen", False)]
    assert out[6] == (texts.SPAWN_DENIED.format(reason=texts.NO_REMOTE), True)
    assert out[7] == (texts.MISSING_FIELDS, True)
    assert len(await client.tasks(parent_id=tid)) == 1


async def test_depth_cap_and_running_cap(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("a")), reply("ok"),
                          reply("", agent("a"), agent("b"), agent("c")), reply("ok"),
                          reply("", agent("a"), agent("b")), reply("ok")])
    rt = runtime(store, {"p": p}, parent(queue="kids", max_background=1),
                 worker())
    client = Client(store)
    tid = await client.create("main", "go", config={"subtask_depth": 3})
    await rt.run_until_idle()
    [(depth, _)] = await results(client, tid)
    assert depth == texts.SPAWN_DENIED.format(reason=texts.DEPTH_REACHED.format(depth=3, n=3))
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    await client.send(tid, "more")
    await rt.run_until_idle()
    out = await results(client, tid)
    # No cap on the Agent calls of one reply (Claude Code has none): only
    # the running agents are counted.
    assert out == [(texts.TOO_MANY_BACKGROUND.format(limit=1), True)] * 5


async def test_agent_calls_of_one_reply_count_once_against_the_cap(
        store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("a"), agent("b")), reply("ok")])
    rt = runtime(store, {"p": p}, parent(queue="kids", max_background=2), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    out = await results(client, tid)
    assert [failed for _, failed in out] == [False, False]
    assert len(await client.tasks(parent_id=tid)) == 2


async def test_task_is_the_old_name_and_exclusive_tools(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", ("Task", {"description": "d", "prompt": "old name",
                                              "subagent_type": "scout"})), reply("ok")])
    rt = runtime(store, {"p": p}, parent(queue="kids"), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [(ack, failed)] = await results(client, tid)
    assert not failed
    assert ack.startswith("The agent was launched")
    probe = Probe()
    p2 = ScriptedProvider([reply("", agent("a"), ("Probe", {})), reply("ok")])
    rt2 = runtime(store, {"p": p2}, parent(tools=[probe], exclusive_with={"Probe"}), worker())
    tid2 = await client.create("main", "go")
    await rt2.run_until_idle()
    text = texts.EXCLUSIVE.format(name="Probe")
    assert await results(client, tid2) == [(text, True), (text, True)]
    assert probe.runs == 0


async def test_model_and_effort_precedence(store: SqliteStore) -> None:
    def defined(model: str | None) -> Agent:
        return Agent(name="fixer", model=model or "p", extra={} if model else {"model": "inherit"},
                     description="Fixes.", effort="low", extensions=[Subtasks()])

    main = Agent(name="main", model="p", extensions=[Subtasks(agents=["*"], queue="kids")])
    p = ScriptedProvider([reply("", agent("a", "fixer"), agent("b", "fixer", model="opus",
                                                                effort="max")), reply("ok")])
    rt = runtime(store, {"p": p}, main, defined("haiku"))
    client = Client(store)
    tid = await client.create("main", "go", config={"model": "p", "effort": "high"})
    await rt.run_until_idle()
    a, b = sorted(await client.tasks(parent_id=tid), key=lambda r: (len(r.task_id), r.task_id))
    ca = (await events(client, a.task_id))[0].config
    cb = (await events(client, b.task_id))[0].config
    assert ca["model"] == "claude-haiku-5-5" and ca["effort"] == "low"
    assert cb["model"] == "claude-opus-5-5" and cb["effort"] == "max"


async def test_a_fork_starts_from_the_callers_conversation(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("you are the fork", "fork")), reply("forked")])
    rt = runtime(store, {"p": p}, parent(queue="default"))
    client = Client(store)
    tid = await client.create("main", "the original task")
    p.add(handback("fork report"), reply("thanks"))
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    created = (await events(client, child.task_id))[0]
    assert isinstance(created, TaskCreated) and created.agent == "main"
    assert created.history_from == tid and created.config["fork"] is True
    fork_request = p.requests[2]
    assert texts_of(fork_request)[0] == "the original task"
    assert texts_of(fork_request)[-2:] == ["you are the fork", texts.HANDBACK_REMINDER]
    assert fork_request.tools[-1].name == "SubagentHandback"


async def test_a_fork_cannot_fork(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("again", "fork")), reply("ok")])
    rt = runtime(store, {"p": p}, parent())
    client = Client(store)
    tid = await client.create("main", "go", config={"fork": True})
    await rt.run_until_idle()
    [(text, _)] = await results(client, tid)
    assert text == texts.SPAWN_DENIED.format(reason=texts.FORK_IN_FORK)


class LoseNotices(Subtasks):
    """Takes the child's end but 'crashes' before recording the notice."""

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        return None


async def test_a_notice_lost_to_a_crash_is_posted_on_the_next_run(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("x")), reply("started")])
    c = ScriptedProvider([reply("no report")])
    lossy = Agent(name="main", model="p", extensions=[LoseNotices(ROSTER)])
    rt = runtime(store, {"p": p, "c": c}, lossy, worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    assert len(p.requests) == 2
    p.add(reply("hi"), reply("thanks for the result"))
    await client.send(tid, "hello")
    await runtime(store, {"p": p, "c": c}, parent(), worker()).run_until_idle()
    assert "no report was delivered" in "".join(texts_of(p.requests[-1]))


async def test_agent_approvals_surface_at_the_root(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("x")), reply("launched")])
    c = ScriptedProvider([reply("", ("Probe", {}))])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker(AskEverything()))
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    [pending] = await pending_tree(client, tid)
    assert pending.task_id == child.task_id and pending.kind == "approval"
    assert (await tree_usage(client, tid)).usage == Usage(uncached=30, output=15)


async def test_progress_label_every_30_seconds(store: SqliteStore,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code's cadence: a label is asked for 30 seconds after the
    agent starts and every 30 seconds after, once it has three messages and
    only when its conversation changed, timed from recorded times."""
    clock = iter([0.0, 31.0, 40.0, 75.0, 76.0, 200.0])
    monkeypatch.setattr(subtasks_extension, "_now", lambda: next(clock))
    p = ScriptedProvider([reply("", agent("dig")), reply("launched"), reply("thanks")])
    c = ScriptedProvider([reply("", ("Probe", {})), reply("", ("Probe", {})),
                          reply("", ("Probe", {})), handback("dug")])
    labels = ScriptedProvider([reply("Reading parser.py\nextra"), reply("Running the tests")])

    class Split:
        """Label asks to ``labels``, the agent's own calls to ``c``."""

        family = "scripted"
        requests = c.requests

        async def complete(self, request: ModelRequest, on_delta: Any = None) -> Any:
            if request.messages[-1].text == texts.PROGRESS_INSTRUCTION:
                return await labels.complete(request, on_delta)
            return await c.complete(request, on_delta)

    rt = runtime(store, {"p": p, "c": Split()}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    # The agent's calls at t=0 (start), 31 (due: a label), 40 (not due yet),
    # 75 (due: a label); each ask copies that call's request.
    assert len(labels.requests) == 2
    assert labels.requests[0].messages[:-1] == c.requests[1].messages
    assert labels.requests[1].messages[:-1] == c.requests[3].messages
    notices = [e.data["text"] for e in await events(client, child.task_id)
               if getattr(e, "kind", "") == "notice"]
    assert notices == ["Reading parser.py", "Running the tests"]
    assert all(texts.PROGRESS_INSTRUCTION not in t for r in c.requests for t in texts_of(r))


async def test_worktree_isolation(store: SqliteStore, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                                 "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    p = ScriptedProvider([reply("", agent("look", isolation="worktree")), reply("launched"),
                          reply("seen it")])
    c = ScriptedProvider([handback("nothing changed")])
    rt = Runtime(store=store, agents={"main": parent(), "worker": worker()},
                 provider_for=lambda m: {"p": p, "c": c}[m],  # type: ignore[arg-type,return-value]
                 env_for=lambda s: LocalExecEnv(str(s.config.get("cwd") or repo)))
    client = Client(store)
    tid = await client.create("main", "go", config={"cwd": str(repo)})
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    cwd = (await events(client, child.task_id))[0].config["cwd"]
    assert cwd.startswith(str(repo / ".noeta" / "worktrees" / "agent-"))
    assert not Path(cwd).exists()  # unchanged, so removed again


async def test_an_agent_refused_for_its_queue_leaves_no_worktree(
        store: SqliteStore, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    p = ScriptedProvider([reply("", agent("look", isolation="worktree")), reply("ok")])
    rt = Runtime(store=store, agents={"main": parent(queue="kids"), "worker": worker()},
                 provider_for=lambda m: {"p": p}[m],  # type: ignore[arg-type,return-value]
                 env_for=lambda s: LocalExecEnv(str(s.config.get("cwd") or repo)))
    client = Client(store)
    tid = await client.create("main", "go", config={"cwd": str(repo)})
    release = await rt.hold(tid)  # as a background job does
    try:
        await rt.run_until_idle()
    finally:
        await release()
    assert await client.tasks(parent_id=tid) == []
    [(text, failed)] = await results(client, tid)
    assert failed and "kids" in text
    assert not (repo / ".noeta" / "worktrees").exists() \
        or not list((repo / ".noeta" / "worktrees").iterdir())
    branches = subprocess.run(["git", "branch", "--list", "worktree-*"], cwd=repo,
                              capture_output=True, text=True, check=True).stdout
    assert branches.strip() == ""


async def test_a_cancelled_parent_closes_its_agents_worktrees(store: SqliteStore,
                                                              tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                                 "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    p = ScriptedProvider([reply("", agent("look", isolation="worktree")), reply("launched")])
    c = ScriptedProvider([reply("", ("probe", {"x": 1}))])  # waits for an approval
    rt = Runtime(store=store, agents={"main": parent(), "worker": worker(AskEverything())},
                 provider_for=lambda m: {"p": p, "c": c}[m],  # type: ignore[arg-type,return-value]
                 env_for=lambda s: LocalExecEnv(str(s.config.get("cwd") or repo)))
    client = Client(store)
    tid = await client.create("main", "go", config={"cwd": str(repo)})
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    cwd = (await events(client, child.task_id))[0].config["cwd"]
    assert Path(cwd).exists()
    await client.cancel(tid)
    await rt.run_until_idle()
    assert not Path(cwd).exists()  # unchanged, so removed with its parent


async def test_a_worktree_a_background_job_runs_in_is_kept_until_the_job_ends(
        store: SqliteStore, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    registry = Registry(log_dir=str(tmp_path / "logs"))
    p = ScriptedProvider([reply("", agent("look", isolation="worktree")), reply("launched"),
                          reply("seen it")])
    c = ScriptedProvider([reply("", ("Bash", {"command": "sleep 30",
                                              "run_in_background": True})),
                          handback("started it")])
    child_agent = Agent(name="worker", model="c", tools=[],
                        extensions=[Subtasks(()), Shell(registry=registry)])
    rt = Runtime(store=store, agents={"main": parent(), "worker": child_agent},
                 provider_for=lambda m: {"p": p, "c": c}[m],  # type: ignore[arg-type,return-value]
                 env_for=lambda s: LocalExecEnv(str(s.config.get("cwd") or repo)))
    client = Client(store)
    tid = await client.create("main", "go", config={"cwd": str(repo)})
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    cwd = (await events(client, child.task_id))[0].config["cwd"]
    assert (await client.get(child.task_id)).status == "ended"
    assert Path(cwd).exists()  # not removed under the running job
    told = [e.data["text"] for e in await events(client, tid)
            if getattr(e, "kind", "") == "pending"]
    # Kept: the notice names it in Claude Code's ``<worktree>`` element.
    assert told and f"<worktree><worktreePath>{cwd}</worktreePath><worktreeBranch>" in told[0]
    [job] = registry.running(tid)
    registry.retire(tid)  # the root ends: its jobs are killed
    await asyncio.wait_for(job.done.wait(), 10)
    for _ in range(200):  # then the unchanged worktree goes
        if not Path(cwd).exists():
            break
        await asyncio.sleep(0.05)
    assert not Path(cwd).exists()


class AskProbe(Extension):
    name = "ask-probe"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Ask(prompt="may I?") if call.name == "probe" else None


async def test_a_resumed_agent_keeps_the_worktree_its_job_was_to_free(
        store: SqliteStore, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    registry = Registry(log_dir=str(tmp_path / "logs"))
    p = ScriptedProvider([reply("", agent("look", isolation="worktree")), reply("launched"),
                          reply("seen it")])
    c = ScriptedProvider([reply("", ("Bash", {"command": "sleep 30",
                                              "run_in_background": True})),
                          handback("started it")])
    child_agent = Agent(name="worker", model="c", tools=[Probe()],
                        extensions=[Subtasks(()), Shell(registry=registry), AskProbe()])
    rt = Runtime(store=store, agents={"main": parent(), "worker": child_agent},
                 provider_for=lambda m: {"p": p, "c": c}[m],  # type: ignore[arg-type,return-value]
                 env_for=lambda s: LocalExecEnv(str(s.config.get("cwd") or repo)))
    client = Client(store)
    tid = await client.create("main", "go", config={"cwd": str(repo)})
    await rt.run_until_idle()
    [first] = await client.tasks(parent_id=tid)
    cwd = (await events(client, first.task_id))[0].config["cwd"]
    assert (await client.get(first.task_id)).status == "ended" and Path(cwd).exists()
    # Resumed while its job still runs: it works in the worktree again.
    p.add(reply("", ("SendMessage", {"to": first.task_id, "message": "look again"})),
          reply("asked"))
    c.add(reply("", ("probe", {"x": 1})))  # waits for an approval
    await client.send(tid, "again")
    await rt.run_until_idle()
    second = next(r for r in await client.tasks(parent_id=tid) if r.task_id != first.task_id)
    assert (await events(client, second.task_id))[0].config["cwd"] == cwd
    [job] = registry.running(tid)
    registry.retire(tid)
    await asyncio.wait_for(job.done.wait(), 10)
    await asyncio.sleep(0.5)
    assert Path(cwd).exists()  # the resumed agent still works there


async def test_a_call_a_crash_cut_short_removes_the_worktree_it_made(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                                 "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    state = TaskState(task_id="t", agent="main", config={"cwd": str(repo)})
    tool = AgentTool(Subtasks(ROSTER), state, {s.name: s for s in ROSTER})
    ctx = ToolContext(task_id="t", call_id="toolu_crash1", env=LocalExecEnv(str(repo)),
                      state={}, record=lambda *_: None)
    args = {"description": "d", "prompt": "p", "isolation": "worktree"}
    # The worker died after making the worktree, before the launch was recorded.
    tree = await make_agent_worktree(ctx, {"cwd": str(repo)})
    assert isinstance(tree, dict) and Path(tree["path"]).exists()
    assert await tool.recover(args, ctx) is None  # no agent ran: outcome unknown
    assert not Path(tree["path"]).exists()
    assert tree["branch"] not in subprocess.run(["git", "branch"], cwd=repo, check=True,
                                                capture_output=True, text=True).stdout
    # Nothing made: nothing to remove.
    assert await tool.recover(args, ctx) is None


async def test_a_hand_back_a_crash_cut_short_reports_once(tmp_path: Path) -> None:
    from noeta.plugins.subtasks.tool import HandbackTool

    posts: list[tuple[Any, dict[str, Any]]] = []

    async def post(body: Any, **kw: Any) -> None:
        posts.append((body, kw))

    tool = HandbackTool(TaskState(task_id="p.c.1", agent="worker", parent_id="p"))
    ctx = ToolContext(task_id="p.c.1", call_id="toolu_back", env=LocalExecEnv(str(tmp_path)),
                      state={}, record=lambda *_: None, post=post)
    first = await tool.run({"message": "1 2 3"}, ctx)
    # The worker died after the report was posted, before the result was written.
    again = await tool.recover({"message": "1 2 3"}, ctx)
    assert again is not None and again.end_turn == first.end_turn == "1 2 3"
    keys = [kw.get("dedupe_key") for _, kw in posts]
    assert len(keys) == 2 and keys[0] is not None and keys[0] == keys[1]


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                                 "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    return repo


async def test_recovery_leaves_another_tasks_worktree_with_the_same_call_id(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def tool_ctx(task_id: str) -> tuple[AgentTool, ToolContext]:
        state = TaskState(task_id=task_id, agent="main", config={"cwd": str(repo)})
        return (AgentTool(Subtasks(ROSTER), state, {s.name: s for s in ROSTER}),
                ToolContext(task_id=task_id, call_id="call_3_0", env=LocalExecEnv(str(repo)),
                            state={}, record=lambda *_: None))
    args = {"description": "d", "prompt": "p", "isolation": "worktree"}
    _, ctx_a = tool_ctx("task-a")
    tool_b, ctx_b = tool_ctx("task-b")
    tree = await make_agent_worktree(ctx_a, {"cwd": str(repo)})
    assert isinstance(tree, dict)
    other = await make_agent_worktree(ctx_b, {"cwd": str(repo)})
    assert isinstance(other, dict) and other["path"] != tree["path"]
    subprocess.run(["git", "worktree", "remove", other["path"]], cwd=repo, check=True)
    await tool_b.recover(args, ctx_b)  # task b's crashed call left nothing behind
    assert Path(tree["path"]).exists()


async def test_recovery_keeps_a_worktree_with_unsaved_work(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state = TaskState(task_id="t", agent="main", config={"cwd": str(repo)})
    tool = AgentTool(Subtasks(ROSTER), state, {s.name: s for s in ROSTER})
    ctx = ToolContext(task_id="t", call_id="toolu_crash2", env=LocalExecEnv(str(repo)),
                      state={}, record=lambda *_: None)
    tree = await make_agent_worktree(ctx, {"cwd": str(repo)})
    assert isinstance(tree, dict)
    (Path(tree["path"]) / "notes.txt").write_text("work")
    await tool.recover({"description": "d", "prompt": "p", "isolation": "worktree"}, ctx)
    assert (Path(tree["path"]) / "notes.txt").exists()


async def test_resuming_a_worktree_agent_whose_worktree_is_gone(store: SqliteStore,
                                                               tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                                 "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    p = ScriptedProvider([reply("", agent("look", isolation="worktree")), reply("launched"),
                          reply("seen it")])
    c = ScriptedProvider([handback("nothing changed"), handback("still nothing")])
    rt = Runtime(store=store, agents={"main": parent(), "worker": worker()},
                 provider_for=lambda m: {"p": p, "c": c}[m],  # type: ignore[arg-type,return-value]
                 env_for=lambda s: LocalExecEnv(str(s.config.get("cwd") or repo)))
    client = Client(store)
    tid = await client.create("main", "go", config={"cwd": str(repo)})
    await rt.run_until_idle()
    [first] = await client.tasks(parent_id=tid)
    gone = (await events(client, first.task_id))[0].config["cwd"]
    assert not Path(gone).exists()
    p.add(reply("", ("SendMessage", {"to": first.task_id, "message": "look again"})),
          reply("asked"), reply("done"))
    await client.send(tid, "again")
    await rt.run_until_idle()
    second = next(r for r in await client.tasks(parent_id=tid) if r.task_id != first.task_id)
    assert (await events(client, second.task_id))[0].config["cwd"] == str(repo)
    sent = "".join(texts_of(p.requests[-1]))
    assert "<status>completed</status>" in sent and gone not in sent


async def test_send_message_is_deferred(store: SqliteStore) -> None:
    SendMessageTool.deferred = True  # type: ignore[misc]
    p = ScriptedProvider([reply("hi")])
    rt = runtime(store, {"p": p}, parent(ToolSearch()))
    await Client(store).create("main", "go")
    await rt.run_until_idle()
    assert [t.name for t in p.requests[0].tools] == [
        "Agent", "ListAgents", "DeferredToolPlaceholder", "ToolSearch"]


async def test_a_sub_agent_hears_of_agent_types_with_its_second_request(
        store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("look")), reply("launched"), reply("ok")])
    c = ScriptedProvider([reply("", ("Probe", {})), handback("seen")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker(roster=ROSTER))
    await Client(store).create("main", "go")
    await rt.run_until_idle()

    def listed(request: ModelRequest) -> bool:
        return any(m.role == "system" and m.text.startswith(texts.AGENT_LIST_HEAD)
                   for m in request.messages)

    first, second = c.requests
    assert not listed(first) and listed(second)


async def test_a_background_agent_keeps_its_transcript_file(store: SqliteStore,
                                                            tmp_path: Path) -> None:
    p = ScriptedProvider([reply("", agent("count to 3")), reply("launched"),
                          reply("it says 1 2 3"), reply("ok")])
    c = ScriptedProvider([reply("", ("Probe", {})), handback("1 2 3")])
    spill = tmp_path / "spill"
    rt = Runtime(store=store, agents={"main": parent(), "worker": worker()},
                 provider_for=lambda m: {"p": p, "c": c}[m],  # type: ignore[arg-type,return-value]
                 env_for=lambda s: LocalExecEnv(str(tmp_path)), spill_root=str(spill))
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    path = spill / tid / "tasks" / f"{child.task_id}.output"
    [(ack, _)] = await results(client, tid)
    assert ack.endswith(texts.LAUNCHED_OUTPUT_FILE.format(path=path))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[0] == {"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": "count to 3"}]}}
    called = [line["message"]["content"][0] for line in lines if line["type"] == "assistant"]
    assert [b["name"] for b in called] == ["Probe", "SubagentHandback"]
    await client.send(tid, "thanks")
    await rt.run_until_idle()
    notice = p.requests[3].messages[-2].text
    assert (f"<tool-use-id>{p.requests[1].messages[-2].tool_uses[0].id}</tool-use-id>\n"
            f"<output-file>{path}</output-file>\n<status>completed</status>") in notice


class _CountingEnv(LocalExecEnv):
    """Counts whole-file writes per path."""

    writes: dict[str, int] = {}

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.writes[path] = self.writes.get(path, 0) + 1
        await super().write_bytes(path, data)


async def test_a_transcript_is_appended_to_not_rewritten(store: SqliteStore,
                                                          tmp_path: Path) -> None:
    p = ScriptedProvider([reply("", agent("count to 3")), reply("launched"),
                          reply("it says 1 2 3"), reply("ok")])
    c = ScriptedProvider([reply("", ("Probe", {})), handback("1 2 3")])
    spill = tmp_path / "spill"
    _CountingEnv.writes = {}
    rt = Runtime(store=store, agents={"main": parent(), "worker": worker()},
                 provider_for=lambda m: {"p": p, "c": c}[m],  # type: ignore[arg-type,return-value]
                 env_for=lambda s: _CountingEnv(str(tmp_path)), spill_root=str(spill))
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    path = spill / tid / "tasks" / f"{child.task_id}.output"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    called = [line["message"]["content"][0] for line in lines if line["type"] == "assistant"]
    assert [b["name"] for b in called] == ["Probe", "SubagentHandback"]
    # Written whole once; every later message was appended.
    assert _CountingEnv.writes[str(path)] == 1


async def test_transcript_progress_is_forgotten_past_its_bound(tmp_path: Path) -> None:
    from noeta.core.loop.state import TaskState
    from noeta.plugins.subtasks import extension as ext_module

    subtasks = Subtasks(ROSTER)
    env = LocalExecEnv(str(tmp_path))
    for i in range(ext_module.TRANSCRIBED_KEEP + 10):
        # Tasks another worker took on: none of them ends here.
        state = TaskState(task_id=f"t{i}", config={TRANSCRIPT_KEY: str(tmp_path)})
        await subtasks._transcribe(TaskView(state=state, env=env), ended=False)
    assert len(subtasks._transcribed) == ext_module.TRANSCRIBED_KEEP
    assert "t0" not in subtasks._transcribed and f"t{ext_module.TRANSCRIBED_KEEP}" in \
        subtasks._transcribed


async def test_an_agent_type_matches_loosely_when_only_one_fits(store: SqliteStore) -> None:
    roster = [*ROSTER, SubagentSpec(name="code-review", description="Reviews.", agent="worker"),
              SubagentSpec(name="code_review", description="Also reviews.", agent="worker")]
    p = ScriptedProvider([reply("", agent("a", "Scout"), agent("b", "General Purpose"),
                                agent("c", "Code Review")), reply("ok")])
    main = Agent(name="main", model="p", extensions=[Subtasks(roster, queue="kids")])
    rt = runtime(store, {"p": p}, main, worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    kinds = sorted([(await events(client, r.task_id))[0].config["subagent_type"]
                    for r in await client.tasks(parent_id=tid)])
    assert kinds == ["general-purpose", "scout"]
    ambiguous = texts.SPAWN_DENIED.format(reason=texts.AMBIGUOUS_AGENT.format(
        name="Code Review", matches="code-review or code_review"))
    assert (ambiguous, True) in await results(client, tid)


@pytest.mark.parametrize(("kind", "lands"), [("scout", "team-a"), ("gpu-only", "kids")])
async def test_a_sub_agent_inherits_its_callers_queue_unless_its_spec_names_one(
        store: SqliteStore, kind: str, lands: str) -> None:
    roster = [*ROSTER, SubagentSpec(name="gpu-only", description="x", agent="worker",
                                    queue="kids")]
    main = Agent(name="main", model="p", extensions=[Subtasks(roster)])
    p = ScriptedProvider([reply("", agent("a", kind)), reply("launched")])
    rt = runtime(store, {"p": p}, main, queues=("team-a",))
    client = Client(store)
    tid = await client.create("main", "go", queue="team-a")
    await rt.run_once()
    [child] = await client.tasks(parent_id=tid)
    assert child.queue == lands


async def test_agent_types_that_join_or_leave_mid_task_are_told(store: SqliteStore) -> None:
    ext = Subtasks(ROSTER)
    main = Agent(name="main", model="p", extensions=[ext])
    p = ScriptedProvider([reply("one"), reply("two"), reply("three"), reply("four")])
    client = Client(store)
    tid = await client.create("main", "go")
    await runtime(store, {"p": p}, main, worker()).run_until_idle()
    reviewer = SubagentSpec(name="reviewer", description="Reviews.", agent="worker")
    ext._roster.specs = {**ext._roster.specs, "reviewer": reviewer}
    await client.send(tid, "more")
    await runtime(store, {"p": p}, main, worker()).run_until_idle()
    ext._roster.specs = {"reviewer": reviewer}
    await client.send(tid, "again")
    await runtime(store, {"p": p}, main, worker()).run_until_idle()
    await client.send(tid, "quiet")
    await runtime(store, {"p": p}, main, worker()).run_until_idle()

    def notes(i: int) -> list[str]:
        return [m.text for m in p.requests[i].messages if m.role == "system"
                and "agent types" in m.text.lower()]

    assert notes(1)[-1] == ("New agent types the Agent tool can start:\n"
                            "- reviewer: Reviews. (Tools: *)")
    assert notes(2)[-1].startswith("These agent types are no longer available:\n"
                                   "- general-purpose\n- scout\nThis is background information")
    assert notes(3) == notes(2)
