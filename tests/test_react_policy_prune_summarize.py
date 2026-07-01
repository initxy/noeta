"""ReActPolicy compaction triggers (③ D-3: proactive + passive).

Both triggers return a :class:`CompactionRequestedDecision` (the unified
contract). The summarize LLM round-trip goes through the injected
``RuntimeLLMClient`` so it is recorded onto the event log.

* proactive — the deterministic pre-call estimate (D-3d) hits the
  available window (``context_window - max_output - buffer``); the main
  LLM call is NOT made this turn (we compact first).
* passive — the provider returned an error response carrying ②'s
  ``raw['category'] == 'overflow'``; the policy compacts before retrying.

Compaction is OFF by default (no ``context_window`` injected) → existing
sessions are unchanged.
"""

from __future__ import annotations

from typing import Any

from noeta.policies.react import ReActPolicy
from noeta.protocols.decisions import (
    CompactionRequestedDecision,
    FailDecision,
    FinishDecision,
)
from noeta.protocols.errors import CATEGORY_OVERFLOW
from noeta.protocols.messages import LLMResponse, Message, TextBlock
from noeta.protocols.step_context import StepContext
from noeta.protocols.token_estimate import estimate_messages_tokens
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.composer import fake_view
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool


def _ctx() -> StepContext:
    return StepContext(task_id="t-1", lease_id="l-1", trace_id="tr-1")


def _big_view(n: int = 40):
    msgs = [
        Message(role="user", content=[TextBlock(text="x" * 200)])
        for _ in range(n)
    ]
    return fake_view(msgs)


def _medium_view(n: int = 10):
    msgs = [
        Message(role="user", content=[TextBlock(text="x" * 200)])
        for _ in range(n)
    ]
    return fake_view(msgs)


def _summary_resp() -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="condensed summary of the conversation")],
    )


def _policy(
    responses: list[LLMResponse],
    *,
    event_log: Any = None,
    content_store: Any = None,
    compaction: bool = True,
    context_window: int = 2000,
) -> tuple[ReActPolicy, FakeLLMProvider, Any, Any]:
    provider = FakeLLMProvider(responses=responses)
    event_log = event_log or InMemoryEventLog()
    content_store = content_store or InMemoryContentStore()
    client = RuntimeLLMClient(
        provider=provider, event_log=event_log, content_store=content_store
    )
    kwargs: dict[str, Any] = {}
    if compaction:
        kwargs.update(
            context_window=context_window,
            max_output_tokens=500,
            compaction_buffer=100,
            tail_token_budget=200,
            composer_version="three_segment.v3",
        )
    policy = ReActPolicy(
        llm=client,
        tools={"echo": FakeTool(name="echo", script={("hi",): "ok"})},
        system_prompt="sys",
        model="gpt-4o",
        **kwargs,
    )
    return policy, provider, event_log, content_store


def test_compaction_off_by_default_no_trigger() -> None:
    resp = LLMResponse(stop_reason="end_turn", content=[TextBlock(text="hi")])
    policy, provider, *_ = _policy([resp], compaction=False)
    decision = policy.decide(_ctx(), _big_view())
    assert isinstance(decision, FinishDecision)
    assert len(provider.received_requests) == 1  # only the normal turn


def test_proactive_trigger_returns_compaction_decision() -> None:
    # The big view estimate exceeds available (2000-500-100=1400); first
    # provider response is consumed by the summarize call.
    policy, provider, *_ = _policy([_summary_resp()])
    decision = policy.decide(_ctx(), _big_view())
    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.reason == "proactive"
    assert decision.summary == "condensed summary of the conversation"
    assert decision.boundary_count > 0
    assert decision.composer_version == "three_segment.v3"
    # Exactly one LLM call: the summarize (no main turn this step).
    assert len(provider.received_requests) == 1


def test_many_small_messages_compact_not_dropped() -> None:
    """Root-cause scenario: a long session of MANY SMALL messages.

    With the legacy count guard gone (default ``max_history_messages=None``),
    a history of 120 short messages whose total estimate fills the window must
    trigger the token summariser (→ ``CompactionRequestedDecision``) instead of
    being silently truncated by a count gate — exactly the ``Compacted == 0 /
    dropped == 97`` regression task-7559 hit. The policy is built WITHOUT a
    ``max_history_messages`` escape hatch, so the only gate that can fire is the
    token one."""
    many_small = [
        Message(role="user", content=[TextBlock(text="x" * 80)])
        for _ in range(120)
    ]
    view = fake_view(many_small)
    policy, provider, *_ = _policy([_summary_resp()])  # no max_history_messages
    assert policy._max_history_messages is None
    decision = policy.decide(_ctx(), view)
    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.boundary_count > 0  # a real prefix was summarised, not dropped
    assert len(provider.received_requests) == 1  # only the summarize call


# ---------------------------------------------------------------------------
# Token trigger mix: real recorded usage + chars/4 delta
# ---------------------------------------------------------------------------


def _ctx_usage(last_input_tokens: int) -> StepContext:
    return StepContext(
        task_id="t-1",
        lease_id="l-1",
        trace_id="tr-1",
        last_input_tokens=last_input_tokens,
    )


def test_trigger_estimate_no_usage_falls_back_to_pure_estimate() -> None:
    """First turn / no recorded usage (``last_input_tokens == 0``) → the mix
    is identical to the legacy pure chars/4 estimate."""
    policy, *_ = _policy([_summary_resp()])
    assert policy._trigger_estimate(_ctx_usage(0), estimated=1234) == 1234


def test_trigger_estimate_real_usage_raises_above_pure_estimate() -> None:
    """Real recorded usage (e.g. cache/structured blocks the chars/4 heuristic
    under-counts) is used as the baseline; with no appended delta yet the mix
    returns ``max(estimated, last_input_tokens)`` — never below the real size."""
    policy, *_ = _policy([_summary_resp()])
    # baseline 5000 >> pure estimate 800; no delta (last_estimate_at_call == 0
    # but estimated 800 < baseline so the +delta clamps to estimated-0=800,
    # then max(800, 5000+800)=5800) — the real usage dominates.
    assert policy._last_estimate_at_call == 0
    got = policy._trigger_estimate(_ctx_usage(5000), estimated=800)
    assert got == 5000 + 800  # baseline + (estimated - 0)
    assert got > 800          # the mix can only RAISE the trigger size


def test_trigger_estimate_adds_appended_delta() -> None:
    """After a real round-trip pins ``_last_estimate_at_call``, the next turn's
    growth (a new tool result) is the chars/4 delta added on top of the real
    baseline."""
    policy, *_ = _policy([_summary_resp()])
    policy._last_estimate_at_call = 1000   # last request we actually sent
    # this turn the request grew to 1300 (≈ a 300-token tool result appended)
    got = policy._trigger_estimate(_ctx_usage(4000), estimated=1300)
    assert got == 4000 + 300               # real baseline + appended delta


def test_trigger_estimate_clamps_shrunk_estimate() -> None:
    """Right after a compaction the request shrinks, so the chars/4 delta goes
    negative — it is clamped to 0 so the mix never dips below the real
    baseline (and ``max`` with the pure estimate still applies)."""
    policy, *_ = _policy([_summary_resp()])
    policy._last_estimate_at_call = 2000
    got = policy._trigger_estimate(_ctx_usage(3000), estimated=500)
    assert got == max(500, 3000 + 0)       # delta clamped, baseline wins


def test_real_usage_triggers_compaction_pure_estimate_would_not() -> None:
    """End-to-end: a history whose pure chars/4 estimate sits UNDER the window
    still compacts when the recorded real usage (carried on the StepContext)
    exceeds it — the precision upgrade that dropping the count gate (D1) made
    necessary."""
    # window = 2000-500-100 = 1400. A medium view estimates well under it, so
    # WITHOUT the mix this would just answer; WITH a real baseline of 1500 the
    # proactive trigger fires and we get a CompactionRequestedDecision.
    policy, provider, *_ = _policy([_summary_resp()])
    view = _medium_view()
    assert estimate_messages_tokens(view.iter_messages()) < 1400
    decision = policy.decide(_ctx_usage(1500), view)
    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.reason == "proactive"
    assert len(provider.received_requests) == 1  # only the summarize call


def test_proactive_not_triggered_when_under_window() -> None:
    resp = LLMResponse(stop_reason="end_turn", content=[TextBlock(text="ok")])
    policy, provider, *_ = _policy([resp])
    decision = policy.decide(_ctx(), fake_view([]))  # empty → tiny estimate
    assert isinstance(decision, FinishDecision)
    assert len(provider.received_requests) == 1


def test_passive_overflow_returns_compaction_decision() -> None:
    overflow = LLMResponse(
        stop_reason="error",
        content=[],
        raw={"category": CATEGORY_OVERFLOW, "error": "context_length_exceeded"},
    )
    # Large window so the proactive estimate does NOT pre-empt — the
    # provider returns the overflow error first (the real tokenizer differs
    # from our cheap estimate), then the summarize response.
    policy, provider, *_ = _policy(
        [overflow, _summary_resp()], context_window=1_000_000
    )
    decision = policy.decide(_ctx(), _medium_view())
    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.reason == "overflow"
    assert decision.summary == "condensed summary of the conversation"
    # Two LLM calls: the overflowing turn + the summarize.
    assert len(provider.received_requests) == 2


def test_proactive_with_nothing_to_summarize_fails_fast() -> None:
    """Finding 3 (policy arm): when the proactive trigger fires but the whole
    history fits inside the protected tail window (boundary == 0), there is
    nothing summarising can collapse. Emitting an empty CompactionRequested
    would spin forever (compose → over window → no-op compact → compose …),
    so the policy fails fast with a non-retryable FailDecision instead."""
    provider = FakeLLMProvider(responses=[])  # would raise if any LLM call
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=store
    )
    # tail_token_budget >= available window (1000-200-100=700) so every
    # message is "protected" → boundary 0 — yet the total estimate exceeds
    # the window so the proactive trigger fires.
    policy = ReActPolicy(
        llm=client,
        tools={"echo": FakeTool(name="echo", script={("hi",): "ok"})},
        system_prompt="sys",
        model="gpt-4o",
        context_window=1000,
        max_output_tokens=200,
        compaction_buffer=100,
        tail_token_budget=100_000,
        composer_version="three_segment.v3",
    )
    decision = policy.decide(_ctx(), _big_view())
    assert isinstance(decision, FailDecision)
    assert decision.retryable is False
    assert "compaction" in decision.reason
    # No summarize LLM call was made (provider would have raised).
    assert len(provider.received_requests) == 0


def test_proactive_when_boundary_already_collapsed_fails_fast() -> None:
    """Fix A (policy primary judge): the proactive trigger fires but the
    summarise boundary the policy would compute does NOT advance past what is
    already collapsed (``view.summary_boundary``) — re-summarising the same
    prefix would spin forever — so the policy self-terminates with a
    non-retryable ``FailDecision(compaction_no_progress)`` and makes NO
    summarize LLM call. This is the per-step guarantee that keeps the kernel's
    boundary-progress arm from ever needing to fire under a good Policy."""
    from dataclasses import replace

    provider = FakeLLMProvider(responses=[])  # would raise if any LLM call
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=store
    )
    policy = ReActPolicy(
        llm=client,
        tools={"echo": FakeTool(name="echo", script={("hi",): "ok"})},
        system_prompt="sys",
        model="gpt-4o",
        context_window=2000,
        max_output_tokens=500,
        compaction_buffer=100,
        tail_token_budget=200,
        composer_version="three_segment.v3",
    )
    view = _big_view()
    # The raw history would compute some boundary > 0; pin summary_boundary at
    # the far end so the freshly computed boundary cannot advance past it.
    view = replace(view, summary_boundary=len(view.rolling_history))
    decision = policy.decide(_ctx(), view)
    assert isinstance(decision, FailDecision)
    assert decision.retryable is False
    assert "compaction" in decision.reason
    assert len(provider.received_requests) == 0


def test_proactive_emits_when_boundary_advances_past_collapsed() -> None:
    """Counterpart: when there IS a new, not-yet-collapsed prefix (the computed
    boundary strictly exceeds ``view.summary_boundary``), the policy DOES emit
    a CompactionRequestedDecision — real progress is never refused."""
    from dataclasses import replace

    policy, provider, *_ = _policy([_summary_resp()])
    view = _big_view()
    # Only the first message is already collapsed; the boundary the policy
    # computes over the long raw history is far larger → progress available.
    view = replace(view, summary_boundary=1)
    decision = policy.decide(_ctx(), view)
    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.boundary_count > 1


def test_summarize_round_trip_is_recorded() -> None:
    """The summarize round-trip goes through the injected
    ``RuntimeLLMClient`` so it is recorded onto the event log (one trio)."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    policy, provider, log, store = _policy(
        [_summary_resp()], event_log=log, content_store=store
    )
    view = _big_view()
    live = policy.decide(_ctx(), view)
    assert isinstance(live, CompactionRequestedDecision)
    assert len(provider.received_requests) == 1
    # The summarize call recorded its three-event trio on the task stream.
    assert [e.type for e in log.read("t-1")] == [
        "LLMRequestStarted",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]
