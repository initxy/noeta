"""``TodoWrite`` batched with ``Task`` in one response.

The checklist is saved AND the spawn happens in the same turn. Every tool_use
gets exactly one tool_result, and the turn gets ONE tool-role message: the
TodoWrite ack joins the background "started" result, or — for a foreground
single spawn / fan-out — it is held on the ``SubtaskSpawned`` record and
lands in the child-result message at resume. The durable resume (a fresh
engine folding the parent from the log) renders the same message. A second
TodoWrite, or TodoWrite beside ``AskUserQuestion``, stays refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from noeta.builtins.todo_write.impl import TODO_WRITE_ACK, TODO_WRITE_TOOL
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import (
    FinishDecision,
    SpawnSubtaskDecision,
    TaskStatePatch,
)
from noeta.protocols.events import SubtaskSpawnedPayload
from noeta.protocols.hooks import ProposedSpawnSubtask, VerdictResult
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.wake import SubtaskCompleted
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.worker import _render_subtask_wake
from noeta.runtime.workspace import FsWriteMode
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.testing.fake_llm import FakeLLMProvider

from tests._sdk_session import (
    default_coding_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)


_TODOS = [
    {"content": "delegate the research", "status": "in_progress",
     "activeForm": "delegating the research"},
    {"content": "summarize", "status": "pending", "activeForm": "summarizing"},
]
_STARTED_MARKER = "runs concurrently while you keep working"


# ---------------------------------------------------------------------------
# scripted responses + fixtures
# ---------------------------------------------------------------------------


def _todo(call_id: str = "tw") -> ToolUseBlock:
    return ToolUseBlock(
        call_id=call_id, tool_name=TODO_WRITE_TOOL, arguments={"todos": _TODOS}
    )


def _task(call_id: str, goal: str, *, background: bool = False) -> ToolUseBlock:
    args: dict[str, Any] = {
        "description": "research",
        "prompt": goal,
        "subagent_type": "explore",
    }
    if background:
        args["background"] = True
    return ToolUseBlock(
        call_id=call_id, tool_name=SPAWN_SUBAGENT_TOOL, arguments=args
    )


def _response(*blocks: Any) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=list(blocks),
        usage=Usage(uncached=1, output=1),
        raw={"id": "turn"},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _host(
    tmp_path: Path,
    provider: FakeLLMProvider,
    *,
    multi_turn: bool = False,
    extra_guards: tuple[Any, ...] = (),
    **caps: Any,
):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    main = runner_main_spec(
        "main",
        delegation=True,
        spawnable=("explore",),
        todo_write=True,
        **caps,
    )
    host = make_host(
        make_registry(main, preset_spec("explore")),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        # A background launch is wired for interactive sessions only.
        multi_turn=multi_turn,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        budget=default_coding_budget(),
        extra_guards=extra_guards,
    )
    driver = make_driver(host)
    host.set_background_notifier(driver)
    return host, driver


class _RecordingLauncher:
    """Records background launches and never drives the child."""

    def __init__(self) -> None:
        self.launched: list[tuple[str, str]] = []

    def capacity(self, parent_task_id: str) -> Optional[str]:
        return None

    def launch(self, *, parent_task_id: str, child_task_id: str) -> None:
        self.launched.append((parent_task_id, child_task_id))


def _types(host: Any, task_id: str) -> list[str]:
    return [e.type for e in host.event_log.read(task_id)]


def _tool_messages_after_first_assistant(messages: list[Message]) -> list[Message]:
    """The tool-role messages between the first assistant turn and the next."""
    out: list[Message] = []
    seen_assistant = False
    for m in messages:
        if m.role == "assistant":
            if seen_assistant:
                break
            seen_assistant = True
            continue
        if seen_assistant and m.role == "tool":
            out.append(m)
    return out


def _answered_call_ids(messages: list[Message]) -> list[str]:
    return [
        b.call_id
        for m in messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]


def _tool_use_call_ids(messages: list[Message]) -> list[str]:
    return [
        b.call_id
        for m in messages
        if m.role == "assistant"
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]


def _assert_each_call_answered_once(messages: list[Message]) -> None:
    answered = _answered_call_ids(messages)
    assert sorted(answered) == sorted(_tool_use_call_ids(messages))
    assert len(answered) == len(set(answered))


# ---------------------------------------------------------------------------
# background: the ack joins the "started" result
# ---------------------------------------------------------------------------


def test_todo_write_with_background_task_saves_and_starts(tmp_path: Path) -> None:
    provider = FakeLLMProvider(
        responses=[
            _response(_todo(), _task("bg", "research omega", background=True)),
            _end("started and tracking"),
        ]
    )
    host, driver = _host(tmp_path, provider, multi_turn=True)
    stub = _RecordingLauncher()
    object.__setattr__(host, "_background_subagents", stub)

    out = driver.start(goal="plan and delegate", agent="main")
    parent = fold(host.event_log, host.content_store, out.task_id)
    types = _types(host, out.task_id)

    assert "TaskFailed" not in types
    assert parent.state.todos == _TODOS
    assert "BackgroundSubagentStarted" in types
    assert len(stub.launched) == 1
    # Patch before the launch: the checklist is saved when the spawn runs.
    assert types.index("TaskStatePatched") < types.index(
        "BackgroundSubagentStarted"
    )

    tool_msgs = _tool_messages_after_first_assistant(parent.runtime.messages)
    assert len(tool_msgs) == 1
    blocks = tool_msgs[0].content
    assert [b.call_id for b in blocks] == ["tw", "bg"]
    assert blocks[0].success is True and blocks[0].output == TODO_WRITE_ACK
    assert blocks[1].success is True and _STARTED_MARKER in blocks[1].output
    _assert_each_call_answered_once(parent.runtime.messages)


# ---------------------------------------------------------------------------
# foreground single: the ack is held until the child's result
# ---------------------------------------------------------------------------


def test_todo_write_with_foreground_task_one_result_message(tmp_path: Path) -> None:
    provider = FakeLLMProvider(
        responses=[
            _response(_task("s1", "research omega"), _todo()),
            _end("omega answer"),  # the child
            _end("parent done"),
        ]
    )
    host, driver = _host(tmp_path, provider)

    out = driver.start(goal="plan and delegate", agent="main")
    parent = fold(host.event_log, host.content_store, out.task_id)
    types = _types(host, out.task_id)

    assert "TaskFailed" not in types
    assert parent.state.todos == _TODOS
    # Saved at spawn time: the patch precedes the spawn record.
    assert types.index("TaskStatePatched") < types.index("SubtaskSpawned")
    spawned = [
        e.payload for e in host.event_log.read(out.task_id)
        if e.type == "SubtaskSpawned"
    ]
    assert spawned[0].preacked_ref is not None

    tool_msgs = _tool_messages_after_first_assistant(parent.runtime.messages)
    assert len(tool_msgs) == 1
    blocks = tool_msgs[0].content
    assert [b.call_id for b in blocks] == ["tw", "s1"]
    assert blocks[0].output == TODO_WRITE_ACK and blocks[0].success is True
    assert blocks[1].output == "omega answer" and blocks[1].success is True
    _assert_each_call_answered_once(parent.runtime.messages)

    # The resumed request the provider saw carried the same single message.
    resumed = provider.received_requests[-1]
    wire_tool = [m for m in resumed.messages if m.role == "tool"]
    assert len(wire_tool) == 1
    assert [b.call_id for b in wire_tool[0].content] == ["tw", "s1"]


def test_todo_write_with_fanout_one_result_message(tmp_path: Path) -> None:
    provider = FakeLLMProvider(
        responses=[
            _response(_todo(), _task("a", "alpha"), _task("b", "beta")),
            _end("child answer"),
            _end("child answer"),
            _end("parent done"),
        ]
    )
    host, driver = _host(tmp_path, provider)

    out = driver.start(goal="plan and fan out", agent="main")
    parent = fold(host.event_log, host.content_store, out.task_id)
    types = _types(host, out.task_id)

    assert "TaskFailed" not in types
    assert parent.state.todos == _TODOS
    assert types.count("SubtaskSpawned") == 2
    assert types.index("TaskStatePatched") < types.index("SubtaskSpawned")
    spawned = [
        e.payload for e in host.event_log.read(out.task_id)
        if e.type == "SubtaskSpawned"
    ]
    # Held on the first member only.
    assert spawned[0].preacked_ref is not None
    assert spawned[1].preacked_ref is None

    tool_msgs = _tool_messages_after_first_assistant(parent.runtime.messages)
    assert len(tool_msgs) == 1
    blocks = tool_msgs[0].content
    assert [b.call_id for b in blocks] == ["tw", "a", "b"]
    assert blocks[0].output == TODO_WRITE_ACK
    assert all(b.success for b in blocks)
    _assert_each_call_answered_once(parent.runtime.messages)


def test_todo_write_keeps_its_ack_when_the_spawn_is_denied(
    tmp_path: Path,
) -> None:
    """The Guard refusing the delegation does not un-save the checklist the
    Engine already applied, so TodoWrite keeps its success ack while the Task
    call reads the refusal — still one tool-role message."""
    denied = ToolUseBlock(
        call_id="s1",
        tool_name=SPAWN_SUBAGENT_TOOL,
        arguments={
            "description": "x", "prompt": "do it", "subagent_type": "plan",
        },
    )
    provider = FakeLLMProvider(
        responses=[_response(_todo(), denied), _end("adapting")]
    )
    host, driver = _host(tmp_path, provider)

    out = driver.start(goal="plan and delegate", agent="main")
    parent = fold(host.event_log, host.content_store, out.task_id)
    types = _types(host, out.task_id)

    assert "SubtaskDenied" in types
    assert "SubtaskSpawned" not in types
    assert parent.state.todos == _TODOS
    tool_msgs = _tool_messages_after_first_assistant(parent.runtime.messages)
    assert len(tool_msgs) == 1
    by_id = {b.call_id: b for b in tool_msgs[0].content}
    assert by_id["tw"].success is True and by_id["tw"].output == TODO_WRITE_ACK
    assert by_id["s1"].success is False
    _assert_each_call_answered_once(parent.runtime.messages)


class _RequireApprovalOnSpawn:
    """A host Guard doing review-before-delegate."""

    name = "approve-spawn"
    priority = 10

    def check(self, action: Any, ctx: Any) -> VerdictResult:  # noqa: ARG002
        if isinstance(action, ProposedSpawnSubtask):
            return VerdictResult.require_approval("review before delegate")
        return VerdictResult.allow()


def test_held_spawn_approved_renders_the_ack_with_the_child_result(
    tmp_path: Path,
) -> None:
    provider = FakeLLMProvider(
        responses=[
            _response(_todo(), _task("s1", "research omega")),
            _end("omega answer"),
            _end("parent done"),
        ]
    )
    host, driver = _host(
        tmp_path, provider, extra_guards=(_RequireApprovalOnSpawn(),)
    )
    out = driver.start(goal="plan and delegate", agent="main")
    assert out.wake_handle == f"approval-spawn-{out.task_id}"
    parent = fold(host.event_log, host.content_store, out.task_id)
    assert parent.state.todos == _TODOS
    assert _answered_call_ids(parent.runtime.messages) == []

    out = driver.approve(out.task_id, call_id=f"spawn-{out.task_id}")
    parent = fold(host.event_log, host.content_store, out.task_id)
    tool_msgs = _tool_messages_after_first_assistant(parent.runtime.messages)
    assert len(tool_msgs) == 1
    assert [b.call_id for b in tool_msgs[0].content] == ["tw", "s1"]
    assert tool_msgs[0].content[0].output == TODO_WRITE_ACK
    _assert_each_call_answered_once(parent.runtime.messages)


def test_held_spawn_denied_keeps_the_ack(tmp_path: Path) -> None:
    provider = FakeLLMProvider(
        responses=[
            _response(_todo(), _task("s1", "research omega")),
            _end("adapting"),
        ]
    )
    host, driver = _host(
        tmp_path, provider, extra_guards=(_RequireApprovalOnSpawn(),)
    )
    out = driver.start(goal="plan and delegate", agent="main")
    out = driver.deny(
        out.task_id, call_id=f"spawn-{out.task_id}", reason="not now"
    )
    parent = fold(host.event_log, host.content_store, out.task_id)
    tool_msgs = _tool_messages_after_first_assistant(parent.runtime.messages)
    assert len(tool_msgs) == 1
    by_id = {b.call_id: b for b in tool_msgs[0].content}
    assert by_id["tw"].success is True and by_id["tw"].output == TODO_WRITE_ACK
    assert by_id["s1"].success is False and by_id["s1"].error == "not now"
    _assert_each_call_answered_once(parent.runtime.messages)


# ---------------------------------------------------------------------------
# durable resume: a fresh engine folding from the log renders the same message
# ---------------------------------------------------------------------------


def _engine(log: Any, cs: Any, policy: Any) -> Engine:
    return Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=policy,
    )


def test_resume_from_log_renders_the_held_ack_with_the_child_result() -> None:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    cs = InMemoryContentStore()

    assistant = Message(
        role="assistant",
        content=[
            ToolUseBlock(
                call_id="s1", tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": "child_agent", "goal": "do thing"},
            ),
            ToolUseBlock(
                call_id="tw", tool_name=TODO_WRITE_TOOL,
                arguments={"todos": _TODOS},
            ),
        ],
    )
    ack = ToolResultBlock(call_id="tw", output=TODO_WRITE_ACK, success=True)
    parent_engine = _engine(
        log, cs,
        StubScriptedPolicy([
            SpawnSubtaskDecision(
                agent_name="child_agent",
                goal="do thing",
                assistant_message=assistant,
                state_patch=TaskStatePatch(set_todos=list(_TODOS)),
                preacked_results=(ack,),
            ),
            FinishDecision(answer="parent done"),
        ]),
    )
    child_engine = _engine(log, cs, StubScriptedPolicy([FinishDecision(answer="42")]))

    parent = parent_engine.create_task(goal="parent", policy_name="scripted")
    disp.enqueue(parent.task_id)
    lease = disp.lease(worker_id="w1")
    assert lease is not None
    parent = parent_engine.run_one_step(parent, lease_id=lease.lease_id)
    assert parent.status == "suspended"
    # Saved at spawn time; the TodoWrite call is still unanswered (held).
    assert parent.state.todos == _TODOS
    assert _answered_call_ids(parent.runtime.messages) == []
    disp.release(lease.lease_id, next_state="suspended", wake_on=parent.wake_on)

    c_lease = disp.lease(worker_id="w1")
    assert c_lease is not None
    child = fold(log, cs, c_lease.task_id)
    child = child_engine.run_one_step(child, lease_id=c_lease.lease_id)
    assert child.status == "terminal"
    disp.release(c_lease.lease_id, next_state="terminal")

    # Durable resume: a FRESH engine, the parent folded from the log only.
    p_lease = disp.lease(worker_id="w2")
    assert p_lease is not None and p_lease.task_id == parent.task_id
    fresh = _engine(log, cs, StubScriptedPolicy([FinishDecision(answer="done")]))
    folded = fold(log, cs, parent.task_id)
    folded = fresh.note_woken(
        folded, lease_id=p_lease.lease_id, wake_event=p_lease.wake_event
    )
    assert isinstance(p_lease.wake_event, SubtaskCompleted)
    resumed = _render_subtask_wake(
        fresh, folded, p_lease.wake_event, lease_id=p_lease.lease_id
    )
    tool_msgs = [m for m in resumed.runtime.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert [b.call_id for b in tool_msgs[0].content] == ["tw", "s1"]
    assert tool_msgs[0].content[0] == ack
    assert tool_msgs[0].content[1].output == "42"

    # Idempotent re-entry (crash between render and step) appends nothing.
    again = _render_subtask_wake(
        fresh, resumed, p_lease.wake_event, lease_id=p_lease.lease_id
    )
    assert len([m for m in again.runtime.messages if m.role == "tool"]) == 1

    # Replay: folding the log reproduces exactly the live messages.
    replayed = fold(log, cs, parent.task_id)
    assert replayed.runtime.messages == resumed.runtime.messages
    assert replayed.state.todos == _TODOS


def test_spawn_payload_without_held_results_is_byte_identical() -> None:
    payload = SubtaskSpawnedPayload(subtask_id="c", agent_name="a", goal="g")
    assert b"preacked_ref" not in to_canonical_bytes(payload)


# ---------------------------------------------------------------------------
# still refused
# ---------------------------------------------------------------------------


def _refused(tmp_path: Path, response: LLMResponse) -> tuple[Any, Any]:
    provider = FakeLLMProvider(responses=[response, _end("retrying")])
    host, driver = _host(tmp_path, provider, ask_user_question=True)
    out = driver.start(goal="plan", agent="main")
    return host, fold(host.event_log, host.content_store, out.task_id)


def test_two_todo_writes_with_task_stay_refused(tmp_path: Path) -> None:
    host, parent = _refused(
        tmp_path, _response(_todo("tw1"), _todo("tw2"), _task("s1", "x"))
    )
    types = _types(host, parent.task_id)
    assert "TaskStatePatched" not in types
    assert "SubtaskSpawned" not in types
    assert parent.state.todos == []
    tool_msgs = _tool_messages_after_first_assistant(parent.runtime.messages)
    assert len(tool_msgs) == 1
    assert all(b.success is False for b in tool_msgs[0].content)
    _assert_each_call_answered_once(parent.runtime.messages)


def test_todo_write_with_ask_user_question_stays_refused(tmp_path: Path) -> None:
    ask = ToolUseBlock(
        call_id="q1",
        tool_name="AskUserQuestion",
        arguments={
            "questions": [{
                "question": "Which one?", "header": "Pick",
                "options": [
                    {"label": "A", "description": "a"},
                    {"label": "B", "description": "b"},
                ],
                "multiSelect": False,
            }],
        },
    )
    host, parent = _refused(tmp_path, _response(_todo(), ask))
    types = _types(host, parent.task_id)
    assert "TaskStatePatched" not in types
    assert parent.state.todos == []
    tool_msgs = _tool_messages_after_first_assistant(parent.runtime.messages)
    assert len(tool_msgs) == 1
    assert all(b.success is False for b in tool_msgs[0].content)
    _assert_each_call_answered_once(parent.runtime.messages)


def test_todo_write_with_malformed_task_refuses_everything(tmp_path: Path) -> None:
    """A refused Task refuses the whole response with its own reason: the
    checklist is not saved while the delegation beside it did not run."""
    bad = ToolUseBlock(
        call_id="s1", tool_name=SPAWN_SUBAGENT_TOOL,
        arguments={"description": "x", "prompt": "   ", "subagent_type": "explore"},
    )
    host, parent = _refused(tmp_path, _response(_todo(), bad))
    types = _types(host, parent.task_id)
    assert "TaskStatePatched" not in types
    assert "SubtaskSpawned" not in types
    tool_msgs = _tool_messages_after_first_assistant(parent.runtime.messages)
    assert len(tool_msgs) == 1
    for block in tool_msgs[0].content:
        assert block.success is False
        assert "'prompt' is empty" in (block.error or "")
    _assert_each_call_answered_once(parent.runtime.messages)

