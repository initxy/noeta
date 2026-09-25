"""ReAct policy edges: what a truncated turn leaves in history, what a
provider failure tells the host, and a ``structured_output`` sent next to
other calls.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from noeta.builtins.react.impl import STRUCTURED_OUTPUT_TOOL, ReActPolicy
from noeta.builtins.react.impl.orchestration import (
    MAX_STRUCTURED_OUTPUT_NUDGES,
    StructuredOutputPolicy,
)
from noeta.protocols.decisions import (
    FailDecision,
    FinishDecision,
    StatePatchDecision,
    ToolCall,
    ToolCallsDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.runtime.llm import RuntimeLLMClient
from noeta.sdk import Client, Options
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.composer import fake_view
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool


def _decide(response: LLMResponse):
    client = RuntimeLLMClient(
        provider=FakeLLMProvider(responses=[response]),
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
    )
    policy = ReActPolicy(
        llm=client,
        tools={"echo": FakeTool(name="echo", script={("hi",): "ok"})},
        system_prompt="you are helpful",
        model="gpt-4o",
    )
    ctx = StepContext(task_id="task-1", lease_id="lease-1", trace_id="trace-1")
    return policy.decide(ctx, fake_view([]))


# ---------------------------------------------------------------------------
# B1: a max_tokens turn never records its tool calls
# ---------------------------------------------------------------------------


def test_truncated_turn_keeps_text_and_drops_tool_calls() -> None:
    decision = _decide(
        LLMResponse(
            stop_reason="max_tokens",
            content=[
                TextBlock(text="reading"),
                ToolUseBlock(call_id="t1", tool_name="echo", arguments={"x": "hi"}),
            ],
        )
    )
    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_truncated"
    assert decision.retryable is True
    assert decision.assistant_message == Message(
        role="assistant", content=[TextBlock(text="reading")]
    )


def test_truncated_turn_of_only_tool_calls_records_nothing() -> None:
    decision = _decide(
        LLMResponse(
            stop_reason="max_tokens",
            content=[ToolUseBlock(call_id="t1", tool_name="echo", arguments={})],
        )
    )
    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_truncated"
    assert decision.assistant_message is None


def test_next_turn_after_truncation_carries_no_dangling_tool_use(tmp_path: Path) -> None:
    """End to end over multi_turn: the follow-up request must not carry a
    tool_use without its result (Anthropic and OpenAI reject that with 400)."""
    ws = tmp_path
    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="max_tokens",
                content=[
                    TextBlock(text="reading"),
                    ToolUseBlock(
                        call_id="toolu_1",
                        tool_name="Read",
                        arguments={"file_path": str(ws / "a")},
                    ),
                ],
                usage=Usage(uncached=1, output=1),
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="ok")],
                usage=Usage(uncached=1, output=1),
            ),
        ]
    )
    client = Client(
        Options(system_prompt="x", name="main", permission_mode="bypassPermissions"),
        provider=provider,
        workspace_dir=ws,
        multi_turn=True,
    )
    try:
        task_id = client.start(goal="alpha").task_id
        client.send_goal(task_id, goal="beta")
        follow_up = provider.received_requests[1].messages
    finally:
        client.shutdown()
    assert not any(
        isinstance(block, ToolUseBlock)
        for message in follow_up
        for block in message.content
    )
    assert any(
        message.role == "assistant"
        and message.content == [TextBlock(text="reading")]
        for message in follow_up
    )


# ---------------------------------------------------------------------------
# B10: the provider's error text reaches FailDecision.detail
# ---------------------------------------------------------------------------


def test_provider_error_text_rides_on_detail() -> None:
    decision = _decide(
        LLMResponse(
            stop_reason="error",
            content=[],
            raw={"error": "HTTP 401: invalid x-api-key", "category": "fatal"},
        )
    )
    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_error"
    assert decision.detail == "HTTP 401: invalid x-api-key"


def test_error_without_provider_text_has_no_detail() -> None:
    decision = _decide(LLMResponse(stop_reason="error", content=[], raw={}))
    assert isinstance(decision, FailDecision)
    assert decision.detail is None


# ---------------------------------------------------------------------------
# B9: structured_output must be the only call in its response
# ---------------------------------------------------------------------------


_SCHEMA = {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}}}


class _Inner:
    def __init__(self, *names: str) -> None:
        self._names = names

    def decide(self, ctx, view):  # noqa: ANN001, ARG002
        calls = [
            ToolCall(
                tool_name=name,
                arguments={"title": "done"} if name == STRUCTURED_OUTPUT_TOOL else {},
                call_id=f"c{i}",
            )
            for i, name in enumerate(self._names)
        ]
        return ToolCallsDecision(
            calls=calls,
            assistant_message=Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        call_id=c.call_id, tool_name=c.tool_name, arguments=dict(c.arguments)
                    )
                    for c in calls
                ],
            ),
        )


def _wrapped(*names: str) -> StructuredOutputPolicy:
    return StructuredOutputPolicy(inner=_Inner(*names), schema=_SCHEMA)


def test_valid_structured_output_alone_still_finishes() -> None:
    decision = _wrapped(STRUCTURED_OUTPUT_TOOL).decide(
        SimpleNamespace(), SimpleNamespace(rolling_history=[])
    )
    assert isinstance(decision, FinishDecision)
    assert decision.answer == {"title": "done"}


def test_valid_structured_output_with_neighbours_is_rejected() -> None:
    decision = _wrapped(STRUCTURED_OUTPUT_TOOL, "Read").decide(
        SimpleNamespace(), SimpleNamespace(rolling_history=[])
    )
    assert isinstance(decision, StatePatchDecision)
    results = decision.messages_after[0].content
    assert sorted(r.call_id for r in results) == ["c0", "c1"]
    for result in results:
        assert result.success is False
        assert (result.error or "").startswith("Nothing in this response ran")
        assert "structured_output must be called on its own" in (result.error or "")


def test_repeated_mixed_calls_spend_the_budget_and_fail() -> None:
    policy = _wrapped(STRUCTURED_OUTPUT_TOOL, "Read")
    rejected_turn = Message(
        role="assistant",
        content=[ToolUseBlock(call_id="c0", tool_name=STRUCTURED_OUTPUT_TOOL, arguments={})],
    )
    spent = SimpleNamespace(rolling_history=[rejected_turn] * MAX_STRUCTURED_OUTPUT_NUDGES)
    decision = policy.decide(SimpleNamespace(), spent)
    assert isinstance(decision, FailDecision)
    assert "not called on its own" in decision.reason
