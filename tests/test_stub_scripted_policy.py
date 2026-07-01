"""StubScriptedPolicy: returns a predetermined Decision sequence.

Used in integration tests to choreograph multi-step Engine runs without
a real LLM. The policy pops one Decision per ``decide`` call and raises
if the script is exhausted (so test bugs surface immediately rather
than infinite-looping the Engine).
"""

from __future__ import annotations

import pytest

from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, ToolCall, ToolCallsDecision
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View
from noeta.testing.composer import fake_view


def _empty_view() -> View:
    return fake_view([])


def _ctx() -> StepContext:
    return StepContext(task_id="t-1", lease_id="lease-1", trace_id="trace-1")


def test_script_yields_decisions_in_order() -> None:
    d1 = ToolCallsDecision(calls=[ToolCall("t", {}, call_id="c1")])
    d2 = FinishDecision(answer="done")
    policy = StubScriptedPolicy([d1, d2])

    assert policy.decide(_ctx(), _empty_view()) is d1
    assert policy.decide(_ctx(), _empty_view()) is d2


def test_exhausted_script_raises() -> None:
    policy = StubScriptedPolicy([FinishDecision(answer="done")])
    policy.decide(_ctx(), _empty_view())

    with pytest.raises(IndexError):
        policy.decide(_ctx(), _empty_view())
