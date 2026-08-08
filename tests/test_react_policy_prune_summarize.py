"""ReActPolicy compaction triggers: the proactive pre-call estimate and the
passive provider overflow.

Proactive fires when the deterministic estimate hits the available window
(``context_window - max_output - buffer``) and skips the main LLM call for that
turn; passive fires when the provider returns an overflow-category error.  Both
return a :class:`CompactionRequestedDecision`, and the summarize round-trip goes
through the injected ``RuntimeLLMClient`` so it is recorded onto the event log.
Compaction is inert without a configured ``context_window``, so an unconfigured
policy must never compact.
"""

from __future__ import annotations

from typing import Any

from noeta.builtins.react.impl import ReActPolicy
from noeta.protocols.decisions import (
    CompactionRequestedDecision,
    FailDecision,
    FinishDecision,
)
from noeta.protocols.errors import CATEGORY_OVERFLOW
from noeta.protocols.messages import LLMResponse, Message, TextBlock, Usage
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
    """A long session of MANY SMALL messages must compact, not silently drop.

    A history of 120 short messages whose total estimate fills the window has to
    reach the token summariser rather than being truncated by a message-count
    gate. The policy is built without a ``max_history_messages`` escape hatch so
    the token gate is the only one that can fire."""
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
    collapses to the pure chars/4 estimate."""
    policy, *_ = _policy([_summary_resp()])
    state = policy._baseline_state("t-1")
    assert (
        policy._trigger_estimate(_ctx_usage(0), estimated=1234, state=state)
        == 1234
    )


def test_trigger_estimate_real_usage_raises_above_pure_estimate() -> None:
    """Real recorded usage (e.g. cache/structured blocks the chars/4 heuristic
    under-counts) is used as the baseline; with no appended delta yet the mix
    returns ``max(estimated, last_input_tokens)`` — never below the real size."""
    policy, *_ = _policy([_summary_resp()])
    state = policy._baseline_state("t-1")
    # baseline 5000 >> pure estimate 800; no delta (last_estimate_at_call == 0
    # but estimated 800 < baseline so the +delta clamps to estimated-0=800,
    # then max(800, 5000+800)=5800) — the real usage dominates.
    assert state.last_estimate_at_call == 0
    got = policy._trigger_estimate(_ctx_usage(5000), estimated=800, state=state)
    assert got == 5000 + 800  # baseline + (estimated - 0)
    assert got > 800          # the mix can only RAISE the trigger size


def test_trigger_estimate_adds_appended_delta() -> None:
    """After a real round-trip pins ``last_estimate_at_call``, the next turn's
    growth (a new tool result) is the chars/4 delta added on top of the real
    baseline."""
    policy, *_ = _policy([_summary_resp()])
    state = policy._baseline_state("t-1")
    state.last_estimate_at_call = 1000     # last request we actually sent
    # this turn the request grew to 1300 (≈ a 300-token tool result appended)
    got = policy._trigger_estimate(
        _ctx_usage(4000), estimated=1300, state=state
    )
    assert got == 4000 + 300               # real baseline + appended delta


def test_trigger_estimate_clamps_shrunk_estimate() -> None:
    """Right after a compaction the request shrinks, so the chars/4 delta goes
    negative — it is clamped to 0 so the mix never dips below the real
    baseline (and ``max`` with the pure estimate still applies)."""
    policy, *_ = _policy([_summary_resp()])
    state = policy._baseline_state("t-1")
    state.last_estimate_at_call = 2000
    got = policy._trigger_estimate(
        _ctx_usage(3000), estimated=500, state=state
    )
    assert got == max(500, 3000 + 0)       # delta clamped, baseline wins


def test_real_usage_triggers_compaction_pure_estimate_would_not() -> None:
    """End-to-end: a history whose pure chars/4 estimate sits UNDER the window
    still compacts when the recorded real usage (carried on the StepContext)
    exceeds it — the heuristic alone is not precise enough to be the only
    trigger."""
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
    """When the proactive trigger fires but the whole history fits inside the
    protected tail window (boundary == 0), there is nothing summarising can
    collapse. Emitting an empty CompactionRequested would spin forever
    (compose → over window → no-op compact → compose …), so the policy fails
    fast with a non-retryable FailDecision."""
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
    """The proactive trigger fires but the summarise boundary the policy would
    compute does NOT advance past what is already collapsed
    (``view.summary_boundary``) — re-summarising the same prefix would spin
    forever — so the policy self-terminates with a non-retryable
    ``FailDecision(compaction_no_progress)`` and makes NO summarize LLM call.
    This per-step guarantee is what keeps the kernel's boundary-progress arm
    from ever needing to fire under a well-behaved Policy."""
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
    assert [e.type for e in log.read("t-1")] == [
        "LLMRequestStarted",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]


# ---------------------------------------------------------------------------
# Observed-density clamp: a gateway that reports garbage usage must not be
# able to pin the summary boundary at 0
# ---------------------------------------------------------------------------


def _usage_response(text: str, input_tokens: int) -> LLMResponse:
    """An ``end_turn`` turn whose reported input count is under our control."""
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=input_tokens, output=5),
    )


def test_sloppy_gateway_usage_still_compacts() -> None:
    """A provider that reports a nonsense ``input_tokens`` must not disable
    compaction.

    The policy converts ``tail_token_budget`` (REAL provider tokens) into the
    chars/4 unit the boundary accumulates in, by dividing by the observed
    ``real / estimate`` density. The numerator is whatever the provider said —
    so a gateway reporting a flat ``input_tokens=10`` for a multi-thousand-token
    request drives the density toward zero, inflates the protected tail past
    the whole history, and every compaction dies on ``compaction_no_progress``
    with nothing in the logs to explain it. The clamp bounds the damage to a
    factor of four, which the tail budget absorbs.

    Reachable in production precisely because an uncatalogued model now
    compacts at all (D4) — and an uncatalogued model is exactly the sloppy
    gateway population.
    """
    policy, provider, *_ = _policy(
        [
            # Turn 1: a normal round-trip that records the garbage baseline.
            _usage_response("ok", input_tokens=10),
            # Turn 2: the summarize call the triggered compaction makes.
            _summary_resp(),
        ]
    )

    first = policy.decide(_ctx(), _medium_view())
    assert isinstance(first, FinishDecision)

    second = policy.decide(_ctx(), _big_view())
    assert isinstance(second, CompactionRequestedDecision), (
        "a nonsense usage report collapsed the density and pinned the boundary"
    )
    assert second.boundary_count > 0
    assert second.summary == "condensed summary of the conversation"


def test_density_clamp_bounds_both_directions() -> None:
    """The band sits outside the chars/4 heuristic's honest spread — which
    reaches ~4–7 on pure-CJK payloads — so it can only ever catch a
    non-measurement, never distort a genuine one."""
    from noeta.builtins.react.impl.react import _DENSITY_MAX, _DENSITY_MIN

    policy, *_ = _policy([_summary_resp()])
    state = policy._baseline_state("t-1")
    # Absurdly low (the sloppy-gateway shape) and absurdly high (a provider
    # inflating counts, which would shrink the tail to nothing) both clamp.
    state.last_estimate_at_call = 2_600
    state.last_input_tokens_at_call = 10
    assert policy._observed_density(state) == _DENSITY_MIN
    state.last_input_tokens_at_call = 2_600_000
    assert policy._observed_density(state) == _DENSITY_MAX
    # A believable ratio passes through untouched.
    state.last_input_tokens_at_call = 3_120
    assert policy._observed_density(state) == 3_120 / 2_600


# ---------------------------------------------------------------------------
# Options.compaction_model — the cheap summarizer (D7)
# ---------------------------------------------------------------------------


def _policy_with_compaction_model(
    responses: list[LLMResponse],
    compaction_model: Any,
    *,
    compaction_max_output_tokens: int | None = None,
) -> tuple[ReActPolicy, FakeLLMProvider]:
    provider = FakeLLMProvider(responses=responses)
    client = RuntimeLLMClient(
        provider=provider,
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
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
        compaction_model=compaction_model,
        compaction_max_output_tokens=compaction_max_output_tokens,
    )
    return policy, provider


def test_compaction_model_routes_only_the_summarize_call() -> None:
    """Set, it moves the summarize round-trip to the cheap model and leaves
    every decide turn on the main one — condensing text the strong model
    already produced is the one mechanical step in the loop."""
    policy, provider = _policy_with_compaction_model(
        [_usage_response("ok", input_tokens=400), _summary_resp()],
        "claude-haiku-4-5",
    )

    assert isinstance(policy.decide(_ctx(), _medium_view()), FinishDecision)
    assert isinstance(
        policy.decide(_ctx(), _big_view()), CompactionRequestedDecision
    )

    decide_request, summarize_request = provider.received_requests
    assert decide_request.model == "gpt-4o"
    assert summarize_request.model == "claude-haiku-4-5"


def test_compaction_model_unset_is_byte_identical() -> None:
    """Default ``None`` must reproduce the pre-knob request exactly — same
    model, same canonical bytes — so a host that never opts in sees no change
    and a recording made before the knob existed still resumes byte-equal."""
    from noeta.protocols.canonical import to_canonical_bytes

    sent: list[Any] = []
    for compaction_model in (None, "gpt-4o"):
        policy, provider = _policy_with_compaction_model(
            [_summary_resp()], compaction_model
        )
        assert isinstance(
            policy.decide(_ctx(), _big_view()), CompactionRequestedDecision
        )
        (summarize_request,) = provider.received_requests
        assert summarize_request.model == "gpt-4o"
        sent.append(to_canonical_bytes(summarize_request))

    # Unset and "explicitly the main model" are the same request, which is the
    # concrete meaning of "no behaviour change unless opted in".
    assert sent[0] == sent[1]


def test_summarize_request_opts_out_of_tool_use() -> None:
    """The summarize round-trip carries the live tool schemas (the collapsed
    prefix still holds tool blocks), so only the metadata opt-out stops a
    summarizer from answering with a tool call — an only-``tool_use`` response
    has no text and would die as ``compaction_summary_failed``. Decide turns
    must NOT carry the opt-out: the main loop's whole point is tool use."""
    policy, provider, *_ = _policy(
        [_usage_response("ok", input_tokens=400), _summary_resp()]
    )

    assert isinstance(policy.decide(_ctx(), _medium_view()), FinishDecision)
    assert isinstance(
        policy.decide(_ctx(), _big_view()), CompactionRequestedDecision
    )

    decide_request, summarize_request = provider.received_requests
    assert decide_request.metadata.get("tool_choice") is None
    assert summarize_request.metadata.get("tool_choice") == "none"


def test_compaction_model_ceiling_caps_the_summarize_request() -> None:
    """The summarize request's ``max_tokens`` must be valid for the model that
    SERVES it: with a compaction model set, the host-derived ceiling replaces
    the main model's cap, which may exceed the summarizer's real limit — a
    provider 400 that would kill every proactive compaction."""
    policy, provider = _policy_with_compaction_model(
        [_summary_resp()],
        "claude-haiku-4-5",
        compaction_max_output_tokens=300,
    )
    assert isinstance(
        policy.decide(_ctx(), _big_view()), CompactionRequestedDecision
    )
    (summarize_request,) = provider.received_requests
    assert summarize_request.max_tokens == 300


def test_compaction_model_without_ceiling_falls_back_to_main_cap() -> None:
    """An old caller that threads ``compaction_model`` but not the ceiling
    keeps the pre-knob behaviour: the main model's cap rides the request."""
    policy, provider = _policy_with_compaction_model(
        [_summary_resp()], "claude-haiku-4-5"
    )
    assert isinstance(
        policy.decide(_ctx(), _big_view()), CompactionRequestedDecision
    )
    (summarize_request,) = provider.received_requests
    assert summarize_request.max_tokens == 500


# ---------------------------------------------------------------------------
# Errored round-trips must not move the trigger baseline
# ---------------------------------------------------------------------------


def test_errored_round_trip_does_not_pin_the_trigger_baseline() -> None:
    """Exception-shaped errors carry an empty Usage, but a 200-shape error
    (e.g. a gateway overflow body) can carry the provider's REAL count — for a
    request whose history is about to change. The baseline must only ever come
    from a successful round-trip; nothing may depend on the downstream
    ``_BASELINE_INVALIDATED`` reset to clean up after an errored one."""
    error = LLMResponse(
        stop_reason="error",
        content=[],
        usage=Usage(uncached=1234, output=0),
        raw={"category": "fatal"},
    )
    policy, *_ = _policy([error], context_window=1_000_000)

    decision = policy.decide(_ctx(), _medium_view())

    assert isinstance(decision, FailDecision)
    assert policy._baseline_state("t-1").last_input_tokens_at_call == 0


# ---------------------------------------------------------------------------
# Baselines are task-scoped: one cached policy instance serves many tasks
# ---------------------------------------------------------------------------


def test_one_tasks_baseline_never_bleeds_into_another() -> None:
    """The policy instance is cached inside an Engine shared by every task
    with equal bindings, so the trigger baselines MUST be keyed per task: a
    conversation that pinned a near-window real usage must not make a fresh
    conversation's first turn fire the proactive trigger (which, with a tiny
    history, dies as non-retryable ``compaction_no_progress`` before the new
    conversation ever reaches the provider)."""
    ok = LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="ok")],
        usage=Usage(uncached=100_000, output=5),
    )
    ok2 = LLMResponse(stop_reason="end_turn", content=[TextBlock(text="hi")])
    # window = 2000-500-100 = 1400; task A's recorded usage (100k) dwarfs it.
    policy, provider, *_ = _policy([ok, ok2])

    a = StepContext(task_id="task-a", lease_id="l", trace_id="t")
    first = policy.decide(a, _medium_view())
    assert isinstance(first, FinishDecision)
    assert (
        policy._baseline_state("task-a").last_input_tokens_at_call == 100_000
    )

    # Task B on the SAME instance: fresh baseline → pure (tiny) estimate →
    # no trigger → a normal provider round-trip, not a compaction death.
    b = StepContext(task_id="task-b", lease_id="l", trace_id="t")
    second = policy.decide(b, _medium_view())
    assert isinstance(second, FinishDecision)
    assert len(provider.received_requests) == 2
    # And B's round-trip did not disturb A's pinned baseline.
    assert (
        policy._baseline_state("task-a").last_input_tokens_at_call == 100_000
    )


def test_baseline_table_evicts_oldest_beyond_the_cap() -> None:
    """The per-task table is bounded: pushing past the cap drops the oldest
    entries, and an evicted task simply restarts from the fresh-entry
    fallback (pure estimate) — no unbounded growth on a long-lived host."""
    from noeta.builtins.react.impl.react import _MAX_TRACKED_TASK_BASELINES

    policy, *_ = _policy([_summary_resp()])
    for i in range(_MAX_TRACKED_TASK_BASELINES + 10):
        policy._baseline_state(f"task-{i}")
    assert len(policy._baselines) == _MAX_TRACKED_TASK_BASELINES
    assert "task-0" not in policy._baselines
    assert f"task-{_MAX_TRACKED_TASK_BASELINES + 9}" in policy._baselines
