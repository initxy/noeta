"""CW18b — durable `todo_write` control tool.

`todo_write` is a model-visible CONTROL tool (gated, not in engine._tools): a
call replace-alls TaskState.todos via a TodoWriteDecision → TaskStatePatched,
and the loop CONTINUES (ack tool_result, no suspend/terminal).

Gates: set_todos protocol (replace-all + old-recording byte-safe) / schema
gating / durable patch (no ToolCallStarted/ToolResultRecorded; ack present) /
event order / malformed → zero state write (not terminal) / replace semantics /
CW18a inspect integration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._read_models.detail import build_code_session_detail
from noeta.core.fold import fold
from noeta.policies.react import (
    SPAWN_SUBAGENT_TOOL,
    TODO_WRITE_TOOL,
)
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.events import TaskCreatedPayload
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.protocols.task import TaskState
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


_T1 = [{"id": "1", "content": "fix the login bug", "status": "pending"}]
_T2 = [
    {"id": "1", "content": "fix the login bug", "status": "completed"},
    {"id": "2", "content": "add a regression test", "status": "in_progress"},
]


def _todo_call(todos: Any, call_id: str = "tw") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=TODO_WRITE_TOOL,
                              arguments={"todos": todos})],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _mixed_spawn_todo_call() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="s1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": "main", "goal": "child"},
            ),
            ToolUseBlock(
                call_id="tw",
                tool_name=TODO_WRITE_TOOL,
                arguments={"todos": _T1},
            ),
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "mixed"},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1), raw={"id": "end"},
    )


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "x.py").write_text("foo\n")
    return ws


def _session(
    ws: Path,
    responses: list[LLMResponse],
    *,
    todo_write_enabled: bool = True,
    delegate_to: tuple[str, ...] = (),
):
    """A one-shot SDK host that may enable ``todo_write`` and/or delegation.

    ``todo_write_enabled`` maps onto ``capabilities.todo_write``; a non-empty
    ``delegate_to`` maps onto ``delegation=True`` + ``spawnable=("main",)`` (the
    SDK host reads delegation rights off the spec). Returns ``(host, driver,
    provider)`` — the shared ``FakeLLMProvider`` carries ``received_requests``."""
    provider = FakeLLMProvider(responses=responses)
    caps: dict[str, Any] = dict(todo_write=todo_write_enabled)
    if delegate_to:
        caps.update(delegation=True, spawnable=("main",))
    host = make_host(
        make_registry(runner_main_spec("main", **caps)),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    return host, make_driver(host), provider


def _types(host, task_id: str) -> list[str]:
    return [e.type for e in host.event_log.read(task_id)]


def _folded_todos(host, task_id: str) -> list[dict[str, Any]]:
    task = fold(host.event_log, host.content_store, task_id)
    return task.state.todos


# ---------------------------------------------------------------------------
# Protocol — set_todos replace-all + byte-safe
# ---------------------------------------------------------------------------


def test_set_todos_replace_all_apply() -> None:
    state = TaskState()
    state.todos.extend([{"id": "old", "content": "x", "status": "pending"}])
    TaskStatePatch(set_todos=_T1).apply(state)
    assert state.todos == _T1  # replaced, not appended
    TaskStatePatch(set_todos=[]).apply(state)
    assert state.todos == []  # [] clears


def test_set_todos_none_is_no_change() -> None:
    state = TaskState()
    state.todos.extend(_T1)
    TaskStatePatch(set_todos=None).apply(state)  # None = leave unchanged
    assert state.todos == _T1


def test_set_todos_old_recording_byte_safe() -> None:
    # A pre-CW18b payload dict has no `set_todos` key → from_dict → None.
    patch = TaskStatePatch.from_dict({"set_phase": "planning"})
    assert patch.set_todos is None
    # round-trip
    rt = TaskStatePatch.from_dict(TaskStatePatch(set_todos=_T1).to_dict())
    assert rt.set_todos == _T1


def test_to_dict_omits_set_todos_when_none_byte_safe() -> None:
    """P1: a non-todo_write patch must NOT carry `set_todos` in its payload —
    otherwise old recordings (no such key) would no longer fold/resume."""
    # Non-todo patches: key absent → byte-identical to pre-CW18b.
    assert "set_todos" not in TaskStatePatch(activate_skills=["s"]).to_dict()
    assert "set_todos" not in TaskStatePatch(set_phase="plan").to_dict()
    assert "set_todos" not in TaskStatePatch().to_dict()
    # todo_write patches keep it — including `[]` (clear) and a list.
    assert TaskStatePatch(set_todos=[]).to_dict()["set_todos"] == []
    assert TaskStatePatch(set_todos=_T1).to_dict()["set_todos"] == _T1
    # round-trips both ways
    assert TaskStatePatch.from_dict(
        TaskStatePatch(set_todos=[]).to_dict()
    ).set_todos == []


# ---------------------------------------------------------------------------
# Schema gating
# ---------------------------------------------------------------------------


def test_todo_write_schema_gating(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, provider = _session(ws, [_todo_call(_T1), _end()], todo_write_enabled=True)
    driver.start(goal="do the work", agent="main")
    tools = [t["function"]["name"] for t in provider.received_requests[0].tools]
    assert TODO_WRITE_TOOL in tools  # visible when enabled
    # control surface — never an executable Engine tool
    engine = host.resolve_engine_for_agent("main", model="gpt-test")
    assert TODO_WRITE_TOOL not in engine._tools  # type: ignore[union-attr]


def test_todo_write_disabled_absent_from_schema(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, provider = _session(ws, [_end()], todo_write_enabled=False)
    driver.start(goal="do the work", agent="main")
    tools = [t["function"]["name"] for t in provider.received_requests[0].tools]
    assert TODO_WRITE_TOOL not in tools


# ---------------------------------------------------------------------------
# Durable patch + event order + no tool execution
# ---------------------------------------------------------------------------


def test_todo_write_durable_patch_no_tool_events(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, _provider = _session(ws, [_todo_call(_T2), _end()])
    out = driver.start(goal="do the work", agent="main")
    types = _types(host, out.task_id)
    assert "TaskStatePatched" in types
    # control tool — NOT executed via ToolRuntime
    assert "ToolCallStarted" not in types
    assert "ToolResultRecorded" not in types
    # durable replace-all folded into TaskState.todos
    assert _folded_todos(host, out.task_id) == _T2
    # a tool-role ack keeps the conversation well-formed
    task = fold(host.event_log, host.content_store, out.task_id)
    assert any(m.role == "tool" for m in task.runtime.messages)
    # loop continued to a normal finish (one-shot → terminal completed)
    assert "TaskCompleted" in types


def test_todo_write_event_order(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, _provider = _session(ws, [_todo_call(_T1), _end()])
    out = driver.start(goal="do the work", agent="main")
    types = _types(host, out.task_id)
    i_patch = types.index("TaskStatePatched")
    # assistant MessagesAppended BEFORE the patch; ack MessagesAppended AFTER;
    # a recompose (ContextPlanComposed) follows the ack.
    assert "MessagesAppended" in types[:i_patch]
    after = types[i_patch + 1:]
    assert "MessagesAppended" in after
    i_ack = i_patch + 1 + after.index("MessagesAppended")
    assert "ContextPlanComposed" in types[i_ack + 1:]


def test_todo_write_replace_semantics(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, _provider = _session(ws, [_todo_call(_T1, "a"), _todo_call(_T2, "b"), _end()])
    out = driver.start(goal="do the work", agent="main")
    assert _folded_todos(host, out.task_id) == _T2  # second call replaced the first
    assert _types(host, out.task_id).count("TaskStatePatched") == 2


# ---------------------------------------------------------------------------
# Malformed → zero state write, recoverable (not terminal-by-fail)
# ---------------------------------------------------------------------------


def test_todo_write_malformed_no_state_write(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    # missing status → malformed
    bad = [{"id": "1", "content": "x"}]
    host, driver, _provider = _session(ws, [_todo_call(bad), _end()])
    out = driver.start(goal="do the work", agent="main")
    types = _types(host, out.task_id)
    assert "TaskStatePatched" not in types  # zero state write
    assert _folded_todos(host, out.task_id) == []
    assert "TaskFailed" not in types  # recoverable, not terminated-by-fail
    assert "TaskCompleted" in types  # loop continued → normal finish
    task = fold(host.event_log, host.content_store, out.task_id)
    # an error ack was appended so the model could retry
    assert any(m.role == "tool" for m in task.runtime.messages)


def test_todo_write_mixed_with_spawn_is_recoverable(
    tmp_path: Path,
) -> None:
    ws = _make_ws(tmp_path)
    host, driver, _provider = _session(
        ws,
        [_mixed_spawn_todo_call(), _end()],
        todo_write_enabled=True,
        delegate_to=("default",),
    )
    out = driver.start(goal="do the work", agent="main")
    types = _types(host, out.task_id)
    assert "TaskFailed" not in types
    assert "SubtaskSpawned" not in types
    assert "TaskStatePatched" not in types
    assert _folded_todos(host, out.task_id) == []
    task = fold(host.event_log, host.content_store, out.task_id)
    tool_messages = [m for m in task.runtime.messages if m.role == "tool"]
    assert tool_messages
    assert len(tool_messages[-1].content) == 2
    assert "TaskCompleted" in types


def test_todo_write_duplicate_ids_rejected(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    dup = [
        {"id": "1", "content": "a", "status": "pending"},
        {"id": "1", "content": "b", "status": "pending"},
    ]
    host, driver, _provider = _session(ws, [_todo_call(dup), _end()])
    out = driver.start(goal="do the work", agent="main")
    assert "TaskStatePatched" not in _types(host, out.task_id)
    assert _folded_todos(host, out.task_id) == []


def test_todo_write_schema_inherited_by_delegation_child(
    tmp_path: Path,
) -> None:
    ws = _make_ws(tmp_path)
    host, _driver, _provider = _session(
        ws,
        [_end()],
        todo_write_enabled=True,
        delegate_to=("default",),
    )
    # A delegated child engine inherits the todo_write + spawn_subagent control
    # schema. Hand-emit its ``TaskCreated`` and resolve its own engine the way
    # the SDK host's subtask drain does (fold → resolve_engine).
    host.event_log.emit(
        task_id="child",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="child goal", policy_name="react", agent_name="default"
        ),
    )
    child_task = fold(host.event_log, host.content_store, "child")
    child_engine = host.resolve_engine(child_task)
    schema = json.dumps(
        getattr(child_engine._composer, "_control_action_schemas")  # noqa: SLF001
    )
    assert TODO_WRITE_TOOL in schema
    assert SPAWN_SUBAGENT_TOOL in schema


# ---------------------------------------------------------------------------
# CW18a integration — inspect read-model sees the todos
# ---------------------------------------------------------------------------


def test_todo_write_visible_in_session_detail(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, _provider = _session(ws, [_todo_call(_T2), _end()])
    out = driver.start(goal="do the work", agent="main")
    detail = build_code_session_detail(
        host.event_log, host.content_store, out.task_id
    )
    assert detail is not None
    assert [dict(t) for t in detail.todos] == _T2


