"""Output-limit recovery: a response the output cap cuts does not end the
task. Its complete tool calls run; a cut text keeps its partial content and
the turn continues with Claude Code's resume nudge, three times in a row at
most; a cut that produced only thinking records no assistant turn and gets
the "nothing to resume" nudge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.builtins.react.impl.react import (
    MAX_OUTPUT_TOKENS_RECOVERY,
    OUTPUT_LIMIT_RESUME_NUDGE,
    OUTPUT_LIMIT_THINKING_NUDGE,
    ReActPolicy,
)
from noeta.protocols.decisions import (
    FailDecision,
    FinishDecision,
    StatePatchDecision,
    ToolCallsDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.runtime.llm import RuntimeLLMClient
from noeta.sdk import Options, query
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.composer import fake_view
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool


def _decide(
    response: LLMResponse,
    history: list[Message] | None = None,
    *,
    output_schema: dict[str, object] | None = None,
):
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
        output_schema=output_schema,
    )
    ctx = StepContext(task_id="task-1", lease_id="lease-1", trace_id="trace-1")
    return policy.decide(ctx, fake_view(history or []))


def _cut(*blocks) -> LLMResponse:  # noqa: ANN002
    return LLMResponse(
        stop_reason="max_tokens",
        content=list(blocks),
        usage=Usage(uncached=1, output=1),
    )


def _user(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def _assistant(text: str) -> Message:
    return Message(role="assistant", content=[TextBlock(text=text)])


def test_nudge_texts_are_claude_codes() -> None:
    assert OUTPUT_LIMIT_RESUME_NUDGE == (
        "Output token limit hit. Resume directly — no apology, no recap of "
        "what you were doing. Pick up mid-thought if that is where the cut "
        "happened. Break remaining work into smaller pieces."
    )
    assert OUTPUT_LIMIT_THINKING_NUDGE.startswith(
        "Output token limit hit while you were still reasoning"
    )
    assert MAX_OUTPUT_TOKENS_RECOVERY == 3


def test_cut_text_is_kept_and_the_resume_nudge_follows() -> None:
    decision = _decide(_cut(ThinkingBlock(text="hm"), TextBlock(text="half a")))
    assert isinstance(decision, StatePatchDecision)
    assert decision.messages_before == (_assistant("half a"),)
    assert decision.messages_after == (_user(OUTPUT_LIMIT_RESUME_NUDGE),)


def test_cut_thinking_records_only_the_thinking_nudge() -> None:
    decision = _decide(_cut(ThinkingBlock(text="still thinking")))
    assert isinstance(decision, StatePatchDecision)
    assert decision.messages_before == ()
    assert decision.messages_after == (_user(OUTPUT_LIMIT_THINKING_NUDGE),)


def test_complete_tool_calls_of_a_cut_response_run() -> None:
    decision = _decide(
        _cut(
            TextBlock(text="reading"),
            ToolUseBlock(call_id="t1", tool_name="echo", arguments={"x": "hi"}),
        )
    )
    assert isinstance(decision, ToolCallsDecision)
    assert [c.call_id for c in decision.calls] == ["t1"]


def _recovered(n: int) -> list[Message]:
    history = [_user("write it")]
    for i in range(n):
        history += [_assistant(f"part {i}"), _user(OUTPUT_LIMIT_RESUME_NUDGE)]
    return history


def test_fourth_cut_in_a_row_fails_and_keeps_its_text() -> None:
    decision = _decide(_cut(TextBlock(text="part 3")), _recovered(3))
    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_truncated"
    assert decision.assistant_message == _assistant("part 3")


def test_fourth_thinking_only_cut_fails_with_nothing_recorded() -> None:
    decision = _decide(_cut(ThinkingBlock(text="x")), _recovered(3))
    assert isinstance(decision, FailDecision)
    assert decision.reason == "llm_empty_response"
    assert decision.assistant_message is None


@pytest.mark.parametrize(
    "breaker",
    [
        _user("a new request"),
        Message(
            role="tool",
            content=[ToolResultBlock(call_id="t1", output="ok", success=True)],
        ),
    ],
)
def test_a_tool_round_or_user_message_restarts_the_count(breaker: Message) -> None:
    history = _recovered(3)
    history.insert(len(history) - 2, breaker)
    decision = _decide(_cut(TextBlock(text="more")), history)
    assert isinstance(decision, StatePatchDecision)


def test_a_cut_answer_continues_to_completion(tmp_path: Path) -> None:
    provider = FakeLLMProvider(
        responses=[
            _cut(TextBlock(text="first half")),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="second half")],
                usage=Usage(uncached=1, output=1),
            ),
        ]
    )
    result = query(
        Options(system_prompt="x", name="main", permission_mode="bypassPermissions"),
        goal="write",
        provider=provider,
        workspace_dir=tmp_path,
    )
    assert result.answer() == "first halfsecond half"
    follow_up = provider.received_requests[1].messages
    assert follow_up[-2] == _assistant("first half")
    assert follow_up[-1].role == "user"
    assert follow_up[-1].content == [TextBlock(text=OUTPUT_LIMIT_RESUME_NUDGE)]


def _done(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def test_the_answer_joins_every_continued_piece() -> None:
    """The final answer is the whole continued text, not its last piece; a
    thinking-only cut in the run contributes nothing and breaks nothing."""
    history = [
        _user("write it"),
        _assistant("PART-A "),
        _user(OUTPUT_LIMIT_RESUME_NUDGE),
        _user(OUTPUT_LIMIT_THINKING_NUDGE),
        _assistant("PART-B "),
        _user(OUTPUT_LIMIT_RESUME_NUDGE),
    ]
    decision = _decide(_done("PART-C"), history)
    assert isinstance(decision, FinishDecision)
    assert decision.answer == "PART-A PART-B PART-C"
    assert decision.assistant_message == _assistant("PART-C")


def test_the_output_schema_parses_the_joined_text() -> None:
    history = [
        _user("json please"),
        _assistant('{"a": 1, '),
        _user(OUTPUT_LIMIT_RESUME_NUDGE),
    ]
    decision = _decide(
        _done('"b": 2}'), history, output_schema={"type": "object"}
    )
    assert isinstance(decision, FinishDecision)
    assert decision.answer == {"a": 1, "b": 2}


def test_an_answer_after_a_tool_round_joins_nothing() -> None:
    history = [
        _user("go"),
        _assistant("earlier answer"),
        _user(OUTPUT_LIMIT_RESUME_NUDGE),
        Message(
            role="assistant",
            content=[ToolUseBlock(call_id="t1", tool_name="echo", arguments={})],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(call_id="t1", output="ok", success=True)],
        ),
    ]
    decision = _decide(_done("final"), history)
    assert isinstance(decision, FinishDecision)
    assert decision.answer == "final"
