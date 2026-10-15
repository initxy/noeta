"""ReAct policy edges: what a provider failure tells the host, and a
``structured_output`` sent next to other calls.
"""

from __future__ import annotations

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
    ToolUseBlock,
)
from noeta.protocols.step_context import StepContext
from noeta.runtime.llm import RuntimeLLMClient
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
