"""The summarize round-trip is sent without ``tool_choice``; a tool-call (or
text-less) answer is retried exactly once with ``tool_choice: none``.

On Anthropic a changed ``tool_choice`` invalidates the message-layer cache
entry, so the first attempt leaves it out and relies on the prompt's closing
no-tools instruction. Only when the model answers with a tool call anyway does
the policy retry with the wire-level opt-out. The retry is an ordinary
recorded round-trip, and whatever it returns goes through the existing
empty-summary / note-shape guards. See ``docs/adr/context-compaction.md``
(Amended 2026-09-25 — summarize without ``tool_choice``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.builtins.react.impl import ReActPolicy
from noeta.builtins.react.impl.react import SUMMARY_FAILED_REASON
from noeta.client import Client, Options
from noeta.protocols.decisions import CompactionRequestedDecision, FailDecision
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.runtime.llm import RuntimeLLMClient
from noeta.sdk.testing import FakeLLMProvider
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.composer import fake_view
from noeta.tools.fake import FakeTool

#: A reply the note gate accepts — two of the prompt's section titles.
_NOTE = (
    "1. Primary Request & Intent: condensed summary of the conversation.\n"
    "6. Pending Tasks: none."
)

_TRIO = ["LLMRequestStarted", "LLMResponseRecorded", "LLMRequestFinished"]


def _ctx() -> StepContext:
    return StepContext(task_id="t-1", lease_id="l-1", trace_id="tr-1")


def _big_view() -> Any:
    return fake_view(
        [
            Message(role="user", content=[TextBlock(text="x" * 200)])
            for _ in range(40)
        ]
    )


def _note() -> LLMResponse:
    return LLMResponse(stop_reason="end_turn", content=[TextBlock(text=_NOTE)])


def _tool_call() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id="c-1", tool_name="echo", arguments={"x": "hi"})
        ],
    )


def _decide(
    responses: list[LLMResponse],
) -> tuple[Any, FakeLLMProvider, InMemoryEventLog]:
    provider = FakeLLMProvider(responses=responses)
    log = InMemoryEventLog()
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=InMemoryContentStore()
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
    return policy.decide(_ctx(), _big_view()), provider, log


def test_first_attempt_has_no_tool_choice_and_a_note_is_not_retried() -> None:
    decision, provider, log = _decide([_note()])

    assert isinstance(decision, CompactionRequestedDecision)
    (request,) = provider.received_requests
    assert "tool_choice" not in request.metadata
    assert [e.type for e in log.read("t-1")] == _TRIO


def test_tool_call_answer_is_retried_once_with_tool_choice_none() -> None:
    decision, provider, log = _decide([_tool_call(), _note()])

    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.summary.startswith("1. Primary Request & Intent")
    first, retry = provider.received_requests
    assert "tool_choice" not in first.metadata
    assert retry.metadata == {"tool_choice": "none"}
    # Apart from the opt-out, the retry is the same request.
    assert retry.messages == first.messages
    assert retry.tools == first.tools
    assert retry.system == first.system
    assert retry.model == first.model
    # Both attempts are ordinary recorded round-trips.
    assert [e.type for e in log.read("t-1")] == _TRIO + _TRIO


def test_text_less_answer_is_retried_once() -> None:
    empty = LLMResponse(stop_reason="end_turn", content=[TextBlock(text="  ")])
    decision, provider, _ = _decide([empty, _note()])

    assert isinstance(decision, CompactionRequestedDecision)
    assert provider.received_requests[1].metadata == {"tool_choice": "none"}


def test_errored_and_narrated_answers_are_not_retried() -> None:
    errored = LLMResponse(stop_reason="error", content=[])
    narrated = LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="Next I will read the config file.")],
    )
    for response in (errored, narrated):
        decision, provider, _ = _decide([response])
        assert isinstance(decision, FailDecision)
        assert decision.reason.startswith(SUMMARY_FAILED_REASON)
        assert len(provider.received_requests) == 1


def test_tool_call_on_the_retry_too_falls_into_the_guard() -> None:
    # Exactly one retry: a third scripted response would be an IndexError.
    decision, provider, log = _decide([_tool_call(), _tool_call()])

    assert isinstance(decision, FailDecision)
    assert decision.retryable is False
    assert decision.reason.startswith(SUMMARY_FAILED_REASON)
    assert "ToolUseBlock" in decision.reason
    assert len(provider.received_requests) == 2
    assert [e.type for e in log.read("t-1")] == _TRIO + _TRIO


def _summarize_call(req: LLMRequest) -> bool:
    if not req.messages or not req.messages[-1].content:
        return False
    return getattr(req.messages[-1].content[0], "text", "").startswith(
        "Summarize the conversation"
    )


def _drive_client(
    tmp_path: Path, summarize: Any
) -> tuple[FakeLLMProvider, list[LLMRequest], str]:
    """Drive a real ``Client`` until a summarize round-trip happens (the
    harness of ``test_compaction_model_bridges_options_to_the_summarize_request``)
    and return the provider, the summarize requests and the last turn's
    status."""

    def _responder(req: LLMRequest) -> LLMResponse:
        if _summarize_call(req):
            response: LLMResponse = summarize(req)
            return response
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "end"},
        )

    provider = FakeLLMProvider(responder=_responder)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    client = Client(
        Options(system_prompt="test agent", name="main"),
        provider=provider,
        workspace_dir=workspace,
        model="gpt-test",
    )
    try:
        filler = "lorem ipsum dolor sit amet " * 1800
        out = client.start(goal=filler)
        for _ in range(24):
            if any(_summarize_call(r) for r in provider.received_requests):
                break
            out = client.send_goal(out.task_id, goal=filler)
        # One more turn proves the task survived the summarize step.
        out = client.send_goal(out.task_id, goal="still there?")
        calls = [r for r in provider.received_requests if _summarize_call(r)]
        return provider, calls, str(out.status)
    finally:
        client.shutdown()


def test_client_tool_call_summarize_is_retried(
    tmp_path: Path,
) -> None:
    def _summarize(req: LLMRequest) -> LLMResponse:
        if req.metadata.get("tool_choice") == "none":
            return LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text=_NOTE)],
                usage=Usage(uncached=1, output=1),
                raw={"id": "note"},
            )
        return LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(call_id="c-1", tool_name="Read", arguments={})
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "tool"},
        )

    _, calls, status = _drive_client(tmp_path, _summarize)

    assert calls, "no summarize round-trip observed"
    assert status == "suspended"
    assert "tool_choice" not in calls[0].metadata
    assert calls[1].metadata == {"tool_choice": "none"}
    # Every summarize step is exactly one tool-choice-free attempt followed
    # by exactly one retry.
    assert [c.metadata.get("tool_choice") for c in calls] == [
        None,
        "none",
    ] * (len(calls) // 2)


def test_client_task_survives_a_retry_that_still_calls_a_tool(
    tmp_path: Path,
) -> None:
    def _summarize(req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(call_id="c-1", tool_name="Read", arguments={})
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "tool"},
        )

    _, calls, status = _drive_client(tmp_path, _summarize)

    # Each summarize step makes exactly two attempts — never a third — and
    # the guard fails that step as before: the task is parked for its next
    # goal, not failed.
    assert calls, "no summarize round-trip observed"
    assert [c.metadata.get("tool_choice") for c in calls] == [
        None,
        "none",
    ] * (len(calls) // 2)
    assert len(calls) % 2 == 0
    assert status == "suspended"
