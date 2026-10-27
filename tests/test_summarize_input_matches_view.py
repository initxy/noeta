"""The summarize request carries the main loop's view of the history, and a
summarize request that still overflows is retried on a shorter input.

The raw history keeps every tool output; the composer clears old ones long
before the summarize trigger fires. A summarize input built from the raw
history brought every cleared output back — a request far past the window the
main loop never sent, which died as ``compaction_summary_failed``. The input
now shows each result the composed View has cleared as cleared, and an
overflow on the summarize call drops the oldest fifth of the rounds, up to
three times, behind a truncation line (Claude Code's behavior).
"""

from __future__ import annotations

from typing import Any

from noeta.builtins.react.impl import ReActPolicy
from noeta.builtins.react.impl.react import (
    MAX_SUMMARY_OVERFLOW_RETRIES,
    SUMMARY_FAILED_REASON,
)
from noeta.context.composer import ThreeSegmentComposer
from noeta.protocols.decisions import CompactionRequestedDecision, FailDecision
from noeta.protocols.errors import CATEGORY_OVERFLOW
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.task import Task, TaskState
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.protocols.view import View
from noeta.runtime.llm import RuntimeLLMClient
from noeta.sdk.testing import FakeLLMProvider
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog

_MARKER = "[Old tool result content cleared]"
_TRUNCATED = "[earlier conversation truncated for compaction retry]"
_NOTE = (
    "1. Primary Request & Intent: condensed summary of the conversation.\n"
    "6. Pending Tasks: none."
)


class _Read:
    name = "Read"
    description = "read"
    risk_level = "low"
    input_schema: dict[str, Any] = {"type": "object"}
    clearable = True

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(success=True, output="")  # pragma: no cover


def _history(rounds: int) -> list[Message]:
    msgs = [Message(role="user", content=[TextBlock(text="read everything")])]
    for i in range(rounds):
        msgs += [
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        call_id=f"r{i}", tool_name="Read", arguments={"n": i}
                    )
                ],
            ),
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        call_id=f"r{i}", output=f"{i:02d}" * 2_000, success=True
                    )
                ],
            ),
        ]
    return msgs


def _view(rounds: int = 12) -> View:
    """A composed View whose clearing valve has cleared all but the newest
    five results (1,000 estimated tokens each)."""
    task = Task(task_id="t-1", state=TaskState())
    task.runtime.messages.extend(_history(rounds))
    composer = ThreeSegmentComposer(
        system_prompt="sys",
        tools={"Read": _Read()},  # type: ignore[dict-item]
        content_store=InMemoryContentStore(),
        available_window=10,
        microcompact_keep_recent=5,
        microcompact_batch_tokens=1,
    )
    return composer.compose(task)


def _decide(responses: list[LLMResponse]) -> tuple[Any, FakeLLMProvider]:
    provider = FakeLLMProvider(responses=responses)
    client = RuntimeLLMClient(
        provider=provider,
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
    )
    policy = ReActPolicy(
        llm=client,
        tools={"Read": _Read()},  # type: ignore[dict-item]
        system_prompt="sys",
        model="gpt-4o",
        context_window=4_000,
        max_output_tokens=500,
        compaction_buffer=100,
        tail_token_budget=1_200,
        composer_version="three_segment.v5",
    )
    ctx = StepContext(task_id="t-1", lease_id="l-1", trace_id="tr-1")
    return policy.decide(ctx, _view()), provider


def _results(request: LLMRequest) -> dict[str, Any]:
    return {
        b.call_id: b.output
        for m in request.messages
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }


def _overflow() -> LLMResponse:
    return LLMResponse(
        stop_reason="error",
        content=[],
        raw={"category": CATEGORY_OVERFLOW, "error": "prompt is too long"},
    )


def _note() -> LLMResponse:
    return LLMResponse(stop_reason="end_turn", content=[TextBlock(text=_NOTE)])


def test_the_summarize_input_shows_cleared_results_as_cleared() -> None:
    view = _view()
    cleared_in_view = {
        b.call_id
        for m in view.segments[2].content
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.output == _MARKER
    }
    assert cleared_in_view == {f"r{i}" for i in range(7)}

    decision, provider = _decide([_note()])

    assert isinstance(decision, CompactionRequestedDecision)
    (request,) = provider.received_requests
    sent = _results(request)
    assert sent  # the boundary covers some rounds
    for call_id, output in sent.items():
        if call_id in cleared_in_view:
            assert output == _MARKER
        else:
            assert output != _MARKER
    assert any(output == _MARKER for output in sent.values())


def _paired(request: LLMRequest) -> bool:
    """Every tool result in the request follows its tool call."""
    seen: set[str] = set()
    for m in request.messages:
        for b in m.content:
            if isinstance(b, ToolUseBlock):
                seen.add(b.call_id)
            if isinstance(b, ToolResultBlock) and b.call_id not in seen:
                return False
    return True


def test_an_overflowing_summarize_retries_on_fewer_rounds() -> None:
    decision, provider = _decide([_overflow(), _overflow(), _note()])

    assert isinstance(decision, CompactionRequestedDecision)
    first, second, third = provider.received_requests
    assert len(third.messages) < len(second.messages) < len(first.messages)
    for retry in (second, third):
        head = retry.messages[0]
        assert head.role == "user" and head.content == [TextBlock(text=_TRUNCATED)]
        assert sum(
            1
            for m in retry.messages
            if m.content == [TextBlock(text=_TRUNCATED)]
        ) == 1
        assert _paired(retry)
    # Everything after the marker is the original input's tail.
    assert third.messages[1:] == first.messages[
        len(first.messages) - (len(third.messages) - 1) :
    ]


def test_the_overflow_retries_stop_at_three() -> None:
    decision, provider = _decide(
        [_overflow() for _ in range(MAX_SUMMARY_OVERFLOW_RETRIES + 1)]
    )

    assert isinstance(decision, FailDecision)
    assert decision.reason.startswith(SUMMARY_FAILED_REASON)
    assert "category=overflow" in decision.reason
    assert len(provider.received_requests) == MAX_SUMMARY_OVERFLOW_RETRIES + 1
