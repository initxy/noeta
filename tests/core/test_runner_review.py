"""Regressions from the core review: what a waiting task tells front ends,
answers that stay with their own call, turns that close cleanly, and a task
that never stays ``running`` after a bug, a lost renewal or a shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import Fatal
from noeta.core.events import (
    Answer,
    Cancel,
    ExtensionRecorded,
    InboxItem,
    InputTaken,
    ModelReplied,
    Recorded,
    Say,
    TaskCreated,
    TaskEnded,
    ToolFinished,
    ToolStarted,
    Waiting,
)
from noeta.core.extension import Ask, Deny, Extension, Rewrite, TaskView
from noeta.core.loop import decide
from noeta.core.loop.state import fold
from noeta.core.records import CORE, TurnStopped
from noeta.core.runner import Runtime
from noeta.core.tool import Park, ToolContext, ToolOutput, text_output
from noeta.core.types import (
    Delta,
    Message,
    ModelResponse,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
    Usage,
    user,
)
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


def runtime(store: Any, provider: Any, *agents: Agent, **kw: Any) -> Runtime:
    return Runtime(store=store, agents={a.name: a for a in agents},
                   provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                   sleep=_no_sleep, **kw)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


class AskTool:
    name = "Ask"
    description = "Ask the user."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return ToolOutput(park=Park(on="question",
                                    detail={"questions": [{"question": "which one?"}]}))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        return text_output(f"user said: {getattr(outcome, 'data', None)}")


class Echo:
    name = "Echo"
    description = "echo"
    input_schema = {"type": "object"}
    read_only = True
    clearable = False

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.inputs.append(dict(input))
        return text_output("ok")


class Delegate:
    name = "Delegate"
    description = "Hand work to a sub-agent."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self, agent: str = "worker") -> None:
        self.agent = agent

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.spawn is not None
        return ToolOutput(park=Park(on="children", keys=[ctx.spawn(self.agent, "sub job")]))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        if outcome is None:
            return text_output("stopped", is_error=True)
        [ended] = outcome.values()
        return text_output(f"child {ended.status}: {ended.error or ended.answer}")


class AskAll(Extension):
    name = "askall"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="ok?")


def last_event(events: list[Recorded]) -> Any:
    return events[-1].event


# ------------------------------------------------------------ #1 question detail


async def test_waiting_question_carries_each_parked_question(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Ask", {}))])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[AskTool()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    w = last_event(await client.events(tid))
    assert isinstance(w, Waiting) and w.on == "question"
    [call_id] = w.ids
    assert w.detail == {call_id: {"on": "question", "questions": [{"question": "which one?"}]}}


# ------------------------------------------------------------ #15 mixed parks


async def test_a_question_surfaces_while_children_run(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Delegate", {}), ("Ask", {}))])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Delegate(), AskTool()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_once()  # only the parent; the child is never run
    w = last_event(await client.events(tid))
    assert isinstance(w, Waiting) and w.on == "question"
    assert len(w.ids) == 2  # the child's id and the question's call id
    assert [v["on"] for v in w.detail.values()] == ["children", "question"]


# ------------------------------------------------------------ #5 the answer text


async def test_thinking_only_cut_does_not_glue_old_text(store: SqliteStore) -> None:
    empty_cut = ModelResponse(message=Message(role="assistant", content=[]),
                              stop_reason="max_tokens", usage=Usage())
    provider = ScriptedProvider([reply("Let me check.", ("Echo", {})), empty_cut,
                                 reply("All good.")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Echo()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    w = last_event(await client.events(tid))
    assert isinstance(w, Waiting) and w.detail.get("answer") == "All good."


async def test_continued_text_survives_a_thinking_only_cut(store: SqliteStore) -> None:
    empty_cut = ModelResponse(message=Message(role="assistant", content=[]),
                              stop_reason="max_tokens", usage=Usage())
    provider = ScriptedProvider([reply("part one, ", stop="max_tokens"), empty_cut,
                                 reply("part two")])
    rt = runtime(store, provider, Agent(name="a", model="m"))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    w = last_event(await client.events(tid))
    assert isinstance(w, Waiting) and w.detail.get("answer") == "part one, part two"


async def test_continuing_does_not_leak_across_turns(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("partial", stop="max_tokens"), Fatal("boom"),
                                 reply("fresh answer")])
    rt = runtime(store, provider, Agent(name="a", model="m"))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await client.send(tid, "again")
    await rt.run_until_idle()
    w = last_event(await client.events(tid))
    assert isinstance(w, Waiting) and w.detail.get("answer") == "fresh answer"


# ------------------------------------------------------------ #6 bugs fail the turn


class Boom(Extension):
    name = "boom"

    def __init__(self) -> None:
        self.calls = 0

    async def before_model(self, view: TaskView, request: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("extension bug")
        return request


async def test_an_extension_bug_fails_the_turn_instead_of_wedging(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("second try works")])
    rt = runtime(store, provider, Agent(name="a", model="m", extensions=[Boom()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    row = await client.get(tid)
    assert row.status == "waiting" and row.owner is None
    w = last_event(await client.events(tid))
    assert isinstance(w, Waiting) and "internal_error" in w.detail["turn_failed"]
    await client.send(tid, "again")
    await rt.run_until_idle()
    w = last_event(await client.events(tid))
    assert isinstance(w, Waiting) and w.detail.get("answer") == "second try works"


async def test_an_extension_bug_ends_a_one_shot_task(store: SqliteStore) -> None:
    rt = runtime(store, ScriptedProvider(), Agent(name="a", model="m", one_shot=True,
                                                  extensions=[Boom()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    ended = last_event(await client.events(tid))
    assert isinstance(ended, TaskEnded) and ended.status == "failed"
    assert ended.error.startswith("internal_error")


class FlakyStore:
    """A store whose ``renew`` / ``claim`` raise what no adapter maps
    (``renew_fails``: always, or once that event is set)."""

    def __init__(self, inner: SqliteStore, *, renew_fails: bool | asyncio.Event = False,
                 claim_fails: int = 0) -> None:
        self.inner = inner
        self.renew_fails = renew_fails
        self.claim_fails = claim_fails

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def renew(self, claim: Any, lease_seconds: float) -> Any:
        failing = self.renew_fails
        if failing is True or (isinstance(failing, asyncio.Event) and failing.is_set()):
            raise RuntimeError("driver bug")
        return await self.inner.renew(claim, lease_seconds)

    async def claim(self, *a: Any, **kw: Any) -> Any:
        if self.claim_fails:
            self.claim_fails -= 1
            raise RuntimeError("driver bug")
        return await self.inner.claim(*a, **kw)


class Slow:
    name = "Slow"
    description = "slow"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self) -> None:
        self.cancelled = asyncio.Event()
        self.started = asyncio.Event()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return text_output("done")


async def test_a_renewal_that_keeps_failing_gives_the_task_up(store: SqliteStore) -> None:
    slow = Slow()
    # Renewals fail once the tool runs: a slow machine reaching the tool
    # after the lease ran out would otherwise give up before it starts.
    rt = runtime(FlakyStore(store, renew_fails=slow.started),
                 ScriptedProvider([reply("", ("Slow", {}))]),
                 Agent(name="a", model="m", tools=[slow]), lease_seconds=0.3)
    client = Client(store)
    await client.create("a", "go")
    running = asyncio.ensure_future(rt.run_once())
    await asyncio.wait_for(slow.started.wait(), 10)
    await asyncio.wait_for(running, 5)  # the keeper gave up and cancelled the step
    assert slow.cancelled.is_set()


async def test_serve_survives_a_claim_that_raises(store: SqliteStore) -> None:
    rt = runtime(FlakyStore(store, claim_fails=2), ScriptedProvider([reply("hi")]),
                 Agent(name="a", model="m"), poll_interval=0.02)
    client = Client(store)
    tid = await client.create("a", "go")
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))
    row = await client.wait(tid, timeout=5)
    stop.set()
    await serving
    assert row.status == "waiting"


# ------------------------------------------------------------ #10 shutdown releases


async def test_shutdown_hands_running_tasks_back(store: SqliteStore) -> None:
    slow = Slow()
    rt = runtime(store, ScriptedProvider([reply("", ("Slow", {}))]),
                 Agent(name="a", model="m", tools=[slow]), poll_interval=0.02)
    client = Client(store)
    tid = await client.create("a", "go")
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))
    await asyncio.wait_for(slow.started.wait(), 5)
    stop.set()
    await serving
    row = await client.get(tid)
    assert row.status == "ready" and row.owner is None
    # Claimable at once, not after the 30 s lease.
    assert await store.claim("other", ["default"], 30) is not None


# ------------------------------------------------------------ #7 #8 children


async def test_a_child_with_an_unknown_agent_reports_to_its_parent(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Delegate", {})), reply("parent done")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Delegate("nosuch")]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    w = last_event(await client.events(tid))
    assert isinstance(w, Waiting) and w.on == "input"
    results = [r.event.result.text for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    assert results and "child failed: no agent named 'nosuch'" in results[0]


class SpawnThenFail:
    name = "Boom"
    description = "d"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.spawn is not None
        ctx.spawn("worker", "x")
        ctx.record("boom", "note", 1)
        raise ValueError("after spawn")


async def test_a_tool_that_fails_after_spawning_leaves_no_phantom_child(
        store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Boom", {})), reply("done")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[SpawnThenFail()]),
                 Agent(name="worker", model="m", one_shot=True))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert await client.tasks(parent_id=tid) == []
    assert not any(isinstance(r.event, ExtensionRecorded) and r.event.kind in ("spawned", "note")
                   for r in await client.events(tid))
    await client.cancel(tid)
    await rt.run_until_idle()
    row = await client.get(tid)
    assert row.status == "ended" and row.end_status == "cancelled"


# ------------------------------------------------------------ #9 call ids


def same_id_reply() -> ModelResponse:
    return ModelResponse(message=Message(role="assistant", content=[
        ToolUse(id="call_0", name="Echo", input={})]), stop_reason="tool_use")


async def test_a_reused_call_id_is_not_auto_approved(store: SqliteStore) -> None:
    provider = ScriptedProvider([same_id_reply(), reply("done 1"), same_id_reply(),
                                 reply("done 2")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Echo()],
                                        extensions=[AskAll()]))
    client = Client(store)
    tid = await client.create("a", "first")
    await rt.run_until_idle()
    first = last_event(await client.events(tid))
    assert isinstance(first, Waiting) and first.on == "approval"
    await client.answer(tid, first.ids[0], "allow")
    await rt.run_until_idle()
    await client.send(tid, "second")
    await rt.run_until_idle()
    second = last_event(await client.events(tid))
    assert isinstance(second, Waiting) and second.on == "approval"
    assert second.ids != first.ids


async def test_missing_and_duplicate_call_ids_are_made_unique(store: SqliteStore) -> None:
    blank = ModelResponse(message=Message(role="assistant", content=[
        ToolUse(id="", name="Echo", input={}), ToolUse(id="", name="Echo", input={}),
        ToolUse(id="x", name="Echo", input={}), ToolUse(id="x", name="Echo", input={})]),
        stop_reason="tool_use")
    provider = ScriptedProvider([blank, reply("done")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Echo()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [replied] = [r for r in await client.events(tid) if isinstance(r.event, ModelReplied)
                 and r.event.response.message.tool_uses]
    assert isinstance(replied.event, ModelReplied)
    ids = [u.id for u in replied.event.response.message.tool_uses]
    assert len(set(ids)) == 4 and all(ids) and ids[2] == "x"
    finished = [r.event.result.call_id for r in await client.events(tid)
                if isinstance(r.event, ToolFinished)]
    assert sorted(finished) == sorted(ids)


# ------------------------------------------------------------ #14 + rewrites


class DenyAfterAsked(Extension):
    """Denies once an approval has been asked (a policy that changed)."""

    name = "late_deny"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Deny(reason="policy changed") if view.state.answers else None


class RewriteInput(Extension):
    name = "rewrite"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        if call.input.get("text") != "rewritten":
            return Rewrite(input={"text": "rewritten"})
        return None


async def test_an_approval_does_not_override_a_deny(store: SqliteStore) -> None:
    echo = Echo()
    provider = ScriptedProvider([reply("", ("Echo", {})), reply("done")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[echo],
                                        extensions=[AskAll(), DenyAfterAsked()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    w = last_event(await client.events(tid))
    await client.answer(tid, w.ids[0], "allow")
    await rt.run_until_idle()
    assert echo.inputs == []
    [result] = [r.event.result for r in await client.events(tid)
                if isinstance(r.event, ToolFinished)]
    assert result.is_error and result.text == "policy changed"


async def test_an_approved_call_still_runs_rewritten(store: SqliteStore) -> None:
    echo = Echo()
    provider = ScriptedProvider([reply("", ("Echo", {"text": "raw"})), reply("done")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[echo],
                                        extensions=[AskAll(), RewriteInput()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    w = last_event(await client.events(tid))
    assert isinstance(w, Waiting) and w.on == "approval"
    await client.answer(tid, w.ids[0], "allow")
    await rt.run_until_idle()
    assert echo.inputs == [{"text": "rewritten"}]


class RenameToGated(Extension):
    name = "rename"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Rewrite(input={}, name="Gated") if call.name == "Echo" else None


class DenyGated(Extension):
    name = "deny_gated"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Deny(reason="no Gated") if call.name == "Gated" else None


class RewriteForever(Extension):
    name = "loop"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Rewrite(input={"n": int(call.input.get("n", 0)) + 1})


async def test_a_rewritten_call_is_gated_again(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Echo", {})), reply("done")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Echo()],
                                        extensions=[RenameToGated(), DenyGated()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [result] = [r.event.result for r in await client.events(tid)
                if isinstance(r.event, ToolFinished)]
    assert result.is_error and result.text == "no Gated"


async def test_endless_rewrites_are_refused(store: SqliteStore) -> None:
    echo = Echo()
    provider = ScriptedProvider([reply("", ("Echo", {})), reply("done")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[echo],
                                        extensions=[RewriteForever()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [result] = [r.event.result for r in await client.events(tid)
                if isinstance(r.event, ToolFinished)]
    assert result.is_error and "rewritten more than 3 times" in result.text
    assert echo.inputs == []


class SeeAfter(Extension):
    name = "see"

    def __init__(self) -> None:
        self.names: list[str] = []

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Rewrite(input={"text": "x"}, name="Echo") if call.name == "Alias" else None

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        self.names.append(call.name)
        return output


async def test_after_tool_sees_the_rewritten_call(store: SqliteStore) -> None:
    see = SeeAfter()
    provider = ScriptedProvider([reply("", ("Alias", {})), reply("done")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Echo()], extensions=[see]))
    client = Client(store)
    await client.create("a", "go")
    await rt.run_until_idle()
    assert see.names == ["Echo"]


# ------------------------------------------------------------ #16 unattended asks


async def test_an_unattended_task_gives_its_claim_back_after_an_ask(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Echo", {}), ("Echo", {}))])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Echo()],
                                        extensions=[AskAll()]))
    client = Client(store)
    tid = await client.create("a", "go", config={"unattended": True})
    await rt.run_until_idle()
    row = await client.get(tid)
    assert row.status == "waiting" and row.waiting_on == "input" and row.owner is None
    w = last_event(await client.events(tid))
    assert isinstance(w, Waiting) and w.detail["turn_failed"].startswith("needs_human")
    # Both calls got a result: the conversation never keeps a call without one.
    finished = [r.event for r in await client.events(tid) if isinstance(r.event, ToolFinished)]
    assert len(finished) == 2 and all(f.result.is_error for f in finished)


# ------------------------------------------------------------ #13 #17 fold rules


def _rec(seq: int, ev: Any) -> Recorded:
    return Recorded(seq=seq, at=0, event=ev)


def _running_call() -> tuple[list[Recorded], ToolUse]:
    call = ToolUse(id="c1", name="Echo", input={})
    return [
        _rec(1, TaskCreated(agent="a")),
        _rec(2, InputTaken(items=[InboxItem(seq=1, at=0, body=Say(message=user("hi")))],
                           through=1)),
        _rec(3, ModelReplied(response=ModelResponse(
            message=Message(role="assistant", content=[call]), stop_reason="tool_use"))),
        _rec(4, ToolStarted(call=call)),
    ], call


def test_a_now_message_sent_with_a_stop_is_answered() -> None:
    recs, _ = _running_call()
    recs.append(_rec(5, InputTaken(items=[
        InboxItem(seq=2, at=0, body=Cancel(turn_only=True)),
        InboxItem(seq=3, at=0, body=Say(message=user("do this instead"), when="now"))],
        through=3)))
    st = fold(recs, {})
    assert isinstance(decide.next_action(st, one_shot=False, max_steps=100), decide.StopTurn)
    stopped = ToolFinished(result=ToolResult(call_id="c1", content=[Text(text="stopped")],
                                             is_error=True))
    st = fold([_rec(6, stopped), _rec(7, ExtensionRecorded(extension=CORE,
                                                           kind="turn_stopped"))], {}, state=st)
    assert st.messages[-1].text == "do this instead"
    assert isinstance(decide.next_action(st, one_shot=False, max_steps=100), decide.CallModel)


@pytest.mark.parametrize("kind", ["turn_failed", "turn_stopped"])
def test_held_messages_join_the_conversation_when_a_turn_closes(kind: str) -> None:
    recs, _ = _running_call()
    recs.append(_rec(5, InputTaken(items=[
        InboxItem(seq=2, at=0, body=Say(message=user("also this"), when="now"))], through=2)))
    st = fold(recs, {})
    assert [m.text for m in st.held] == ["also this"]
    st = fold([_rec(6, ExtensionRecorded(extension=CORE, kind=kind,
                                         data={"reason": "x"}))], {}, state=st)
    assert st.held == [] and st.messages[-1].text == "also this"
    assert st.turn_open
    assert isinstance(decide.next_action(st, one_shot=False, max_steps=100), decide.CallModel)


def test_an_answer_applies_only_to_its_own_batch() -> None:
    recs, _call = _running_call()
    recs.append(_rec(5, InputTaken(items=[
        InboxItem(seq=2, at=0, body=Answer(request_id="c1", decision="allow"))], through=2)))
    st = fold(recs, {})
    assert "c1" in st.answers
    nxt = ModelReplied(response=ModelResponse(
        message=Message(role="assistant", content=[ToolUse(id="c2", name="Echo", input={})]),
        stop_reason="tool_use"))
    done = ToolFinished(result=ToolResult(call_id="c1", content=[Text(text="ok")]))
    st = fold([_rec(6, done), _rec(7, nxt)], {}, state=st)
    assert st.answers == {} and st.call_ids == {"c1", "c2"}


# ------------------------------------------------------------ an interrupted reply


class _Stalls:
    """Streams a little of a reply, then never finishes (until cancelled);
    later calls answer from ``after``."""

    family = "scripted"

    def __init__(self, after: ScriptedProvider) -> None:
        self.after = after
        self.stalled = asyncio.Event()
        self.requests: list[Any] = []

    async def complete(self, request: Any, on_delta: Any = None) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) > 1:
            return await self.after.complete(request, on_delta)
        from noeta.core.types import Delta
        on_delta(Delta(kind="text", text="Floating point "))
        on_delta(Delta(kind="text", text="division"))
        self.stalled.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_a_reply_stopped_mid_stream_keeps_its_text_and_marks_the_next_turn(
        store: SqliteStore) -> None:
    from noeta.backends.providers.anthropic import AnthropicProvider

    provider = _Stalls(ScriptedProvider([reply("3 functions")]))
    rt = runtime(store, provider, Agent(name="a", model="m"))
    client = Client(store)
    tid = await client.create("a", "explain at length")
    running = asyncio.create_task(rt.run_until_idle())
    await asyncio.wait_for(provider.stalled.wait(), 5)
    await client.cancel(tid, "interrupted", turn_only=True)
    await asyncio.wait_for(running, 5)
    assert (await client.get(tid)).waiting_on == "input"
    await client.send(tid, "never mind: how many functions?")
    await rt.run_until_idle()
    messages = provider.requests[1].messages
    assert [(m.role, m.text, m.origin) for m in messages[-3:]] == [
        ("assistant", "Floating point division", None),
        ("user", "[Request interrupted by user]\n", "inline"),
        ("user", "never mind: how many functions?", "human")]
    # On the wire the marker and the next prompt share one user turn.
    wire = AnthropicProvider(api_key="k").build_body(provider.requests[1])["messages"]
    assert [b["text"] for b in wire[-1]["content"]] == [
        "[Request interrupted by user]\n", "never mind: how many functions?"]


class _StallsAfterThinking(_Stalls):
    """Finishes a signed thinking block (and an empty one), streams some
    text, then starts a thinking block it never finishes."""

    async def complete(self, request: Any, on_delta: Any = None) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) > 1:
            return await self.after.complete(request, on_delta)
        on_delta(Delta(kind="thinking", text="Plan the answer."))
        on_delta(Delta(kind="block", block=Thinking(text="Plan the answer.", signature="sig",
                                                    family="anthropic")))
        on_delta(Delta(kind="block", block=Thinking(text="", signature="", family="anthropic")))
        on_delta(Delta(kind="text", text="Floating point "))
        on_delta(Delta(kind="thinking", text="half a thought"))
        self.stalled.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_a_reply_stopped_mid_stream_keeps_the_thinking_it_finished(
        store: SqliteStore) -> None:
    from noeta.backends.providers.anthropic import AnthropicProvider

    provider = _StallsAfterThinking(ScriptedProvider([reply("3 functions")]))
    seen: list[str] = []
    rt = runtime(store, provider, Agent(name="a", model="m"),
                 on_delta=lambda _tid, d: seen.append(d.kind))
    client = Client(store)
    tid = await client.create("a", "explain at length")
    running = asyncio.create_task(rt.run_until_idle())
    await asyncio.wait_for(provider.stalled.wait(), 5)
    await client.cancel(tid, "interrupted", turn_only=True)
    await asyncio.wait_for(running, 5)
    await client.send(tid, "never mind")
    await rt.run_until_idle()
    wire = AnthropicProvider(api_key="k").build_body(provider.requests[1])["messages"]
    assert wire[-2] == {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "Plan the answer.", "signature": "sig"},
        {"type": "text", "text": "Floating point "}]}
    # A finished block is the runner's, never a front end's.
    assert "block" not in seen


def test_a_stop_recorded_before_thinking_was_kept_folds_to_its_text() -> None:
    recs, _ = _running_call()
    old = ExtensionRecorded(extension=CORE, kind="turn_stopped",
                            data={"partial": "Floating", "marker": "[stopped]\n"})
    st = fold([*recs[:2], _rec(3, old)], {})
    assert [(m.role, m.content) for m in st.messages[-2:]] == [
        ("assistant", [Text(text="Floating")]), ("user", [Text(text="[stopped]\n")])]
    kept = TurnStopped(partial="x", marker="m", thinking=[Thinking(text="t", signature="s")])
    assert TurnStopped.parse(kept.data()) == kept
