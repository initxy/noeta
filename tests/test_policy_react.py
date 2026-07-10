"""ReActPolicy unit tests.

Drives ``decide(ctx, view) -> Decision`` against a scripted
:class:`noeta.testing.fake_llm.FakeLLMProvider` so the path is fully
deterministic and offline. Each case isolates one behavior of the policy.

The policy is wrapped in a :class:`noeta.runtime.llm.RuntimeLLMClient`
because that is the production wiring: Policy talks to the wrapper,
wrapper talks to the provider. We assert on the Decision shape, the
LLMRequest the provider received, and the per-call event count.
"""

from __future__ import annotations

from typing import Any

from noeta.policies.react import ReActPolicy
from noeta.protocols.decisions import (
    FailDecision,
    FinishDecision,
    ToolCallsDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(task_id: str = "task-1") -> StepContext:
    return StepContext(
        task_id=task_id, lease_id="lease-1", trace_id="trace-1"
    )


from noeta.testing.composer import fake_view  # noqa: E402


def _empty_view() -> View:
    return fake_view([])


def _make_client(
    responses: list[LLMResponse],
) -> tuple[RuntimeLLMClient, FakeLLMProvider]:
    provider = FakeLLMProvider(responses=responses)
    client = RuntimeLLMClient(
        provider=provider,
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
    )
    return client, provider


def _make_policy(
    responses: list[LLMResponse],
    *,
    tools: dict[str, Any] | None = None,
    system_prompt: str = "you are helpful",
    model: str = "gpt-4o",
    max_steps: int = 50,
    max_history_messages: int = 50,
) -> tuple[ReActPolicy, FakeLLMProvider]:
    client, provider = _make_client(responses)
    if tools is None:
        tools = {"echo": FakeTool(name="echo", script={("hi",): "ok"})}
    policy = ReActPolicy(
        llm=client,
        tools=tools,
        system_prompt=system_prompt,
        model=model,
        max_steps=max_steps,
        max_history_messages=max_history_messages,
    )
    return policy, provider


# ---------------------------------------------------------------------------
# stop_reason translation
# ---------------------------------------------------------------------------


def test_end_turn_response_becomes_finish_decision_with_text_joined() -> None:
    """Two TextBlocks → single answer joined by ``\n`` + assistant_message."""
    resp = LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="hello"), TextBlock(text="world")],
    )
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, FinishDecision)
    assert decision.answer == "hello\nworld"
    assert decision.assistant_message is not None
    assert decision.assistant_message.role == "assistant"
    assert decision.assistant_message.content == resp.content


def test_empty_end_turn_fails_instead_of_recording_empty_message() -> None:
    """An ``end_turn`` with no renderable content (e.g. a safety-classifier
    ``refusal`` now mapped to ``end_turn`` that came back with an empty content
    array) must NOT record a ``Message(content=[])`` — Anthropic 400s on
    ``{"role":"assistant","content":[]}`` on the next request. It fails cleanly
    instead, leaving history unpolluted."""
    resp = LLMResponse(stop_reason="end_turn", content=[])
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_empty_response"
    assert decision.retryable is False
    assert decision.assistant_message is None


def test_tool_use_response_becomes_tool_calls_decision_one_call() -> None:
    """Single ToolUseBlock → ToolCallsDecision with one ToolCall preserving
    call_id / tool_name / arguments verbatim."""
    block = ToolUseBlock(
        call_id="call-xyz", tool_name="echo", arguments={"text": "hi"}
    )
    resp = LLMResponse(stop_reason="tool_use", content=[block])
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, ToolCallsDecision)
    assert len(decision.calls) == 1
    call = decision.calls[0]
    assert call.call_id == "call-xyz"
    assert call.tool_name == "echo"
    assert call.arguments == {"text": "hi"}
    assert decision.assistant_message == Message(
        role="assistant", content=[block]
    )


def test_tool_use_response_preserves_three_tool_use_blocks_in_order() -> None:
    """3 parallel ToolUseBlocks → 3 ToolCalls in original order."""
    blocks = [
        ToolUseBlock(call_id=f"c-{i}", tool_name="echo", arguments={"i": i})
        for i in range(3)
    ]
    resp = LLMResponse(stop_reason="tool_use", content=list(blocks))
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, ToolCallsDecision)
    assert [c.call_id for c in decision.calls] == ["c-0", "c-1", "c-2"]
    assert [c.arguments for c in decision.calls] == [
        {"i": 0},
        {"i": 1},
        {"i": 2},
    ]


def test_tool_use_with_mixed_text_and_thinking_blocks_drops_thinking() -> None:
    """``stop_reason=tool_use`` with mixed Thinking/Text/ToolUse content:
    ToolCall is extracted from the ToolUse block; the assistant_message
    that lands in RuntimeState.messages **drops** ThinkingBlock so the
    history stays deterministic across Verify runs (reasoning trace is
    non-deterministic even at temperature=0). TextBlock + ToolUseBlock
    are preserved in order."""
    blocks: list[Any] = [
        ThinkingBlock(text="let me think", signature="sig-abc"),
        TextBlock(text="I'll call echo"),
        ToolUseBlock(call_id="c-1", tool_name="echo", arguments={"x": 1}),
    ]
    resp = LLMResponse(stop_reason="tool_use", content=list(blocks))
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, ToolCallsDecision)
    assert len(decision.calls) == 1
    assert decision.assistant_message is not None
    assert decision.assistant_message.content == [
        TextBlock(text="I'll call echo"),
        ToolUseBlock(call_id="c-1", tool_name="echo", arguments={"x": 1}),
    ]
    assert not any(
        isinstance(b, ThinkingBlock)
        for b in decision.assistant_message.content
    )


def test_tool_use_carries_thinking_out_of_band_on_decision() -> None:
    """Extended-thinking end-to-end (Slice B): the ThinkingBlock the LLM
    emitted ahead of its ``tool_use`` is stripped from ``assistant_message``
    (the persisted, verify-stable history) but PRESERVED out-of-band on
    ``ToolCallsDecision.assistant_thinking`` — so the Engine can record it
    and the Composer can replay the signature on an Anthropic continuation.
    """
    thinking = ThinkingBlock(text="let me think", signature="sig-abc")
    resp = LLMResponse(
        stop_reason="tool_use",
        content=[
            thinking,
            ToolUseBlock(call_id="c-1", tool_name="echo", arguments={"x": 1}),
        ],
    )
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, ToolCallsDecision)
    # carried out-of-band, verbatim (signature intact)...
    assert decision.assistant_thinking == (thinking,)
    # ...and absent from the persisted assistant turn.
    assert decision.assistant_message is not None
    assert not any(
        isinstance(b, ThinkingBlock)
        for b in decision.assistant_message.content
    )


def test_thinking_only_response_yields_empty_history_content() -> None:
    """A response that is *only* a ThinkingBlock + a behaviour block
    (no other text) still drops the thinking from the history, keeping
    just the behaviour-bearing piece. Guards against accidental
    "keep thinking when content shrinks" regression."""
    resp = LLMResponse(
        stop_reason="end_turn",
        content=[
            ThinkingBlock(text="ponder ponder"),
            TextBlock(text="final answer"),
        ],
    )
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, FinishDecision)
    assert decision.assistant_message is not None
    assert decision.assistant_message.content == [
        TextBlock(text="final answer")
    ]
    # Answer is still the user-visible TextBlock — thinking does not
    # leak into the FinishDecision.answer either.
    assert decision.answer == "final answer"


def test_max_tokens_response_becomes_retryable_fail_decision() -> None:
    """``stop_reason=max_tokens`` → ``FailDecision(reason="llm_truncated",
    retryable=True, assistant_message=...)``."""
    resp = LLMResponse(
        stop_reason="max_tokens",
        content=[TextBlock(text="partial...")],
    )
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_truncated"
    assert decision.retryable is True
    assert decision.assistant_message == Message(
        role="assistant", content=resp.content
    )


def test_max_tokens_response_all_thinking_fails_instead_of_recording_empty_message() -> None:
    """Mirrors ``test_empty_end_turn_fails_instead_of_recording_empty_message``:
    a reasoning model that spends its whole output budget on ThinkingBlock(s)
    before any text/tool_use leaves ``history_content`` empty on
    ``max_tokens`` too (thinking is stripped by ``_strip_thinking``).
    Recording ``Message(content=[])`` here would reproduce the Anthropic 400
    on the next request — and since this branch is normally retryable, a
    retry would resend the very history a poisoned turn just wrote. Guard
    identically to the end_turn branch instead: fail non-retryable with no
    assistant_message."""
    resp = LLMResponse(
        stop_reason="max_tokens",
        content=[ThinkingBlock(text="still reasoning...")],
    )
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_empty_response"
    assert decision.retryable is False
    assert decision.assistant_message is None


def test_error_response_becomes_non_retryable_fail_with_none_message() -> None:
    """``stop_reason=error`` (no category) → ``FailDecision(reason=
    "llm_error", retryable=False, assistant_message=None)``. Engine sees no
    assistant_message so the rolling history is not contaminated by an
    empty / error response. Old recordings whose error ``raw`` has no
    ``category`` key keep this behaviour (backward compatible)."""
    resp = LLMResponse(stop_reason="error", content=[], raw={"error": "boom"})
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_error"
    assert decision.retryable is False
    assert decision.assistant_message is None


def test_fatal_category_error_becomes_non_retryable_fail() -> None:
    """② error recovery: ``raw['category'] == 'fatal'`` →
    ``FailDecision(reason="llm_error", retryable=False)``. A fatal class
    (auth / malformed request) is not worth retrying."""
    resp = LLMResponse(
        stop_reason="error",
        content=[],
        raw={"error": "unauthorized", "category": "fatal", "retry_after": None},
    )
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_error"
    assert decision.retryable is False
    assert decision.assistant_message is None


def test_transient_category_does_not_reach_policy_but_is_handled_as_fail() -> None:
    """Transient errors are consumed inside the runtime LLM client and
    never surface to Policy. Should one ever arrive (defensive), it must
    NOT loop forever — Policy treats an unrecognised / transient error
    response the same as a plain error: a non-retryable FailDecision (the
    runtime already exhausted its retry budget before stamping the
    category)."""
    resp = LLMResponse(
        stop_reason="error",
        content=[],
        raw={"error": "rate limited", "category": "transient", "retry_after": None},
    )
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_error"
    assert decision.retryable is False


# ---------------------------------------------------------------------------
# 3-step scripted ReAct: tool_use, tool_use, end_turn
# ---------------------------------------------------------------------------


def test_three_step_scripted_react_produces_expected_decision_sequence() -> None:
    """A canonical ReAct run: two tool_use steps then end_turn. Decisions
    come out in the expected order and each carries its own
    assistant_message."""
    r1 = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id="c1", tool_name="echo", arguments={"i": 1})
        ],
    )
    r2 = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id="c2", tool_name="echo", arguments={"i": 2})
        ],
    )
    r3 = LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text="all done")]
    )
    policy, provider = _make_policy([r1, r2, r3])

    d1 = policy.decide(_ctx(), _empty_view())
    d2 = policy.decide(_ctx(), _empty_view())
    d3 = policy.decide(_ctx(), _empty_view())

    assert isinstance(d1, ToolCallsDecision)
    assert isinstance(d2, ToolCallsDecision)
    assert isinstance(d3, FinishDecision)
    assert d3.answer == "all done"
    # Each Decision rebrandished its own assistant_message — never None,
    # never reused from the previous round.
    assert d1.assistant_message is not None
    assert d2.assistant_message is not None
    assert d3.assistant_message is not None
    assert d1.assistant_message != d2.assistant_message
    assert len(provider.received_requests) == 3


# ---------------------------------------------------------------------------
# LLMRequest construction (system / model / tools / messages)
# ---------------------------------------------------------------------------


def test_request_system_field_carries_view_stable_prefix() -> None:
    """Issue 14: ``LLMRequest.system`` comes from the View's
    stable_prefix segment (Composer is the SoT for prompt material),
    not from ReActPolicy's constructor. ``LLMRequest.messages`` still
    contains no ``role="system"`` Message — system flows separately."""
    resp = LLMResponse(stop_reason="end_turn", content=[TextBlock(text="ok")])
    policy, provider = _make_policy(
        [resp], system_prompt="constructor-supplied (ignored when View has segments)"
    )

    user_msg = Message(role="user", content=[TextBlock(text="hi there")])
    view = fake_view([user_msg], system_prompt="composer-supplied prompt")
    policy.decide(_ctx(), view)

    req = provider.received_requests[0]
    assert req.system == view.segments[0].content[0]
    assert req.system.content[0].text == "composer-supplied prompt"
    assert all(m.role != "system" for m in req.messages)


def test_request_model_field_is_constructor_value() -> None:
    """``LLMRequest.model`` is whatever the constructor was given."""
    resp = LLMResponse(stop_reason="end_turn", content=[TextBlock(text="ok")])
    policy, provider = _make_policy([resp], model="gpt-4o-mini")

    policy.decide(_ctx(), _empty_view())

    assert provider.received_requests[0].model == "gpt-4o-mini"


def test_request_tools_field_mirrors_view_provider_tool_schemas() -> None:
    """Issue 14: ``LLMRequest.tools`` comes from ``view.provider_tool_schemas``
    (the Composer is the SoT for the tool roster), not from
    ReActPolicy's constructor tools dict."""
    resp = LLMResponse(stop_reason="end_turn", content=[TextBlock(text="ok")])
    schema_a = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    schema_b = {"type": "object", "additionalProperties": True}
    tool_a = FakeTool(name="alpha", input_schema=schema_a)
    tool_b = FakeTool(name="beta", input_schema=schema_b)
    policy, provider = _make_policy(
        [resp], tools={"alpha": tool_a, "beta": tool_b}
    )

    expected_provider_tool_schemas = [
        {"type": "function", "function": {"name": "alpha", "parameters": schema_a}},
        {"type": "function", "function": {"name": "beta", "parameters": schema_b}},
    ]
    policy.decide(_ctx(), fake_view([], provider_tool_schemas=expected_provider_tool_schemas))

    req = provider.received_requests[0]
    assert req.tools == expected_provider_tool_schemas
    assert req.tools == [
        {"type": "function", "function": {"name": "alpha", "parameters": schema_a}},
        {"type": "function", "function": {"name": "beta", "parameters": schema_b}},
    ]


def test_history_truncation_keeps_only_last_n_messages() -> None:
    """``max_history_messages=10`` + 200 messages in the view → the
    outgoing ``LLMRequest.messages`` is the tail-most 10. ``system`` is
    not counted (lives on its own field)."""
    resp = LLMResponse(stop_reason="end_turn", content=[TextBlock(text="ok")])
    policy, provider = _make_policy([resp], max_history_messages=10)
    big_history = [
        Message(role="user", content=[TextBlock(text=f"m-{i}")])
        for i in range(200)
    ]

    policy.decide(_ctx(), fake_view(big_history))

    req = provider.received_requests[0]
    assert len(req.messages) == 10
    assert req.messages == big_history[-10:]


# ---------------------------------------------------------------------------
# max_steps ceiling
# ---------------------------------------------------------------------------


def test_max_steps_ceiling_fails_after_n_calls_without_calling_provider() -> None:
    """A 51-long tool_use script + ``max_steps=50``: the 51st ``decide``
    returns ``FailDecision(reason="react_max_steps_exceeded",
    retryable=False)`` and the FakeLLMProvider was called exactly 50
    times."""
    tool_use = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id="x", tool_name="echo", arguments={"i": 0})
        ],
    )
    policy, provider = _make_policy([tool_use] * 51, max_steps=50)

    for _ in range(50):
        d = policy.decide(_ctx(), _empty_view())
        assert isinstance(d, ToolCallsDecision)

    final = policy.decide(_ctx(), _empty_view())
    assert isinstance(final, FailDecision)
    assert final.reason == "react_max_steps_exceeded"
    assert final.retryable is False
    assert len(provider.received_requests) == 50
