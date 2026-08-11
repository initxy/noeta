"""A multi-turn conversation's finished turn keeps its terminal answer.

``MultiTurnReActPolicy`` rewrites a ``FinishDecision`` into a next-goal suspend
so the conversation stays resumable. That substitution used to be lossy in one
place: ``FinishDecision.answer`` had nowhere to go, because a conversation never
writes ``TaskCompleted`` and ``TaskSuspended`` carried only ``reason`` +
``wake_on``. The cost landed on every host that wanted both "resumable" and "a
structured final answer" — with ``Options.output_schema`` set the kernel had
already deserialized the JSON, and the host had to throw that away and re-parse
the assistant text out of the message projection.

These pin the answer surviving the substitution, the spill for an oversized one,
and the property that makes the field safe to add: a suspend carrying no answer
is byte-equal to one recorded before the field existed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from noeta.core.fold import fold
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import FinishDecision, YieldForHumanDecision
from noeta.protocols.events import (
    TaskSuspendedPayload,
    answer_from_payload,
)
from noeta.protocols.wake import HumanResponseReceived, NEXT_GOAL_WAKE_HANDLE
from noeta.execution.multi_turn import MultiTurnReActPolicy
from noeta.sdk import Client, LLMResponse, Options, TextBlock, Usage
from noeta.sdk.testing import FakeLLMProvider


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["verdict", "score"],
    "additionalProperties": False,
}


# --- the wrapper itself ------------------------------------------------------


class _Finishing:
    def __init__(self, answer: Any) -> None:
        self._answer = answer

    def decide(self, ctx: Any, view: Any) -> FinishDecision:  # noqa: ARG002
        return FinishDecision(answer=self._answer)


def test_wrapper_carries_the_answer_into_the_suspend() -> None:
    wrapped = MultiTurnReActPolicy(_Finishing({"verdict": "ship", "score": 9}), final=False)
    decision = wrapped.decide(None, None)
    assert isinstance(decision, YieldForHumanDecision)
    assert decision.prompt == NEXT_GOAL_WAKE_HANDLE
    assert decision.answer == {"verdict": "ship", "score": 9}


def test_final_turn_still_finishes_untouched() -> None:
    """``final=True`` has a ``TaskCompleted`` to carry the answer, so nothing
    about that path changes."""
    wrapped = MultiTurnReActPolicy(_Finishing("plain text"), final=True)
    decision = wrapped.decide(None, None)
    assert isinstance(decision, FinishDecision)
    assert decision.answer == "plain text"


# --- the recorded payload ----------------------------------------------------


def test_a_suspend_without_an_answer_is_byte_equal_to_the_old_shape() -> None:
    """What makes this additive rather than a schema bump: ``None`` is omitted
    from the canonical bytes, so every suspend that stands in for no finish
    serializes exactly as it did before the field existed."""
    wake = HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
    payload = TaskSuspendedPayload(reason="waiting_human", wake_on=wake)
    body = to_canonical_bytes(payload)
    assert b"answer" not in body


def test_an_answer_bearing_suspend_serializes_it() -> None:
    wake = HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
    payload = TaskSuspendedPayload(
        reason="waiting_human", wake_on=wake, answer={"verdict": "ship"}
    )
    assert b"verdict" in to_canonical_bytes(payload)


# --- end to end --------------------------------------------------------------


def _client(workspace: Path, *, responses: list[LLMResponse], **opts: Any) -> Client:
    return Client(
        Options(
            system_prompt="you answer briefly",
            name="main",
            permission_mode="bypassPermissions",
            **opts,
        ),
        provider=FakeLLMProvider(responses=responses),
        workspace_dir=workspace,
        model="stub-model",
        multi_turn=True,
    )


def _end_turn(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def test_structured_answer_survives_a_parked_turn(tmp_path: Path) -> None:
    """The case this exists for: multi_turn + output_schema. The host gets the
    deserialized value back without re-parsing the transcript."""
    body = json.dumps({"verdict": "ship", "score": 9})
    client = _client(
        tmp_path, responses=[_end_turn(body)], output_schema=VERDICT_SCHEMA
    )
    try:
        outcome = client.start(goal="judge it")
        assert outcome.status == "suspended"  # parked, not completed
        assert outcome.wake_handle == NEXT_GOAL_WAKE_HANDLE

        assert client.task_answer(outcome.task_id) == {"verdict": "ship", "score": 9}
    finally:
        client.shutdown()


def test_the_answer_lands_on_the_suspend_event(tmp_path: Path) -> None:
    """Durable, not just returned — a later process reading the ledger sees it."""
    body = json.dumps({"verdict": "hold", "score": 2})
    client = _client(
        tmp_path, responses=[_end_turn(body)], output_schema=VERDICT_SCHEMA
    )
    try:
        outcome = client.start(goal="judge it")
        suspend = next(
            env
            for env in reversed(client.events(outcome.task_id))
            if env.type == "TaskSuspended"
        )
        answer = answer_from_payload(suspend.payload, client._host.content_store)
    finally:
        client.shutdown()
    assert answer == {"verdict": "hold", "score": 2}


def test_each_turn_reports_its_own_answer(tmp_path: Path) -> None:
    """``task_answer`` reads the LATEST turn, so a conversation's second answer
    replaces the first rather than stacking."""
    client = _client(
        tmp_path,
        responses=[
            _end_turn(json.dumps({"verdict": "first", "score": 1})),
            _end_turn(json.dumps({"verdict": "second", "score": 2})),
        ],
        output_schema=VERDICT_SCHEMA,
    )
    try:
        outcome = client.start(goal="judge it")
        assert client.task_answer(outcome.task_id)["verdict"] == "first"
        client.send_goal(outcome.task_id, goal="judge it again")
        assert client.task_answer(outcome.task_id)["verdict"] == "second"
    finally:
        client.shutdown()


def test_a_plain_text_conversation_reports_its_text(tmp_path: Path) -> None:
    client = _client(tmp_path, responses=[_end_turn("all done")])
    try:
        outcome = client.start(goal="do a thing")
        assert client.task_answer(outcome.task_id) == "all done"
    finally:
        client.shutdown()


def test_an_oversized_answer_spills_instead_of_bursting_the_envelope(
    tmp_path: Path,
) -> None:
    """The payload ceiling is why ``TaskCompleted`` spills; the suspend inherits
    the same treatment rather than a second, divergent rule."""
    big = json.dumps({"verdict": "x" * 60_000, "score": 1})
    client = _client(tmp_path, responses=[_end_turn(big)], output_schema=VERDICT_SCHEMA)
    try:
        outcome = client.start(goal="judge it")
        suspend = next(
            env
            for env in reversed(client.events(outcome.task_id))
            if env.type == "TaskSuspended"
        )
        assert suspend.payload.answer is None  # moved out of the envelope
        assert suspend.payload.answer_ref is not None
        assert client.task_answer(outcome.task_id)["verdict"] == "x" * 60_000
    finally:
        client.shutdown()


def test_the_added_field_does_not_disturb_fold_or_resume(tmp_path: Path) -> None:
    """A stream carrying the new field still refolds to the same task state,
    which is the property a resume depends on."""
    body = json.dumps({"verdict": "ship", "score": 9})
    client = _client(
        tmp_path, responses=[_end_turn(body)], output_schema=VERDICT_SCHEMA
    )
    try:
        outcome = client.start(goal="judge it")
        host = client._host
        refolded = fold(
            host.event_log, host.content_store, outcome.task_id, ignore_snapshots=True
        )
        accelerated = fold(host.event_log, host.content_store, outcome.task_id)
    finally:
        client.shutdown()
    assert refolded.status == accelerated.status == "suspended"
    assert refolded.state_dict() == accelerated.state_dict()
