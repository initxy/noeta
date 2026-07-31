"""``ReActPolicy`` — translating a provider response into a ``Decision``.

Every ``stop_reason`` the provider can return has to land on exactly one
Decision shape, and the assistant turn the policy hands back is what gets
persisted — a wrong shape there poisons the *next* request's history. Cases
drive ``decide(ctx, view)`` through a :class:`noeta.runtime.llm.RuntimeLLMClient`
over a scripted ``FakeLLMProvider``, which is the production wiring and keeps
the path deterministic and offline.
"""

from __future__ import annotations

from typing import Any

from noeta.builtins.react.impl import ReActPolicy
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
    """Two TextBlocks join into one answer separated by a newline."""
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
    """An ``end_turn`` with no renderable content (a safety-classifier refusal
    arrives this way) must NOT record a ``Message(content=[])`` — Anthropic 400s
    on ``{"role":"assistant","content":[]}`` in the next request. Fail cleanly
    instead, leaving history unpolluted."""
    resp = LLMResponse(stop_reason="end_turn", content=[])
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_empty_response"
    assert decision.retryable is False
    assert decision.assistant_message is None


def test_tool_use_response_becomes_tool_calls_decision_one_call() -> None:
    """call_id / tool_name / arguments survive the translation verbatim."""
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
    """The assistant_message that lands in RuntimeState.messages **drops**
    ThinkingBlock, so the persisted history stays deterministic across replays
    (a reasoning trace varies even at temperature=0). TextBlock and
    ToolUseBlock keep their order."""
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
    """The ThinkingBlock the LLM emitted ahead of its ``tool_use`` is stripped
    from ``assistant_message`` (the persisted, replay-stable history) but
    PRESERVED out-of-band on ``ToolCallsDecision.assistant_thinking`` — so the
    Engine can record it and the Composer can replay the signature on an
    Anthropic continuation.
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
    """Thinking is dropped even when it is most of the response: the history
    keeps only the behaviour-bearing block, and it never leaks into
    ``FinishDecision.answer`` either."""
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
    assert decision.answer == "final answer"


def test_max_tokens_response_becomes_retryable_fail_decision() -> None:
    """A truncated turn is retryable, and its partial text is still recorded."""
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
    """A reasoning model can spend its whole output budget on ThinkingBlock(s),
    which ``_strip_thinking`` removes — leaving ``history_content`` empty on
    ``max_tokens`` too. Recording ``Message(content=[])`` here is worse than on
    ``end_turn``: this branch is normally retryable, so the retry would resend
    the very history the poisoned turn just wrote. Fail non-retryable with no
    assistant_message instead."""
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
    """An uncategorised ``error`` is non-retryable and carries no
    assistant_message, so the Engine never contaminates the rolling history
    with an empty / error response."""
    resp = LLMResponse(stop_reason="error", content=[], raw={"error": "boom"})
    policy, _ = _make_policy([resp])

    decision = policy.decide(_ctx(), _empty_view())

    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_error"
    assert decision.retryable is False
    assert decision.assistant_message is None


def test_fatal_category_error_becomes_non_retryable_fail() -> None:
    """A ``fatal`` class (auth / malformed request) is not worth retrying."""
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
    """Transient errors are consumed inside the runtime LLM client and never
    surface to Policy. Should one arrive anyway it must NOT loop forever: the
    runtime already exhausted its retry budget before stamping the category, so
    Policy treats it exactly like a plain error — a non-retryable
    FailDecision."""
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
    """Two tool_use steps then end_turn: each Decision carries its own
    assistant_message, never one reused from the previous round."""
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
    assert d1.assistant_message is not None
    assert d2.assistant_message is not None
    assert d3.assistant_message is not None
    assert d1.assistant_message != d2.assistant_message
    assert len(provider.received_requests) == 3


# ---------------------------------------------------------------------------
# LLMRequest construction (system / model / tools / messages)
# ---------------------------------------------------------------------------


def test_request_system_field_carries_view_stable_prefix() -> None:
    """``LLMRequest.system`` comes from the View's stable_prefix segment — the
    Composer owns prompt material, not ReActPolicy's constructor — and
    ``LLMRequest.messages`` carries no ``role="system"`` Message, because
    system flows on its own field."""
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
    """``LLMRequest.tools`` comes from ``view.provider_tool_schemas`` — the
    Composer owns the tool roster, not ReActPolicy's constructor tools dict."""
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
    """Truncation keeps the tail, not the head; ``system`` is not counted
    against the budget because it rides its own field."""
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
    """The ceiling trips before the provider is called again — a runaway loop
    costs exactly ``max_steps`` requests, not one more."""
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
