"""The Agent tool and its companions as Claude Code has them: background
launch, the sub-agent's SubagentHandback, the agent-message and the
completion notice, SendMessage (resume, steer, reply), ListAgents, forks,
worktrees and the refusals."""

from __future__ import annotations

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
from noeta.core.runner import Runtime
from noeta.core.types import Message, ModelRequest, ToolUse, Usage
from noeta.host.pending import pending_tree
from noeta.host.usage import tree_usage
from noeta.plugins.subtasks import SubagentSpec, Subtasks, texts
from noeta.plugins.subtasks.tool import (
    SendMessageTool,
    agent_message,
    agent_schema,
    handback_sender,
)
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
    return [m.text for m in request.messages if m.role == "user" and m.text]


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
                 texts.RESULT_POINTER.format(id=cid),
                 "<usage><subagent_tokens>15</subagent_tokens><tool_uses>2</tool_uses>"):
        assert part in notice
    call_id = p.requests[1].messages[-2].tool_uses[0].id
    assert f"<tool-use-id>{call_id}</tool-use-id>" in notice


async def test_final_text_without_hand_back_is_dropped(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("x", "scout")), reply("launched"), reply("hm")])
    c = ScriptedProvider([reply("my secret final words")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    notice = p.requests[2].messages[-1]
    assert notice.origin == "system" and texts.RESULT_NO_REPORT in notice.text
    assert "my secret final words" not in "".join(texts_of(p.requests[2]))
    assert (await client.get(tid)).waiting_on == "input"


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
    assert "<result>made the file\n</result>" in notice


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
              ("SendMessage", {"to": "x", "message": " "})),
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
        (texts.EMPTY_MESSAGE, True)]
    p.add(reply("", ("SendMessage", {"to": other, "message": "ping"}), ("ListAgents", {})),
          reply("done"))
    await client.send(tid, "ping the other one")
    await rt.run_until_idle()
    out = await results(client, tid)
    assert out[3] == (texts.SENT.format(to=other), False)
    assert out[4][0].startswith(texts.LIST_HEAD) and f"- {other} — session" in out[4][0]
    got = other_p.requests[-1].messages[-1]
    assert got.origin == "agent" and '<cross-session-message from="' in got.text


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
        reply("", agent("  ")),
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
    assert out[0] == (texts.NOTHING_RAN.format(reason=texts.EMPTY_PROMPT), True)
    assert out[1] == (texts.NOTHING_RAN.format(reason=texts.BAD_CHOICE.format(
        field="model", choices="sonnet, opus, haiku, fable", value="gpt")), True)
    unknown = texts.SPAWN_DENIED.format(reason=texts.UNKNOWN_AGENT.format(
        name="nobody", available="fork, general-purpose, scout"))
    assert out[2:4] == [(unknown, True), ("seen", False)]
    assert out[4] == (texts.SPAWN_DENIED.format(reason=texts.NO_REMOTE), True)
    assert out[5] == (texts.MISSING_FIELDS, True)
    assert await client.tasks(parent_id=tid) == []


async def test_depth_cap_and_fan_out(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("a")), reply("ok"),
                          reply("", agent("a"), agent("b"), agent("c")), reply("ok"),
                          reply("", agent("a"), agent("b")), reply("ok")])
    rt = runtime(store, {"p": p}, parent(queue="kids", max_fanout=2, max_background=1),
                 worker())
    client = Client(store)
    tid = await client.create("main", "go", config={"subtask_depth": 3})
    await rt.run_until_idle()
    [(depth, _)] = await results(client, tid)
    assert depth == texts.SPAWN_DENIED.format(reason=texts.DEPTH_REACHED.format(n=3))
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    await client.send(tid, "more")
    await rt.run_until_idle()
    out = await results(client, tid)
    assert out[:3] == [(texts.FANOUT_TOO_BIG.format(n=3, limit=2), True)] * 3
    assert out[3:] == [(texts.TOO_MANY_BACKGROUND.format(limit=1), True)] * 2


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
        return Agent(name="fixer", model=model or "p", inherit_model=not model,
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
    assert texts.RESULT_NO_REPORT in "".join(texts_of(p.requests[-1]))


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


async def test_progress_label_beside_the_third_call(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("dig")), reply("launched"), reply("thanks")])
    c = ScriptedProvider([reply("", ("Probe", {})), reply("", ("Probe", {})),
                          reply("Reading parser.py\nextra"), handback("dug")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    label_request = c.requests[2]
    assert label_request.messages[-1].text == texts.PROGRESS_INSTRUCTION
    assert label_request.messages[:-1] == c.requests[3].messages[:len(label_request.messages) - 1]
    notices = [e.data["text"] for e in await events(client, child.task_id)
               if getattr(e, "kind", "") == "notice"]
    assert notices == ["Reading parser.py"]
    assert all(texts.PROGRESS_INSTRUCTION not in t for t in texts_of(c.requests[3]))


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


async def test_send_message_is_deferred(store: SqliteStore) -> None:
    SendMessageTool.deferred = True  # type: ignore[misc]
    p = ScriptedProvider([reply("hi")])
    rt = runtime(store, {"p": p}, parent())
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
