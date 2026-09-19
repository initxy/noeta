"""Approval at the two Guard *decision* points — ``before_finish`` and
``before_spawn_subtask``.

A Guard returning ``require_approval`` there suspends the Task on
``approval-finish-{task_id}`` / ``approval-spawn-{task_id}``. These tests
prove the exit exists and does the right thing:

* the suspend records the same ``ToolCallApprovalRequested`` anchor a gated
  tool call does, under the reserved ``finish-{task_id}`` /
  ``spawn-{task_id}`` call_id — the id a host reads off the event and hands
  to ``approve`` / ``deny``;
* ``approve`` proceeds with the decision the human reviewed (the answer
  ships, the sub-agent launches) without asking the model again and without
  re-consulting the Guard that held it;
* ``deny`` returns the refusal to the model the way a denied tool call does,
  so the turn continues instead of dying;
* each of those works from a host that has never seen the Task, rebuilding
  it from durable state alone.

The runtime-level counterpart for a gated **tool call** is
``tests/test_tool_approval.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._sdk_session import make_driver, make_host, official_registry

from noeta.client.host import SdkHost
from noeta.core.fold import fold
from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedFinish,
    ProposedSpawnSubtask,
    VerdictResult,
)
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _end_turn(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _spawn_call(call_id: str = "s1") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name="Task",
                arguments={"agent": "explore", "goal": "scout"},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


class _RequireApprovalOnFinish:
    """The documented compliance shape: a human reviews the answer before it
    ships."""

    name = "approve-finish"
    priority = 10

    def check(
        self, action: ProposedAction, ctx: GuardContext  # noqa: ARG002
    ) -> VerdictResult:
        if isinstance(action, ProposedFinish):
            return VerdictResult.require_approval("review before finish")
        return VerdictResult.allow()


class _RequireApprovalOnSpawn:
    """The other documented shape: a human approves the delegation."""

    name = "approve-spawn"
    priority = 10

    def check(
        self, action: ProposedAction, ctx: GuardContext  # noqa: ARG002
    ) -> VerdictResult:
        if isinstance(action, ProposedSpawnSubtask):
            return VerdictResult.require_approval("review before delegate")
        return VerdictResult.allow()


def _host(
    workspace: Path,
    *,
    responses: list[LLMResponse],
    guards: tuple[Any, ...],
    sqlite_path: str | None = None,
) -> SdkHost:
    """A one-shot (``multi_turn=False``) host, so a finishing turn produces a
    real ``FinishDecision`` and the ``before_finish`` action point fires."""
    return make_host(
        official_registry(),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=list(responses)),
        multi_turn=False,
        extra_guards=guards,
        sqlite_path=sqlite_path,
    )


def _pending_call_id(host: SdkHost, task_id: str) -> str:
    """Discover the pending approval exactly the way a host does: read the
    ``call_id`` off the newest ``ToolCallApprovalRequested``."""
    anchors = [
        e
        for e in host.event_log.read(task_id)
        if e.type == "ToolCallApprovalRequested"
    ]
    assert anchors, "the suspend recorded no approval anchor"
    return str(anchors[-1].payload.call_id)


def _types(host: SdkHost, task_id: str) -> list[str]:
    return [e.type for e in host.event_log.read(task_id)]


def _requests(host: SdkHost) -> list[Any]:
    provider = host.default_provider_instance
    assert isinstance(provider, FakeLLMProvider)
    return provider.received_requests


def _text(message: Any) -> str:
    return " ".join(
        b.text for b in message.content if isinstance(b, TextBlock)
    )


# ---------------------------------------------------------------------------
# before_finish
# ---------------------------------------------------------------------------


def test_finish_approval_suspends_with_a_resolvable_anchor(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host = _host(
        ws,
        responses=[_end_turn("the answer")],
        guards=(_RequireApprovalOnFinish(),),
    )
    out = make_driver(host).start(goal="answer me", agent="main")

    assert out.status == "suspended"
    assert out.wake_handle == f"approval-finish-{out.task_id}"
    # The handle is derived from the anchor's call_id, so the documented
    # recipe ("read the call_id off ToolCallApprovalRequested") works.
    assert _pending_call_id(host, out.task_id) == f"finish-{out.task_id}"
    # The blocked decision is durably recoverable, not just in memory.
    task = fold(host.event_log, host.content_store, out.task_id)
    pending = task.governance.pending_approvals[f"finish-{out.task_id}"]
    assert pending["tool_name"] == "finish"
    assert pending["arguments"]["answer"] == "the answer"
    assert "TaskCompleted" not in _types(host, out.task_id)


def test_approve_ships_the_reviewed_answer(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host = _host(
        ws,
        responses=[_end_turn("the answer")],
        guards=(_RequireApprovalOnFinish(),),
    )
    driver = make_driver(host)
    out = driver.start(goal="answer me", agent="main")

    out = driver.approve(
        out.task_id, call_id=_pending_call_id(host, out.task_id),
        reason="looks good",
    )

    assert out.status == "terminal"
    types = _types(host, out.task_id)
    assert "TaskCompleted" in types
    assert types.index("ToolCallApprovalResolved") < types.index("TaskCompleted")
    completed = [
        e for e in host.event_log.read(out.task_id) if e.type == "TaskCompleted"
    ][0]
    assert completed.payload.answer == "the answer"
    # The ORIGINAL decision proceeded: the model was never asked a second
    # time, and the Guard was not re-consulted (no second suspend).
    assert len(_requests(host)) == 1
    assert types.count("TaskSuspended") == 1
    # ...and the answer is recorded once, not duplicated by the resume.
    task = fold(host.event_log, host.content_store, out.task_id)
    assert [
        _text(m) for m in task.runtime.messages if m.role == "assistant"
    ] == ["the answer"]


def test_deny_returns_the_refusal_and_the_turn_continues(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host = _host(
        ws,
        responses=[_end_turn("first answer"), _end_turn("second answer")],
        guards=(_RequireApprovalOnFinish(),),
    )
    driver = make_driver(host)
    out = driver.start(goal="answer me", agent="main")

    out = driver.deny(
        out.task_id, call_id=_pending_call_id(host, out.task_id),
        reason="cite your sources",
    )

    # The turn CONTINUED — the model was asked again and proposed a second
    # answer, which the same Guard gates again (a live, resolvable state).
    assert len(_requests(host)) == 2
    assert out.status == "suspended"
    assert out.wake_handle == f"approval-finish-{out.task_id}"
    types = _types(host, out.task_id)
    assert "TaskFailed" not in types
    assert "TaskCompleted" not in types
    # The denial is visible to the model: recorded as a system-origin turn
    # and carried into the request that followed it.
    task = fold(host.event_log, host.content_store, out.task_id)
    denial = [
        m
        for m in task.runtime.messages
        if m.role == "user" and m.origin == "system"
        and "cite your sources" in _text(m)
    ]
    assert len(denial) == 1
    assert any(
        "cite your sources" in _text(m) for m in _requests(host)[-1].messages
    )


# ---------------------------------------------------------------------------
# before_spawn_subtask
# ---------------------------------------------------------------------------


def test_approve_launches_the_reviewed_delegation(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host = _host(
        ws,
        responses=[_spawn_call(), _end_turn("scouted"), _end_turn("done")],
        guards=(_RequireApprovalOnSpawn(),),
    )
    driver = make_driver(host)
    out = driver.start(goal="delegate the scouting", agent="main")

    assert out.status == "suspended"
    assert out.wake_handle == f"approval-spawn-{out.task_id}"
    call_id = _pending_call_id(host, out.task_id)
    assert call_id == f"spawn-{out.task_id}"
    # The held delegation spec is what the human reviews — and what runs.
    task = fold(host.event_log, host.content_store, out.task_id)
    assert task.governance.pending_approvals[call_id] == {
        "tool_name": "spawn_subtask",
        "arguments": {
            "agent_name": "explore",
            "goal": "scout",
            "inputs": {},
            "background": False,
        },
    }
    assert "SubtaskSpawned" not in _types(host, out.task_id)

    out = driver.approve(out.task_id, call_id=call_id)

    assert out.status == "terminal"
    types = _types(host, out.task_id)
    assert "SubtaskSpawned" in types
    assert types.index("ToolCallApprovalResolved") < types.index("SubtaskSpawned")
    spawned = [
        e for e in host.event_log.read(out.task_id) if e.type == "SubtaskSpawned"
    ][0]
    assert spawned.payload.agent_name == "explore"
    assert spawned.payload.goal == "scout"


def test_spawn_deny_answers_the_call_and_the_turn_continues(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host = _host(
        ws,
        responses=[_spawn_call(), _end_turn("did it myself")],
        guards=(_RequireApprovalOnSpawn(),),
    )
    driver = make_driver(host)
    out = driver.start(goal="delegate the scouting", agent="main")

    out = driver.deny(
        out.task_id, call_id=_pending_call_id(host, out.task_id),
        reason="no delegation today",
    )

    # No child, no failed Task — the model got the refusal as the result of
    # its own ``Task`` call and finished the work itself.
    types = _types(host, out.task_id)
    assert "SubtaskSpawned" not in types
    assert "TaskFailed" not in types
    assert out.status == "terminal"
    task = fold(host.event_log, host.content_store, out.task_id)
    denials = [
        b
        for m in task.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.call_id == "s1"
    ]
    assert len(denials) == 1
    assert denials[0].success is False
    assert denials[0].error == "no delegation today"


# ---------------------------------------------------------------------------
# restart: resolve from a host that has never seen the Task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("approved", [True, False])
def test_finish_approval_resolves_on_a_fresh_host(
    tmp_path: Path, approved: bool
) -> None:
    """The anchor is durable, so the approve/deny may land on a different
    process: a second host over the same sqlite storage rebuilds the Task
    from the EventLog alone and resolves it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    db = str(tmp_path / "noeta.sqlite")
    first = _host(
        ws,
        responses=[_end_turn("the answer")],
        guards=(_RequireApprovalOnFinish(),),
        sqlite_path=db,
    )
    out = make_driver(first).start(goal="answer me", agent="main")
    assert out.status == "suspended"
    task_id = out.task_id
    call_id = _pending_call_id(first, task_id)

    # A brand-new host + driver + dispatcher over the same durable storage:
    # nothing of the suspended turn survives in memory.
    second = _host(
        ws,
        responses=[_end_turn("second answer")],
        guards=(_RequireApprovalOnFinish(),),
        sqlite_path=db,
    )
    driver = make_driver(second)
    out = (
        driver.approve(task_id, call_id=call_id)
        if approved
        else driver.deny(task_id, call_id=call_id, reason="cite your sources")
    )

    types = _types(second, task_id)
    if approved:
        assert out.status == "terminal"
        completed = [
            e
            for e in second.event_log.read(task_id)
            if e.type == "TaskCompleted"
        ][0]
        # The answer recovered from the log, not from a second model call.
        assert completed.payload.answer == "the answer"
        assert _requests(second) == []
    else:
        assert out.status == "suspended"
        assert out.wake_handle == f"approval-finish-{task_id}"
        assert "TaskFailed" not in types
        assert len(_requests(second)) == 1


@pytest.mark.parametrize("approved", [True, False])
def test_spawn_approval_resolves_on_a_fresh_host(
    tmp_path: Path, approved: bool
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    db = str(tmp_path / "noeta.sqlite")
    first = _host(
        ws,
        responses=[_spawn_call()],
        guards=(_RequireApprovalOnSpawn(),),
        sqlite_path=db,
    )
    out = make_driver(first).start(goal="delegate the scouting", agent="main")
    assert out.status == "suspended"
    task_id = out.task_id
    call_id = _pending_call_id(first, task_id)

    second = _host(
        ws,
        responses=[_end_turn("scouted"), _end_turn("done")],
        guards=(_RequireApprovalOnSpawn(),),
        sqlite_path=db,
    )
    driver = make_driver(second)
    out = (
        driver.approve(task_id, call_id=call_id)
        if approved
        else driver.deny(task_id, call_id=call_id, reason="no delegation today")
    )

    types = _types(second, task_id)
    assert out.status == "terminal"
    assert "TaskFailed" not in types
    if approved:
        # The delegation the first host recorded is the one this host runs.
        spawned = [
            e
            for e in second.event_log.read(task_id)
            if e.type == "SubtaskSpawned"
        ][0]
        assert spawned.payload.agent_name == "explore"
        assert spawned.payload.goal == "scout"
    else:
        assert "SubtaskSpawned" not in types
