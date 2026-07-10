"""CW6 — code-session inspect / tail read-models (library-level, read-only).

Gates the data layer that ``noeta code inspect`` / ``noeta code tail`` render
(the operator-CLI surface itself was retired in the three-layer split — its
stdout/exit-code/argparse UX is no longer under test here):
* ``build_code_session_detail`` — summary fidelity for a next-goal-resumable
  session (agent/model/goal/status_text/wake_kind); active-skill fidelity;
  approval call_id surfaced (hands it to the future ``approve``); MCP /
  delegation sessions OBSERVABLE (detail folds, the inverse of resume's
  refusal — regression guard); closed session; files-changed / tool-calls;
  plan/todo read-model surface (CW18a) incl. non-JSON-native plainify (W5).
* ``tail_event_rows`` — ascending-seq order; ``after_seq`` cursor contract;
  ``detail`` gloss present for varied event types, never raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.client import SdkHost
from noeta.core.fold import fold
from noeta.agent.read_models.detail import build_code_session_detail
from noeta.agent.read_models.tail import tail_event_rows
from noeta.policies.react import spawn_subagent_tool_schema
from noeta.protocols.events import (
    ConversationClosedPayload,
    LLMRequestStartedPayload,
    ModelBoundPayload,
    ToolCallApprovalRequestedPayload,
    ToolCallStartedPayload,
    ToolResultRecordedPayload,
    TaskCreatedPayload,
    TaskStartedPayload,
    TaskStatePatchedPayload,
    TaskSuspendedPayload,
)
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.protocols.wake import HumanResponseReceived
from noeta.storage.sqlite import SqliteReadOnlyStore
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.testing.profile import build_sqlite_stack
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    coding_replay_budget,
    official_registry as official_agent_registry,
)

import json


_SKILL = """\
---
name: tidy-up
description: keep edits minimal
priority: 50
---
1. Read the file.
2. Make the smallest change.
"""


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _close(*objs: Any) -> None:
    for obj in objs:
        close = getattr(obj, "close", None)
        if callable(close):
            close()


def _seed(db: Path) -> tuple[Any, Any, Any]:
    return build_sqlite_stack(str(db))


def _detail(db: Path, task_id: str) -> Any:
    store = SqliteReadOnlyStore(str(db))
    try:
        return build_code_session_detail(store, store, task_id)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Recording helpers
# ---------------------------------------------------------------------------


def _record_next_goal(db: Path, ws: Path) -> str:
    """Real next-goal-suspended session (with opening ModelBound) via the
    shared InteractionDriver over a sqlite store — agent=default, model=gpt-test."""
    log, cs, disp = build_sqlite_stack(str(db))
    host = SdkHost(
        event_log=log, content_store=cs, dispatcher=disp,
        provider=FakeLLMProvider(responses=[_end_turn("t1")]),
        model="gpt-test", workspace_dir=ws,
        write_mode=FsWriteMode.DRY_RUN, shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
        require_approval_tools=(),
    )
    started = InteractionDriver(host).start(goal="first goal", agent="main")
    assert started.status == "suspended"
    _close(log, cs, disp)
    return started.task_id


def _record_with_skill(db: Path, ws: Path) -> str:
    """Next-goal-suspended session with an active workspace skill (no ModelBound),
    driven through the production SdkHost + InteractionDriver over a sqlite store
    (multi_turn) with a pre-loop skill activation (``extra_skills`` → the driver's
    ``activations=``)."""
    skills = ws / ".noeta" / "skills"
    (skills / "tidy-up").mkdir(parents=True)
    (skills / "tidy-up" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    log, cs, disp = build_sqlite_stack(str(db))
    host = SdkHost(
        event_log=log, content_store=cs, dispatcher=disp,
        provider=FakeLLMProvider(responses=[_end_turn("t1")]),
        model="gpt-test", workspace_dir=ws,
        write_mode=FsWriteMode.DRY_RUN, shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
        require_approval_tools=(),
        budget=coding_replay_budget(None),
    )
    started = InteractionDriver(host).start(
        goal="first", agent="main", activations=("tidy-up",)
    )
    assert started.status == "suspended"
    folded = fold(log, cs, started.task_id)
    assert "tidy-up" in folded.state.active_skills
    _close(log, cs, disp)
    return started.task_id


def _emit_created_started(log: Any, task_id: str, *, agent: str = "default") -> None:
    log.emit(
        task_id=task_id, type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="g", policy_name="react", agent_name=agent
        ),
    )
    log.emit(
        task_id=task_id, type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
    )


def _suspend_next_goal(log: Any, task_id: str) -> None:
    from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE

    log.emit(
        task_id=task_id, type="TaskSuspended",
        payload=TaskSuspendedPayload(
            reason="waiting_human",
            wake_on=HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE),
        ),
    )


def _record_approval(db: Path) -> str:
    """Approval-suspended session: a pending fs_write approval (call_id=c1)."""
    log, cs, disp = _seed(db)
    _emit_created_started(log, "t1")
    log.emit(
        task_id="t1", type="ToolCallApprovalRequested",
        payload=ToolCallApprovalRequestedPayload(
            call_id="c1", tool_name="fs_write", arguments={"path": "a.py"}
        ),
    )
    log.emit(
        task_id="t1", type="TaskSuspended",
        payload=TaskSuspendedPayload(
            reason="waiting_human",
            wake_on=HumanResponseReceived(handle="approval-c1"),
        ),
    )
    _close(log, cs, disp)
    return "t1"


# ---------------------------------------------------------------------------
# inspect — fidelity (build_code_session_detail)
# ---------------------------------------------------------------------------


def test_inspect_next_goal_summary_fidelity(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    db = tmp_path / "s.db"
    task_id = _record_next_goal(db, ws)
    detail = _detail(db, task_id)
    assert detail is not None
    # Note: agent="main" was passed in, so the recorded value is the canonical name "main".
    assert detail.agent == "main"
    assert detail.model == "gpt-test"
    assert detail.goal == "first goal"
    assert detail.status_text == "resumable"
    assert detail.wake_kind == "next-goal"
    assert detail.approval_call_id is None


def test_inspect_active_skill_fidelity(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    db = tmp_path / "s.db"
    task_id = _record_with_skill(db, ws)
    detail = _detail(db, task_id)
    assert detail is not None
    assert "tidy-up" in detail.active_skills
    assert detail.status_text == "resumable"


def test_inspect_approval_call_id_visible(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    task_id = _record_approval(db)
    # The fields that unblock approve — surfaced on the folded detail.
    detail = _detail(db, task_id)
    assert detail is not None
    assert detail.status_text == "awaiting approval"
    assert detail.wake_kind == "approval"
    assert detail.wake_handle == "approval-c1"
    assert detail.approval_call_id == "c1"
    assert detail.pending_approvals == (
        {"call_id": "c1", "tool_name": "fs_write"},
    )


def test_inspect_mcp_session_observable(tmp_path: Path) -> None:
    """MCP session folds into a normal detail — the INVERSE of resume's refusal."""
    db = tmp_path / "s.db"
    log, cs, disp = _seed(db)
    _emit_created_started(log, "t1")
    body = json.dumps(
        {"tools": [{"type": "function", "function": {"name": "mcp__srv__do"}}]}
    ).encode("utf-8")
    ref = cs.put(body, media_type="application/json")
    log.emit(
        task_id="t1", type="LLMRequestStarted",
        payload=LLMRequestStartedPayload(
            call_id="c0", model="gpt-test", request_ref=ref, selection=None
        ),
    )
    _suspend_next_goal(log, "t1")
    _close(log, cs, disp)
    detail = _detail(db, "t1")
    assert detail is not None  # observable, NOT a "resume of MCP not supported" refusal
    assert detail.status_text == "resumable"


def test_inspect_delegation_session_observable(tmp_path: Path) -> None:
    """Delegation session folds into a normal detail — inverse of resume's refusal."""
    db = tmp_path / "s.db"
    log, cs, disp = _seed(db)
    _emit_created_started(log, "t1")
    spawn = spawn_subagent_tool_schema()["function"]["name"]
    body = json.dumps(
        {"tools": [{"type": "function", "function": {"name": spawn}}]}
    ).encode("utf-8")
    ref = cs.put(body, media_type="application/json")
    log.emit(
        task_id="t1", type="LLMRequestStarted",
        payload=LLMRequestStartedPayload(
            call_id="c0", model="gpt-test", request_ref=ref, selection=None
        ),
    )
    _suspend_next_goal(log, "t1")
    _close(log, cs, disp)
    detail = _detail(db, "t1")
    assert detail is not None  # a normal summary, no refusal
    assert detail.status_text == "resumable"


def test_inspect_closed_session(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    log, cs, disp = _seed(db)
    _emit_created_started(log, "t1")
    _suspend_next_goal(log, "t1")
    log.emit(
        task_id="t1", type="ConversationClosed",
        payload=ConversationClosedPayload(closed_by="leo", reason="all done"),
    )
    _close(log, cs, disp)
    detail = _detail(db, "t1")
    assert detail is not None
    assert detail.closed is True
    assert detail.status_text == "closed"
    assert detail.closed_by == "leo"
    assert detail.close_reason == "all done"


def test_inspect_files_changed_and_tool_calls(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    log, cs, disp = _seed(db)
    _emit_created_started(log, "t1")
    log.emit(
        task_id="t1", type="ToolCallStarted",
        payload=ToolCallStartedPayload(
            call_id="w1", tool_name="fs_write", arguments={"path": "src/a.py"}
        ),
    )
    out_ref = cs.put(b"ok", media_type="text/plain")
    log.emit(
        task_id="t1", type="ToolResultRecorded",
        payload=ToolResultRecordedPayload(
            call_id="w1", success=True, output_ref=out_ref, summary="wrote",
            side_effects=[{"path": "src/a.py"}],
        ),
    )
    _suspend_next_goal(log, "t1")
    _close(log, cs, disp)

    detail = _detail(db, "t1")
    assert detail is not None
    assert detail.recent_tool_calls == (
        {"call_id": "w1", "tool_name": "fs_write"},
    )
    assert detail.files_changed == ("src/a.py",)
    assert detail.tool_calls == 1


# ---------------------------------------------------------------------------
# tail (tail_event_rows)
# ---------------------------------------------------------------------------


def _record_varied(db: Path) -> str:
    """A stream covering many event types — for ordering / detail-gloss gates."""
    log, cs, disp = _seed(db)
    log.emit(
        task_id="t1", type="TaskCreated",
        payload=TaskCreatedPayload(goal="hello", policy_name="react", agent_name="default"),
    )
    log.emit(task_id="t1", type="TaskStarted", payload=TaskStartedPayload(lease_id="L"))
    log.emit(
        task_id="t1", type="ModelBound",
        payload=ModelBoundPayload(model="gpt-test", principal_identity="leo"),
    )
    log.emit(
        task_id="t1", type="ToolCallStarted",
        payload=ToolCallStartedPayload(call_id="w1", tool_name="fs_write", arguments={}),
    )
    log.emit(
        task_id="t1", type="ToolCallApprovalRequested",
        payload=ToolCallApprovalRequestedPayload(call_id="w1", tool_name="fs_write", arguments={}),
    )
    _suspend_next_goal(log, "t1")
    log.emit(
        task_id="t1", type="ConversationClosed",
        payload=ConversationClosedPayload(closed_by="leo", reason="done"),
    )
    _close(log, cs, disp)
    return "t1"


def test_tail_ascending_order(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _record_varied(db)
    store = SqliteReadOnlyStore(str(db))
    try:
        rows, _ = tail_event_rows(store, "t1", after_seq=None)
    finally:
        store.close()
    seqs = [r.seq for r in rows]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))  # no dupes, strictly append order


def test_tail_since_seq_and_cursor(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _record_varied(db)
    store = SqliteReadOnlyStore(str(db))
    try:
        rows, cursor = tail_event_rows(store, "t1", after_seq=None)
        assert [r.seq for r in rows] == sorted(r.seq for r in rows)
        # A second poll from the advanced cursor yields nothing new.
        rows2, cursor2 = tail_event_rows(store, "t1", after_seq=cursor)
        assert rows2 == []
        assert cursor2 == cursor
        # after_seq N keeps only seq > N.
        midpoint = rows[0].seq
        only_after, _ = tail_event_rows(store, "t1", after_seq=midpoint)
        assert all(r.seq > midpoint for r in only_after)
    finally:
        store.close()


def test_tail_detail_never_raises(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _record_varied(db)
    store = SqliteReadOnlyStore(str(db))
    try:
        rows, _ = tail_event_rows(store, "t1", after_seq=None)
    finally:
        store.close()
    types = {r.type for r in rows}
    assert {"TaskCreated", "ModelBound", "ToolCallStarted", "TaskSuspended",
            "ConversationClosed"} <= types
    for r in rows:
        assert isinstance(r.detail, str)  # gloss present, never raised
    # spot-check a couple of glosses
    created = next(r for r in rows if r.type == "TaskCreated")
    assert "agent=default" in created.detail
    bound = next(r for r in rows if r.type == "ModelBound")
    assert "model=gpt-test" in bound.detail


# ---------------------------------------------------------------------------
# CW18a — plan/todo read-model surface in the detail fold
# ---------------------------------------------------------------------------


def _seed_with_plan(db: Path) -> str:
    log, cs, disp = _seed(db)
    _emit_created_started(log, "t1")
    log.emit(
        task_id="t1", type="TaskStatePatched",
        payload=TaskStatePatchedPayload(patch={
            "set_phase": "planning",
            "set_next_action": "write the failing test",
            "add_todos": [
                {"id": "1", "status": "pending", "content": "fix the login bug"},
                {"id": "2", "status": "done", "content": "add logging"},
            ],
            "add_decisions": [{"text": "use the adapter approach"}],
        }),
    )
    _suspend_next_goal(log, "t1")
    _close(log, cs, disp)
    return "t1"


def test_inspect_plan_todo_fold(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    task_id = _seed_with_plan(db)
    detail = _detail(db, task_id)
    assert detail is not None
    assert detail.phase == "planning"
    assert detail.next_action == "write the failing test"
    assert len(detail.todos) == 2
    assert len(detail.decisions) == 1
    assert detail.todos[0]["content"] == "fix the login bug"
    assert detail.todos[0]["status"] == "pending"
    assert detail.decisions[0]["text"] == "use the adapter approach"


def test_inspect_no_plan_folds_to_defaults(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    log, cs, disp = _seed(db)
    _emit_created_started(log, "t1")
    _suspend_next_goal(log, "t1")
    _close(log, cs, disp)
    detail = _detail(db, "t1")
    assert detail is not None
    assert detail.phase is None and detail.next_action is None
    assert detail.todos == () and detail.decisions == ()


def test_inspect_plan_non_native_value_plainified(tmp_path: Path) -> None:
    """W5: a todo carrying a non-JSON-native value is plainified, so a
    downstream json.dumps never crashes."""
    db = tmp_path / "s.db"
    log, cs, disp = _seed(db)
    _emit_created_started(log, "t1")
    # a set is not JSON-native; fold stores it verbatim in the patch path.
    log.emit(
        task_id="t1", type="TaskStatePatched",
        payload=TaskStatePatchedPayload(patch={
            "add_todos": [{"id": "1", "weird": {1, 2, 3}}],
        }),
    )
    _close(log, cs, disp)
    detail = _detail(db, "t1")
    assert detail is not None
    assert len(detail.todos) == 1
    assert isinstance(detail.todos[0]["weird"], str)  # plainified
    # the plainified detail round-trips through json without raising.
    json.dumps([dict(t) for t in detail.todos])
