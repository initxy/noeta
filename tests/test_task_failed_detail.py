"""``TaskFailed`` carries the failure's diagnostic text, not just its tag.

A provider error used to reach the ledger as ``TaskFailed(reason="llm_error")``
and nothing else: the gateway's own message ("image input is not supported by
…") was durable only inside the ContentStore body behind
``LLMResponseRecorded``, so no host could show it. ``FailDecision.detail`` is
copied onto ``TaskFailedPayload.detail``; a multi-turn conversation, which parks
a failed turn instead of terminating, appends it to ``TaskSuspended.reason``.

The field is additive: a failure with no detail encodes byte-identically to a
recording made before the field existed, and such a recording still restores
and folds.
"""

from __future__ import annotations

import json
from pathlib import Path

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.execution.multi_turn import MultiTurnReActPolicy
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.decisions import FailDecision, YieldForHumanDecision
from noeta.protocols.events import (
    FAIL_DETAIL_MAX_CHARS,
    SUSPEND_REASON_TURN_FAILED,
    TaskCreatedPayload,
    TaskFailedPayload,
    parse_suspend_reason,
)
from noeta.protocols.task import Task
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.storage.spi import restore_payload
from noeta.testing.composer import trivial_three_segment


GATEWAY_TEXT = (
    "HTTP 400 from gateway: {'error': 'image input is not supported by "
    "model_hub/es1_orange_o48'}"
)

#: The exact bytes a ``TaskFailed`` payload had before ``detail`` existed.
_PRE_DETAIL_BYTES = b'{"reason":"llm_error","retryable":false}'


# --- byte safety ---------------------------------------------------------------


def test_detail_less_failure_encodes_to_the_pre_detail_bytes() -> None:
    assert to_canonical_bytes(TaskFailedPayload(reason="llm_error")) == _PRE_DETAIL_BYTES
    assert (
        to_canonical_bytes(TaskFailedPayload(reason="llm_error", detail=None))
        == _PRE_DETAIL_BYTES
    )


def test_detail_enters_the_bytes_only_when_set() -> None:
    body = to_canonical_bytes(TaskFailedPayload(reason="llm_error", detail=GATEWAY_TEXT))
    assert json.loads(body) == {
        "reason": "llm_error",
        "retryable": False,
        "detail": GATEWAY_TEXT,
    }
    restored = restore_payload("TaskFailed", from_canonical_bytes(body))
    assert restored == TaskFailedPayload(reason="llm_error", detail=GATEWAY_TEXT)


def test_pre_detail_recording_restores_and_folds() -> None:
    restored = restore_payload("TaskFailed", json.loads(_PRE_DETAIL_BYTES))
    assert restored == TaskFailedPayload(reason="llm_error", retryable=False)
    assert restored.detail is None

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(task_id="t1", type="TaskFailed", payload=restored)
    assert fold(log, cs, "t1").status == "terminal"


def test_restore_tolerates_a_key_this_reader_does_not_know() -> None:
    body = {"reason": "llm_error", "retryable": False, "detail": "x", "future": 1}
    assert restore_payload("TaskFailed", body) == TaskFailedPayload(
        reason="llm_error", detail="x"
    )


# --- the Engine copies detail from the decision ------------------------------


def _run_one_fail(decision: FailDecision) -> TaskFailedPayload:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=StubScriptedPolicy([decision]),
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    engine.run_one_step(Task(task_id=task.task_id, status="pending"), lease_id=lease.lease_id)
    failed = [e for e in event_log.read(task.task_id) if e.type == "TaskFailed"]
    assert len(failed) == 1
    payload = failed[0].payload
    assert isinstance(payload, TaskFailedPayload)
    return payload


def test_engine_copies_detail_onto_task_failed() -> None:
    payload = _run_one_fail(FailDecision(reason="llm_error", detail=GATEWAY_TEXT))
    assert payload == TaskFailedPayload(
        reason="llm_error", retryable=False, detail=GATEWAY_TEXT
    )


def test_engine_records_no_detail_for_none_or_empty() -> None:
    assert _run_one_fail(FailDecision(reason="boom")).detail is None
    assert _run_one_fail(FailDecision(reason="boom", detail="")).detail is None


def test_engine_caps_an_oversized_detail() -> None:
    payload = _run_one_fail(FailDecision(reason="llm_error", detail="e" * 50_000))
    assert payload.detail is not None
    assert len(payload.detail) == FAIL_DETAIL_MAX_CHARS
    assert payload.detail.endswith("…")


# --- multi-turn: the parked turn keeps the detail ----------------------------


class _Failing:
    def __init__(self, decision: FailDecision) -> None:
        self._decision = decision

    def decide(self, ctx: object, view: object) -> FailDecision:  # noqa: ARG002
        return self._decision


def test_parked_failed_turn_appends_detail_to_the_suspend_reason() -> None:
    wrapped = MultiTurnReActPolicy(
        _Failing(FailDecision(reason="llm_error", detail=GATEWAY_TEXT)), final=False
    )
    decision = wrapped.decide(None, None)  # type: ignore[arg-type]
    assert isinstance(decision, YieldForHumanDecision)
    assert decision.suspend_reason == f"turn_failed: llm_error: {GATEWAY_TEXT}"
    parsed = parse_suspend_reason(decision.suspend_reason)
    assert parsed.kind == SUSPEND_REASON_TURN_FAILED
    assert parsed.detail == f"llm_error: {GATEWAY_TEXT}"


def test_parked_failed_turn_without_detail_is_unchanged() -> None:
    wrapped = MultiTurnReActPolicy(_Failing(FailDecision(reason="llm_error")), final=False)
    decision = wrapped.decide(None, None)  # type: ignore[arg-type]
    assert isinstance(decision, YieldForHumanDecision)
    assert decision.suspend_reason == "turn_failed: llm_error"


# --- end to end: a fatal provider error reaches the ledger -------------------


def test_fatal_provider_error_text_lands_on_task_failed(tmp_path: Path) -> None:
    """The reported repro: before, the payload was
    ``TaskFailedPayload(reason='llm_error', retryable=False)`` and the gateway
    text was reachable nowhere on the caller path."""
    from noeta.client import QueryFailedError, query
    from noeta.protocols.messages import LLMResponse, Usage
    from noeta.sdk import Options
    from noeta.testing.fake_llm import FakeLLMProvider

    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="error",
                content=[],
                usage=Usage(uncached=1, output=0),
                raw={"category": "fatal", "error": GATEWAY_TEXT, "retry_after": None},
            )
        ]
    )
    result = query(
        Options(
            system_prompt="Return the requested text exactly.",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        goal="read the chess board image",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )
    try:
        result.answer()
    except QueryFailedError:
        pass
    failed = [env.payload for env in result if isinstance(env.payload, TaskFailedPayload)]
    assert len(failed) == 1
    assert failed[0].reason == "llm_error"
    assert failed[0].detail is not None
    assert GATEWAY_TEXT in failed[0].detail


# --- a failed child tells its parent why --------------------------------------


def _parent_sees(child_decision: FailDecision) -> object:
    from noeta.protocols.wake import SubtaskResult
    from tests.test_engine_child_completion import (
        _build_engine_with_child,
        _find_child_id,
    )

    engine, log, _disp, lease_id = _build_engine_with_child(child_decision=child_decision)
    child_id = _find_child_id(log, parent_id="task-parent")
    engine.run_one_step(
        Task(task_id=child_id, status="pending", parent_task_id="task-parent"),
        lease_id=lease_id,
    )
    (completed,) = [e for e in log.read("task-parent") if e.type == "SubtaskCompleted"]
    result = completed.payload.result
    assert isinstance(result, SubtaskResult)
    assert result.status == "failed"
    return result.error


def test_failed_child_detail_reaches_the_parent_subtask_result() -> None:
    error = _parent_sees(FailDecision(reason="llm_error", detail=GATEWAY_TEXT))
    assert error == f"llm_error: {GATEWAY_TEXT}"


def test_failed_child_without_detail_keeps_the_bare_reason() -> None:
    assert _parent_sees(FailDecision(reason="boom")) == "boom"
