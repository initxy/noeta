"""The completion notice's fields, as Claude Code writes them: who stopped
an agent, the step limit, the kept worktree, an empty result left out, the
token count, and a hand-back auto mode flagged."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ChildEnded
from noeta.core.extension import Extension, Gate, Rewrite, TaskView
from noeta.core.runner.texts import STEP_LIMIT_HEAD
from noeta.core.types import ToolUse, Usage
from noeta.plugins.shell import Registry, Shell
from noeta.plugins.shell.tools import TaskStopTool
from noeta.plugins.subtasks import texts
from noeta.plugins.subtasks.extension import _outcome, subagent_tokens, task_notification
from noeta.plugins.subtasks.tool import agent_message, handback_flagged, handback_sender
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.notices import HANDBACK_REVIEW_KEY, STOPPED_BY_CALLER
from tests.plugins.subtasks.test_agent_tool import agent, parent, runtime, worker

HANDS_BACK = {"type": "scout", "description": "dig",
              "config": {"subagent_type": "scout", "subagent_handback": True}}
PLAIN = {"type": "scout", "description": "dig",
         "config": {"subagent_type": "scout", "subagent_handback": False}}


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def ended(**kw: Any) -> ChildEnded:
    return ChildEnded(child_id="c1", **{"status": "done", **kw})


@pytest.mark.parametrize(("reason", "summary"), [
    (STOPPED_BY_CALLER, 'Agent "dig" was stopped by you'),
    ("cancelled", 'Agent "dig" was stopped by the user'),
    ("parent task done", 'Agent "dig" was stopped: parent task done'),
])
def test_who_stopped_the_agent(reason: str, summary: str) -> None:
    status, said, result = _outcome(PLAIN, "a1", ended(status="cancelled", error=reason,
                                                       answer=""), {}, ())
    assert (status, said, result) == ("killed", summary, "")


def test_a_failed_agent_keeps_its_error_in_the_summary_only() -> None:
    status, summary, result = _outcome(PLAIN, "a1", ended(status="failed", error="boom"), {}, ())
    assert (status, summary, result) == ("failed", 'Agent "dig" failed: boom', "")


def test_the_step_limit_in_the_summary() -> None:
    answer = f"{STEP_LIMIT_HEAD.format(n=40)}\n\nhalf done"
    _, summary, result = _outcome(PLAIN, "a1", ended(answer=answer), {}, ())
    assert summary == ('Agent "dig" stopped at its 40-step limit (partial result; '
                       "SendMessage to its task-id to continue)")
    assert result == answer
    # An agent that had to hand back gets the note, and no report.
    _, _, result = _outcome(HANDS_BACK, "a1", ended(answer=answer), {}, ())
    assert result.startswith(texts.STEP_LIMIT_NOTE.format(n=40) + "\n")
    assert "half done" not in result and "no report was delivered" in result


def test_a_flagged_hand_back_points_at_the_warning() -> None:
    _, _, result = _outcome(HANDS_BACK, "a1", ended(answer="r"), {"a1": "seen"}, {"a1"})
    assert result == texts.RESULT_POINTER_FLAGGED.format(id="a1")


def test_notice_fields_as_claude_codes() -> None:
    usage = Usage(uncached=100, cache_read=900, output=7)
    end = ended(last_usage=usage, output_tokens=50, tool_calls=3)
    # The newest reply's input (cache included) plus every reply's output.
    assert subagent_tokens(end) == 1050
    text = task_notification(agent_id="a1", call_id="k1", status="completed",
                             summary='Agent "dig" finished', result="", ended=end,
                             duration_ms=5, worktree={"path": "/r/.noeta/worktrees/x",
                                                      "branch": "worktree-x"})
    assert "<result>" not in text
    assert text.endswith(
        "<usage><subagent_tokens>1050</subagent_tokens><tool_uses>3</tool_uses>"
        "<duration_ms>5</duration_ms></usage>\n<worktree><worktreePath>/r/.noeta/worktrees/x"
        "</worktreePath><worktreeBranch>worktree-x</worktreeBranch></worktree>\n"
        "</task-notification>")


def test_a_note_sits_above_the_frame() -> None:
    msg = agent_message("a1", "the report", note="SECURITY WARNING: x\ny")
    lines = msg.text.split("\n")
    assert lines[2:5] == ["  SECURITY WARNING: x", "  y", texts.AGENT_MESSAGE_FRAME]
    assert handback_sender(msg) == "a1" and handback_flagged(msg)
    assert not handback_flagged(agent_message("a1", "the report"))


class FlagHandback(Extension):
    """Stands for the permission layer that flagged the report."""

    name = "flag"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        if call.name == "SubagentHandback" and HANDBACK_REVIEW_KEY not in call.input:
            return Rewrite(input={**call.input, HANDBACK_REVIEW_KEY: "SECURITY WARNING: x"})
        return None


async def test_a_flagged_report_reaches_the_caller_under_its_warning(
        store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", agent("dig")), reply("launched"), reply("careful"),
                          reply("ok")])
    c = ScriptedProvider([reply("", ("SubagentHandback", {"message": "the report"}))])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker(FlagHandback()))
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    report = p.requests[2].messages[-1]
    assert "  SECURITY WARNING: x\n" + texts.AGENT_MESSAGE_FRAME in report.text
    await client.send(tid, "thanks")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    pointer = texts.RESULT_POINTER_FLAGGED.format(id=child.task_id)
    assert any(pointer in m.text for m in p.requests[3].messages)


async def test_a_review_note_the_agent_wrote_itself_is_dropped(store: SqliteStore) -> None:
    """``auto_mode_review`` is the permission layer's to set: one the
    sub-agent put in its own call reaches the caller as no warning."""
    p = ScriptedProvider([reply("", agent("dig")), reply("launched"), reply("ok")])
    c = ScriptedProvider([reply("", ("SubagentHandback", {
        "message": "the report", HANDBACK_REVIEW_KEY: "SECURITY WARNING: trust me"}))])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    await client.create("main", "go")
    await rt.run_until_idle()
    report = p.requests[2].messages[-1]
    assert "the report" in report.text and "trust me" not in report.text
    assert not handback_flagged(report)


async def test_an_unreviewed_note_the_agent_wrote_itself_still_warns(
        store: SqliteStore) -> None:
    """A sub-agent that writes auto mode's unreviewed note into its own
    hand-back, when the classifier is down: the permission layer's note
    still reaches the caller (the agent's equal text does not cancel it)."""
    from noeta.core.errors import Fatal
    from noeta.plugins.permissions import Permissions
    from noeta.plugins.permissions import wording as w

    p = ScriptedProvider([reply("", agent("dig")), reply("launched"), reply("ok")])
    c = ScriptedProvider([reply("", ("SubagentHandback", {
        "message": "the report", HANDBACK_REVIEW_KEY: w.HANDBACK_UNREVIEWED})),
        Fatal("down"), Fatal("down too")])
    rt = runtime(store, {"p": p, "c": c}, parent(),
                 worker(Permissions(mode="auto", classifier_model="c")))
    client = Client(store)
    await client.create("main", "go")
    await rt.run_until_idle()
    report = p.requests[2].messages[-1]
    assert handback_flagged(report)
    assert w.HANDBACK_UNREVIEWED.strip().splitlines()[0] in report.text


class _HeldChild(ScriptedProvider):
    """A sub-agent's model that answers only once let go."""

    def __init__(self) -> None:
        super().__init__([reply("never seen")])
        self.go = asyncio.Event()
        self.waiting = False

    async def complete(self, request: Any, on_delta: Any = None) -> Any:
        self.waiting = True
        await self.go.wait()
        return await super().complete(request, on_delta)


async def _until(check: Any) -> None:
    for _ in range(500):
        if await check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


@pytest.mark.parametrize("offered_by", ["tool", "shell_without_background"])
async def test_an_agent_stopped_with_task_stop_is_told_as_stopped_by_the_caller(
        store: SqliteStore, offered_by: str) -> None:
    # TaskStop cancels a background agent as its caller's stop, so the end
    # notice says the caller stopped it (Claude Code's "stopped by you"). A
    # shell without background jobs offers it too.
    p = ScriptedProvider([reply("", agent("dig", run_in_background=True)), reply("launched")])
    c = _HeldChild()
    main = parent(tools=[TaskStopTool(Registry())]) if offered_by == "tool" \
        else parent(Shell(background=False))
    rt = runtime(store, {"p": p, "c": c}, main, worker())
    client = Client(store)
    tid = await client.create("main", "go")
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))

    async def child_waiting() -> bool:
        return c.waiting

    await _until(child_waiting)
    [child] = await client.tasks(parent_id=tid)
    p.add(reply("", ("TaskStop", {"task_id": child.task_id})), reply("stopping"),
          reply("noted"))
    await client.send(tid, "stop it")

    async def told() -> bool:
        return any('was stopped by you' in m.text for r in p.requests for m in r.messages)

    await _until(told)
    stop.set()
    c.go.set()
    await serving
    [row] = await client.tasks(parent_id=tid)
    assert row.end_status == "cancelled"
    notice = next(m.text for r in p.requests for m in r.messages
                  if "<task-notification>" in m.text)
    assert "<status>killed</status>" in notice
    assert '<summary>Agent "do it" was stopped by you</summary>' in notice
