"""``Client.deliver_event`` — the external-event wake verb.

The ``wait_external`` Decision branch suspends on the ``ExternalEvent`` wake
condition (projection-matching on ``event_kind``); the internal delivery path
(``dispatcher.wake`` from a host ingress) is covered by
``tests/test_timer_poller.py``. This file proves the **exposed** verb: an SDK
caller wakes the suspend through ``Client.deliver_event``, the optional
``payload`` rides the message channel (never the wake event), and every
mis-delivery (wrong ``event_kind`` / not waiting / terminal / repeat) raises
the same typed ``NotResumableError`` a repeat ``answer`` does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.agent.spec import ComponentRef
from noeta.protocols.decisions import FinishDecision, WaitExternalDecision
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.protocols.wake import ExternalEvent
from noeta.sdk import Client, NotResumableError, Options
from noeta.testing.fake_llm import FakeLLMProvider


EVENT_KIND = "webhook:payment"


class _WaitExternalThenFinishPolicy:
    """Scripted: suspend on ``wait_external`` once, then finish."""

    def __init__(self) -> None:
        self._decisions = [
            WaitExternalDecision(event_kind=EVENT_KIND),
            FinishDecision(answer="paid"),
        ]

    def decide(self, ctx, view):  # noqa: ARG002 — scripted
        return self._decisions.pop(0)


class _WaitExternalPolicyProvider:
    """``Options.policy`` shape: ``(llm) -> Policy`` carrying a ``.ref``."""

    @property
    def ref(self) -> ComponentRef:
        return ComponentRef("wait-external-scripted", "1")

    def __call__(self, llm) -> _WaitExternalThenFinishPolicy:  # noqa: ARG002
        return _WaitExternalThenFinishPolicy()


def _client(tmp_path: Path) -> Client:
    return Client(
        Options(
            system_prompt="you wait for an external event",
            name="main",
            allowed_tools=(),
            policy=_WaitExternalPolicyProvider(),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(responses=[]),  # scripted policy: never called
        workspace_dir=tmp_path,
        multi_turn=False,  # FinishDecision reaches a real TaskCompleted
    )


def test_deliver_event_wakes_wait_external_suspend_to_terminal(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    try:
        outcome = client.start(goal="wait for the payment webhook")
        assert outcome.status == "suspended"
        # Suspended on the ExternalEvent wake — not a human handle.
        assert outcome.wake_handle is None
        events = client.events(outcome.task_id)
        suspend = [e for e in events if e.type == "TaskSuspended"][-1]
        assert suspend.payload.reason == "waiting_external"
        assert suspend.payload.wake_on == ExternalEvent(event_kind=EVENT_KIND)

        outcome = client.deliver_event(
            outcome.task_id,
            event_kind=EVENT_KIND,
            payload={"amount": 42, "currency": "EUR"},
        )

        assert outcome.status == "terminal"
        types = [e.type for e in client.events(outcome.task_id)]
        assert "TaskWoken" in types
        assert types[-1] == "TaskCompleted"
        # The payload rode the message channel as a source-tagged notice.
        texts = [
            str(getattr(m, "text", getattr(m, "answer", "")))
            for m in client.messages(outcome.task_id)
        ]
        assert any("<external-event kind=\"webhook:payment\">" in t for t in texts)
        assert any('"amount": 42' in t for t in texts)
    finally:
        client.shutdown()


def test_deliver_event_without_payload_records_no_notice(
    tmp_path: Path,
) -> None:
    """``payload=None`` seeds no prelude — the resumed turn is the plain woken
    branch, byte-identical to an internal (daemon-ingress) wake delivery."""
    client = _client(tmp_path)
    try:
        outcome = client.start(goal="wait")
        outcome = client.deliver_event(outcome.task_id, event_kind=EVENT_KIND)
        assert outcome.status == "terminal"
        texts = [
            str(getattr(m, "text", getattr(m, "answer", "")))
            for m in client.messages(outcome.task_id)
        ]
        assert not any("<external-event" in t for t in texts)
    finally:
        client.shutdown()


def test_deliver_event_wrong_kind_is_typed_and_leaves_suspend_intact(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    try:
        outcome = client.start(goal="wait")
        with pytest.raises(NotResumableError) as exc_info:
            client.deliver_event(outcome.task_id, event_kind="webhook:other")
        assert exc_info.value.code == "not_resumable"
        assert "webhook:other" in str(exc_info.value)
        # The mis-delivery left no durable write: the task is still suspended
        # on its declared kind and the correct delivery still resumes it.
        outcome = client.deliver_event(outcome.task_id, event_kind=EVENT_KIND)
        assert outcome.status == "terminal"
    finally:
        client.shutdown()


def test_deliver_event_to_terminal_task_raises_not_resumable(
    tmp_path: Path,
) -> None:
    """A repeat delivery after the wake was consumed mirrors a repeat answer:
    the same typed ``not_resumable`` refusal, never a silent ack."""
    client = _client(tmp_path)
    try:
        outcome = client.start(goal="wait")
        outcome = client.deliver_event(outcome.task_id, event_kind=EVENT_KIND)
        assert outcome.status == "terminal"
        with pytest.raises(NotResumableError) as exc_info:
            client.deliver_event(outcome.task_id, event_kind=EVENT_KIND)
        assert exc_info.value.code == "not_resumable"
        assert exc_info.value.status == "terminal"
    finally:
        client.shutdown()


def test_deliver_event_to_task_not_waiting_external_raises(
    tmp_path: Path,
) -> None:
    """A task suspended on a HUMAN handle (the trailing next-goal suspend of a
    multi-turn conversation) must not be woken by an external delivery."""
    client = Client(
        Options(
            system_prompt="you finish immediately",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="done")],
                    usage=Usage(uncached=1, output=1),
                )
            ]
        ),
        workspace_dir=tmp_path,
        multi_turn=True,  # turn lands on the next-goal human suspend
    )
    try:
        outcome = client.start(goal="hello")
        assert outcome.status == "suspended"
        assert outcome.wake_handle is not None  # a human handle, not external
        with pytest.raises(NotResumableError) as exc_info:
            client.deliver_event(outcome.task_id, event_kind=EVENT_KIND)
        assert exc_info.value.code == "not_resumable"
    finally:
        client.shutdown()
