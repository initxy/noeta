"""CW18d — durable `ask_user_question` typed HITL control tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._read_models.detail import build_code_session_detail
from noeta.core.fold import fold
from noeta.core.snapshot import rehydrate_task, serialize_task_state
from noeta.policies.react import ASK_USER_QUESTION_TOOL, SPAWN_SUBAGENT_TOOL
from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.events import (
    EventEnvelope,
    TaskCreatedPayload,
    UserQuestionAnsweredPayload,
    UserQuestionRequestedPayload,
)
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.policies.control_tools import load_questions_body
from noeta.protocols.task import GovernanceState, Task
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES, ContentRef
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


_QUESTIONS = [
    {
        "id": "target",
        "question": "Which deploy target should I use?",
        "header": "Target",
        "choices": [
            {
                "id": "staging",
                "label": "Staging",
                "description": "Use the staging deployment.",
            },
            {
                "id": "prod",
                "label": "Production",
                "description": "Use the production deployment.",
            },
        ],
        "allow_freeform": False,
    }
]


def _ask(
    *,
    call_id: str = "q1",
    questions: Any = _QUESTIONS,
    reason: Any = "need target",
) -> LLMResponse:
    args: dict[str, Any] = {"questions": questions}
    if reason is not None:
        args["reason"] = reason
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name=ASK_USER_QUESTION_TOOL,
                arguments=args,
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _mixed_ask_spawn() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="q1",
                tool_name=ASK_USER_QUESTION_TOOL,
                arguments={"questions": _QUESTIONS},
            ),
            ToolUseBlock(
                call_id="s1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": "main", "goal": "child"},
            ),
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "mixed"},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _answer_text(text: str = "staging") -> dict[str, dict[str, str]]:
    return {"target": {"text": text}}


def _answer_choice(choice: str = "staging") -> dict[str, dict[str, str]]:
    return {"target": {"choice_id": choice}}


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("hello\n", encoding="utf-8")
    return ws


def _session(
    ws: Path,
    responses: list[LLMResponse],
    *,
    ask_user_question_enabled: bool = True,
    delegate_to: tuple[str, ...] = (),
):
    """A one-shot SDK host that may enable ``ask_user_question`` and/or delegation.

    ``ask_user_question_enabled`` maps onto ``capabilities.ask_user_question``; a
    non-empty ``delegate_to`` maps onto ``delegation=True`` + ``spawnable=("main",)``.
    Returns ``(host, driver, provider)`` — the shared ``FakeLLMProvider`` carries
    ``received_requests`` for the white-box schema / resume assertions."""
    provider = FakeLLMProvider(responses=responses)
    caps: dict[str, Any] = dict(ask_user_question=ask_user_question_enabled)
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


def _tool_names(provider) -> list[str]:
    return [t["function"]["name"] for t in provider.received_requests[0].tools]


def test_ask_user_question_schema_gating_and_not_engine_tool(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, provider = _session(ws, [_end()], ask_user_question_enabled=True)
    driver.start(goal="do the work", agent="main")
    assert ASK_USER_QUESTION_TOOL in _tool_names(provider)
    engine = host.resolve_engine_for_agent("main", model="gpt-test")
    assert ASK_USER_QUESTION_TOOL not in engine._tools  # type: ignore[union-attr]  # noqa: SLF001

    host, driver, provider = _session(ws, [_end()], ask_user_question_enabled=False)
    driver.start(goal="do the work", agent="main")
    assert ASK_USER_QUESTION_TOOL not in _tool_names(provider)


def test_valid_ask_records_ref_payload_and_suspends(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, _provider = _session(ws, [_ask()])
    out = driver.start(goal="do the work", agent="main")
    assert out.status == "suspended"
    assert out.wake_handle == "question-q1"
    types = _types(host, out.task_id)
    i_req = types.index("UserQuestionRequested")
    assert "MessagesAppended" in types[:i_req]
    assert types[i_req + 1] == "TaskSnapshot"
    assert types[i_req + 2] == "TaskSuspended"
    assert "ToolCallStarted" not in types
    assert "ToolResultRecorded" not in types

    request = next(
        e.payload for e in host.event_log.read(out.task_id)
        if e.type == "UserQuestionRequested"
    )
    assert isinstance(request, UserQuestionRequestedPayload)
    assert request.question_id == "q1"
    assert request.call_id == "q1"
    assert request.question_count == 1
    assert to_canonical_bytes(request)
    assert len(to_canonical_bytes(request)) < EVENT_PAYLOAD_MAX_BYTES
    assert load_questions_body(host.content_store, request.questions_ref) == _QUESTIONS

    folded = fold(host.event_log, host.content_store, out.task_id)
    assert sorted(folded.governance.pending_questions) == ["q1"]


def test_max_size_question_stays_out_of_event_payload(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    long_questions = [
        {
            "id": f"q{i}",
            "question": "Q" * 500,
            "header": "H" * 40,
            "choices": [
                {
                    "id": f"c{i}_{j}",
                    "label": "L" * 80,
                    "description": "D" * 300,
                }
                for j in range(5)
            ],
            "allow_freeform": False,
        }
        for i in range(3)
    ]
    host, driver, _provider = _session(ws, [_ask(questions=long_questions, reason="R" * 500)])
    out = driver.start(goal="do the work", agent="main")
    request = next(
        e.payload for e in host.event_log.read(out.task_id)
        if e.type == "UserQuestionRequested"
    )
    assert isinstance(request, UserQuestionRequestedPayload)
    assert len(to_canonical_bytes(request)) < EVENT_PAYLOAD_MAX_BYTES
    body = host.content_store.get(request.questions_ref)
    assert len(body) > EVENT_PAYLOAD_MAX_BYTES


def test_bad_call_id_and_mixed_ask_are_recoverable_without_suspend_or_spawn(
    tmp_path: Path,
) -> None:
    ws = _make_ws(tmp_path)
    host, driver, _provider = _session(ws, [_ask(call_id="bad id with space"), _end()])
    out = driver.start(goal="do the work", agent="main")
    types = _types(host, out.task_id)
    assert out.status == "terminal"
    assert "UserQuestionRequested" not in types
    assert "TaskSuspended" not in types
    task = fold(host.event_log, host.content_store, out.task_id)
    tool_msgs = [m for m in task.runtime.messages if m.role == "tool"]
    assert tool_msgs
    block = tool_msgs[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert "call_id must match" in str(block.error)

    host, driver, _provider = _session(
        ws,
        [_mixed_ask_spawn(), _end()],
        ask_user_question_enabled=True,
        delegate_to=("default",),
    )
    out = driver.start(goal="do the work", agent="main")
    types = _types(host, out.task_id)
    assert out.status == "terminal"
    assert "UserQuestionRequested" not in types
    assert "SubtaskSpawned" not in types
    assert "TaskFailed" not in types


def test_unanswerable_question_is_recoverable_without_pending_or_suspend(
    tmp_path: Path,
) -> None:
    ws = _make_ws(tmp_path)
    unanswerable = [
        {
            "id": "q1",
            "question": "Need a valid answer control?",
            "allow_freeform": False,
        }
    ]
    host, driver, _provider = _session(ws, [_ask(questions=unanswerable), _end()])
    out = driver.start(goal="do the work", agent="main")
    types = _types(host, out.task_id)
    assert out.status == "terminal"
    assert "UserQuestionRequested" not in types
    assert "TaskSuspended" not in types
    task = fold(host.event_log, host.content_store, out.task_id)
    assert task.governance.pending_questions == {}
    tool_msgs = [m for m in task.runtime.messages if m.role == "tool"]
    assert tool_msgs
    block = tool_msgs[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert "must provide choices or allow freeform" in str(block.error)


def test_answer_records_audit_tool_result_and_continues(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, provider = _session(ws, [_ask(), _end("answered")])
    out = driver.start(goal="do the work", agent="main")
    result = driver.answer(
        out.task_id,
        question_id="q1",
        answers=_answer_choice("staging"),
        answered_by="tester",
    )
    assert result.status == "terminal"
    types = _types(host, out.task_id)
    i_woken = types.index("TaskWoken")
    assert types[i_woken + 1] == "UserQuestionAnswered"
    assert types[i_woken + 2] == "MessagesAppended"
    assert "ContextPlanComposed" in types[i_woken + 3:]

    folded = fold(host.event_log, host.content_store, out.task_id)
    assert folded.governance.pending_questions == {}
    assert folded.governance.question_answers[-1]["question_id"] == "q1"
    tool_messages = [m for m in folded.runtime.messages if m.role == "tool"]
    block = tool_messages[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.call_id == "q1"
    assert block.success is True
    assert block.output == {
        "question_id": "q1",
        "answers": {"target": {"choice_id": "staging", "text": None}},
    }

    resumed_request = provider.received_requests[-1]
    assert any(
        msg.role == "tool"
        and isinstance(msg.content[0], ToolResultBlock)
        and msg.content[0].call_id == "q1"
        for msg in resumed_request.messages
    )


def test_answer_accepts_choice_and_freeform_together(tmp_path: Path) -> None:
    # B17 / U6 — a choice AND a freeform note resume the task end-to-end and the
    # recorded tool result carries BOTH fields (the model decides how to use them).
    ws = _make_ws(tmp_path)
    freeform_q = [{**_QUESTIONS[0], "allow_freeform": True}]
    host, driver, _provider = _session(ws, [_ask(questions=freeform_q), _end("answered")])
    out = driver.start(goal="do the work", agent="main")
    result = driver.answer(
        out.task_id,
        question_id="q1",
        answers={"target": {"choice_id": "staging", "text": "but only the EU region"}},
        answered_by="tester",
    )
    assert result.status == "terminal"
    folded = fold(host.event_log, host.content_store, out.task_id)
    tool_messages = [m for m in folded.runtime.messages if m.role == "tool"]
    block = tool_messages[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.output == {
        "question_id": "q1",
        "answers": {"target": {"choice_id": "staging", "text": "but only the EU region"}},
    }


def test_answer_payload_cap_uses_content_store(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, _provider = _session(
        ws, [_ask(questions=[{**_QUESTIONS[0], "allow_freeform": True}]), _end()]
    )
    out = driver.start(goal="do the work", agent="main")
    driver.answer(
        out.task_id,
        question_id="q1",
        answers=_answer_text("A" * 4000),
        answered_by="tester",
    )
    answer = next(
        e.payload for e in host.event_log.read(out.task_id)
        if e.type == "UserQuestionAnswered"
    )
    assert isinstance(answer, UserQuestionAnsweredPayload)
    assert len(to_canonical_bytes(answer)) < EVENT_PAYLOAD_MAX_BYTES
    assert answer.answers_ref.size > EVENT_PAYLOAD_MAX_BYTES - 200


def test_detail_projection_fail_open_on_missing_question_ref(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, _provider = _session(ws, [_ask()])
    out = driver.start(goal="do the work", agent="main")
    events = _events_with_missing_question_ref(host, out.task_id, "q1")
    detail = build_code_session_detail(
        _StaticEventLog(events), host.content_store, out.task_id
    )
    assert detail is not None
    item = detail.pending_questions[0]
    assert item["questions"] == []
    assert "decode_error" in item


def test_old_snapshot_governance_defaults_rehydrate() -> None:
    task = Task(task_id="t1", status="suspended")
    state = from_canonical_bytes(serialize_task_state(task))
    assert isinstance(state, dict)
    governance = state["governance"]
    assert isinstance(governance, dict)
    governance.pop("pending_questions", None)
    governance.pop("question_answers", None)
    restored = rehydrate_task(state)
    assert isinstance(restored.governance, GovernanceState)
    assert restored.governance.pending_questions == {}
    assert restored.governance.question_answers == []


def test_child_engine_does_not_expose_ask_schema(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, _driver, _provider = _session(
        ws,
        [_end()],
        ask_user_question_enabled=True,
        delegate_to=("default",),
    )
    # The root (depth-0) engine exposes ask_user_question...
    parent_engine = host.resolve_engine_for_agent("main", model="gpt-test")
    assert ASK_USER_QUESTION_TOOL in json.dumps(
        parent_engine._composer._control_action_schemas  # type: ignore[union-attr]  # noqa: SLF001
    )
    # ...but a delegated child (parent_task_id set, depth>0) has it masked off,
    # while still inheriting spawn_subagent. Hand-emit the child's ``TaskCreated``
    # and resolve its own engine the way the SDK host's subtask drain does
    # (fold → resolve_engine, which depth-masks ask_user_question).
    host.event_log.emit(
        task_id="child",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="child",
            policy_name="react",
            agent_name="default",
            parent_task_id="root",
            subtask_depth=1,
        ),
    )
    child_task = fold(host.event_log, host.content_store, "child")
    child_engine = host.resolve_engine(child_task)
    schema = json.dumps(
        getattr(child_engine._composer, "_control_action_schemas")  # noqa: SLF001
    )
    assert ASK_USER_QUESTION_TOOL not in schema
    assert SPAWN_SUBAGENT_TOOL in schema


def _events_with_missing_question_ref(
    host, task_id: str, question_id: str
) -> list[EventEnvelope]:
    missing = ContentRef(hash="0" * 64, size=10, media_type="application/json")
    out: list[EventEnvelope] = []
    for env in host.event_log.read(task_id):
        if env.type == "TaskSnapshot":
            continue
        if env.type == "UserQuestionRequested" and env.payload.question_id == question_id:
            out.append(
                EventEnvelope.build(
                    task_id=env.task_id,
                    type=env.type,
                    payload=UserQuestionRequestedPayload(
                        question_id=env.payload.question_id,
                        call_id=env.payload.call_id,
                        questions_ref=missing,
                        question_count=env.payload.question_count,
                        reason=env.payload.reason,
                    ),
                    id=env.id,
                    actor=env.actor,
                    trace_id=env.trace_id,
                    causation_id=env.causation_id,
                    schema_version=env.schema_version,
                    occurred_at=env.occurred_at,
                    origin=env.origin,
                ).with_seq(env.seq)
            )
            continue
        out.append(env)
    return out


class _StaticEventLog:
    def __init__(self, events: list[EventEnvelope]) -> None:
        self._events = events

    def read(
        self, task_id: str, *, after_seq: int | None = None
    ) -> list[EventEnvelope]:
        events = [env for env in self._events if env.task_id == task_id]
        if after_seq is None:
            return events
        return [env for env in events if env.seq > after_seq]

    def find_latest_snapshot(self, task_id: str) -> EventEnvelope | None:
        for env in reversed(self.read(task_id)):
            if env.type == "TaskSnapshot":
                return env
        return None
