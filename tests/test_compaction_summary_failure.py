"""A failed or empty summarize response must NOT be recorded as a compaction.

Compaction is destructive by design: fold writes ``summary_ref`` and the
Composer's ``_apply_summary`` REPLACES the collapsed prefix with that single
message. So a summarize round-trip that comes back with an ``error``
stop_reason, a ``max_tokens`` truncation, or nothing but whitespace must be
detected and turned into ``FailDecision(compaction_summary_failed: …)`` —
recording it destroys the early intent and accumulated context for good, and
the empty text block 400s the very next request. The same goes for a reply
that is not a note at all: a model that ignored the instruction and narrated
its next step instead is refused by the section-title gate.

The setup mirrors ``test_compaction_boundary_alignment``: a real
``ThreeSegmentComposer`` + a history large enough to trip the proactive trigger,
so the summarize call is the first — and only — LLM round-trip of the step.
"""

from __future__ import annotations

import threading

from noeta.context.composer import RenderedContent, ThreeSegmentComposer
from noeta.builtins.react.impl import ReActPolicy
from noeta.builtins.react.impl.react import (
    _SUMMARIZE_PROMPT,
    _SUMMARY_SECTION_TITLES,
    SUMMARY_FAILED_REASON,
    looks_like_summary_note,
)
from noeta.protocols.decisions import CompactionRequestedDecision, FailDecision
from noeta.protocols.messages import LLMResponse, Message, TextBlock, Usage
from noeta.protocols.step_context import StepContext
from noeta.protocols.task import Task
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider


#: The smallest reply the note gate accepts: two of the prompt's section
#: titles. Everything a real note carries beyond that is irrelevant here.
_NOTE = (
    "1. Primary Request & Intent: keep working the task.\n"
    "6. Pending Tasks: finish it."
)


def _ctx() -> StepContext:
    return StepContext(task_id="t-1", lease_id="l-1", trace_id="tr-1")


def _skill_renderer(_: list[str], __=None) -> RenderedContent:
    return RenderedContent(
        messages=[
            Message(role="user", content=[TextBlock(text="SKILL-BODY" * 20)])
        ],
        selected_skills=["fake-skill"],
    )


def _runtime_messages(n: int) -> list[Message]:
    return [
        Message(role="user", content=[TextBlock(text=f"m{i}-" + "x" * 400)])
        for i in range(n)
    ]


def _drive(summarize_response: LLMResponse):
    """Compose a proactive-compaction-tripping view and drive one decide().

    Returns ``(decision, provider)`` — ``provider.received_requests`` proves the
    summarize round-trip actually happened."""
    store = InMemoryContentStore()
    log = InMemoryEventLog()
    composer = ThreeSegmentComposer(
        system_prompt="sys",
        tools={},
        content_store=store,
        skill_renderer=_skill_renderer,
        tail_token_budget=300,
    )
    task = Task(task_id="t-1")
    task.runtime.messages = _runtime_messages(12)
    view = composer.compose(task)

    provider = FakeLLMProvider(responses=[summarize_response])
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=store
    )
    policy = ReActPolicy(
        llm=client,
        tools={},
        system_prompt="sys",
        model="gpt-4o",
        context_window=600,
        max_output_tokens=50,
        compaction_buffer=50,
        tail_token_budget=200,
        composer_version="three_segment.v3",
    )
    return policy.decide(_ctx(), view), provider


def test_errored_summarize_fails_cleanly_without_recording_compaction() -> None:
    decision, provider = _drive(LLMResponse(stop_reason="error", content=[]))
    # The proactive trigger fired — the summarize call was made ...
    assert len(provider.received_requests) == 1
    # ... but the failed summarize is NOT turned into an empty compaction.
    assert isinstance(decision, FailDecision)
    assert decision.reason.startswith(f"{SUMMARY_FAILED_REASON}: ")


def test_empty_summary_fails_cleanly_without_recording_compaction() -> None:
    # A "successful" stop_reason but no usable text (whitespace only / a model
    # that emitted only a thinking block) is just as destructive.
    decision, provider = _drive(
        LLMResponse(stop_reason="end_turn", content=[TextBlock(text="   ")])
    )
    assert len(provider.received_requests) == 1
    assert isinstance(decision, FailDecision)
    assert decision.reason.startswith(f"{SUMMARY_FAILED_REASON}: ")


def test_reasoning_model_maxtokens_truncation_fails_cleanly() -> None:
    # A reasoning model on a gateway that caps output when the client sends no
    # ``max_tokens`` can spend its whole default budget on hidden reasoning and
    # return ``stop_reason="max_tokens"`` with no text block — a "successful"
    # response carrying nothing. The guard must fail cleanly here too.
    decision, provider = _drive(LLMResponse(stop_reason="max_tokens", content=[]))
    assert len(provider.received_requests) == 1
    assert isinstance(decision, FailDecision)
    assert decision.reason.startswith(f"{SUMMARY_FAILED_REASON}: ")


def test_summarize_request_forwards_output_ceiling() -> None:
    # Root-cause guard for the fix: the summarize round-trip must carry the
    # model's output ceiling (``max_output_tokens``) the same as a normal turn.
    # Without it a gateway that caps output when no ``max_tokens`` is sent
    # truncates a reasoning model's summary into the empty ``max_tokens`` body
    # above, so every proactive compaction dies as ``compaction_summary_failed``.
    _, provider = _drive(
        LLMResponse(
            stop_reason="end_turn", content=[TextBlock(text=_NOTE)]
        )
    )
    assert len(provider.received_requests) == 1
    # ``_drive`` builds the policy with ``max_output_tokens=50``.
    assert provider.received_requests[0].max_tokens == 50


def test_nonempty_summary_still_compacts() -> None:
    # Guard the happy path: a real summary is still recorded as a compaction.
    decision, _ = _drive(
        LLMResponse(
            stop_reason="end_turn", content=[TextBlock(text=_NOTE)]
        )
    )
    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.summary == _NOTE


# ---------------------------------------------------------------------------
# Interrupt mid-summarize (interrupt-responsiveness D9)
# ---------------------------------------------------------------------------


class _BlockedSummarizeProvider:
    """Blocks inside ``complete`` until released — a summarize call in flight."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.finished = threading.Event()

    def complete(self, request):  # noqa: ANN001 - protocol shape
        self.entered.set()
        assert self.release.wait(timeout=10.0), "test forgot to release"
        self.finished.set()
        return LLMResponse(
            stop_reason="end_turn", content=[TextBlock(text="late note")]
        )


def test_interrupt_mid_summarize_aborts_without_a_compaction() -> None:
    """D9 pin: the summarize call goes through ``RuntimeLLMClient.complete``
    with the step ctx, so the abandonable wait already covers it — no extra
    wiring. A stop flipping ``ctx.cancelled`` while the summarize provider
    call is blocked makes ``decide()`` return promptly (provider still open),
    the aborted response becomes ``FailDecision(compaction_summary_failed)``
    — never a ``CompactionRequestedDecision``, so no ``Compacted`` can be
    recorded from the abandoned summary — and at engine level the post-decide
    cancel poll then raises before ANY decision is acted on."""
    store = InMemoryContentStore()
    log = InMemoryEventLog()
    composer = ThreeSegmentComposer(
        system_prompt="sys",
        tools={},
        content_store=store,
        skill_renderer=_skill_renderer,
        tail_token_budget=300,
    )
    task = Task(task_id="t-1")
    task.runtime.messages = _runtime_messages(12)
    view = composer.compose(task)

    provider = _BlockedSummarizeProvider()
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=store,
        abandon_poll_seconds=0.01,
    )
    policy = ReActPolicy(
        llm=client,
        tools={},
        system_prompt="sys",
        model="gpt-4o",
        context_window=600,
        max_output_tokens=50,
        compaction_buffer=50,
        tail_token_budget=200,
        composer_version="three_segment.v3",
    )
    flag = threading.Event()
    ctx = StepContext(
        task_id="t-1", lease_id="l-1", trace_id="tr-1",
        cancelled=flag.is_set,
    )

    result: list[object] = []
    step = threading.Thread(
        target=lambda: result.append(policy.decide(ctx, view))
    )
    step.start()
    assert provider.entered.wait(timeout=5.0)  # summarize call in flight
    flag.set()  # the human stop lands
    step.join(timeout=5.0)
    assert not step.is_alive(), "decide() must return without the provider"

    # The provider is STILL blocked — the wait was abandoned, not completed.
    assert not provider.finished.is_set()

    # The aborted summarize is a clean failure, never a compaction decision.
    (decision,) = result
    assert isinstance(decision, FailDecision)
    assert decision.reason.startswith(f"{SUMMARY_FAILED_REASON}: ")

    # Only the recorded trio reached the stream — nothing compaction-shaped —
    # and it records the abort (errored response, unsuccessful round-trip).
    events = log.read("t-1")
    assert [e.type for e in events] == [
        "LLMRequestStarted",
        "LLMResponseRecorded",
        "LLMRequestFinished",
    ]
    assert events[1].payload.stop_reason == "error"
    assert events[2].payload.success is False

    # Releasing the orphan later adds nothing to the stream.
    provider.release.set()
    assert provider.finished.wait(timeout=5.0)
    assert len(log.read("t-1")) == 3


# ---------------------------------------------------------------------------
# The note gate and the failure reason's facts
# ---------------------------------------------------------------------------

#: Verbatim what a relay-served model returned to the system-only summarize
#: request in production — its next step, not a note. Recorded as the note,
#: it replaced the whole collapsed prefix.
_NARRATION = "The file is long and got cut off. Fetching the rest."


def test_narration_instead_of_note_is_refused_and_quoted() -> None:
    decision, provider = _drive(
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text=_NARRATION)],
            usage=Usage(uncached=68_336, output=79),
        )
    )
    assert len(provider.received_requests) == 1
    assert isinstance(decision, FailDecision)
    assert decision.retryable is False
    assert decision.reason.startswith(f"{SUMMARY_FAILED_REASON}: ")
    # The receipt says WHAT came back, not just that it was refused.
    assert "not a note" in decision.reason
    assert "stop_reason=end_turn" in decision.reason
    assert "output_tokens=79" in decision.reason
    assert "Fetching the rest." in decision.reason


def test_empty_reason_names_stop_reason_output_tokens_and_blocks() -> None:
    # Tokens billed, no text: the block list is what tells "only thinking"
    # from "nothing at all" in the receipt.
    decision, _ = _drive(
        LLMResponse(
            stop_reason="end_turn", content=[], usage=Usage(output=2098)
        )
    )
    assert isinstance(decision, FailDecision)
    assert "empty summarize response" in decision.reason
    assert "stop_reason=end_turn" in decision.reason
    assert "output_tokens=2098" in decision.reason
    assert "blocks=none" in decision.reason

    decision, _ = _drive(
        LLMResponse(stop_reason="max_tokens", content=[TextBlock(text="  ")])
    )
    assert isinstance(decision, FailDecision)
    assert "stop_reason=max_tokens" in decision.reason
    assert "blocks=TextBlock" in decision.reason


def test_errored_reason_names_category_and_error() -> None:
    decision, _ = _drive(
        LLMResponse(
            stop_reason="error",
            content=[],
            raw={"category": "overflow", "error": "prompt is too long"},
        )
    )
    assert isinstance(decision, FailDecision)
    assert "errored summarize round-trip" in decision.reason
    assert "category=overflow" in decision.reason
    assert "prompt is too long" in decision.reason


def test_failure_reason_stays_one_short_line() -> None:
    # The reason rides an inline event payload (4 KB ceiling) and a host
    # renders it as one line: a long narration is excerpted, never dumped.
    decision, _ = _drive(
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="word " * 2_000)],
        )
    )
    assert isinstance(decision, FailDecision)
    assert len(decision.reason) < 400
    assert "\n" not in decision.reason


def test_summarize_request_carries_instruction_as_trailing_user_turn() -> None:
    """The instruction rides twice: the request's ``system`` (unchanged, so
    the summarize call stays recognisable by its system text) and a trailing
    ``user`` turn after the history — the placement a model that weighs the
    conversation's momentum over ``system`` still answers with the note."""
    _, provider = _drive(
        LLMResponse(stop_reason="end_turn", content=[TextBlock(text=_NOTE)])
    )
    (request,) = provider.received_requests
    system_text = "".join(
        b.text for b in request.system.content if isinstance(b, TextBlock)
    )
    assert system_text == _SUMMARIZE_PROMPT
    last = request.messages[-1]
    assert last.role == "user"
    assert last.origin is None  # a plain user turn, not a host injection
    assert [b.text for b in last.content if isinstance(b, TextBlock)] == [
        _SUMMARIZE_PROMPT
    ]
    # Only the trailing turn is synthetic: everything before it is history.
    assert all(
        _SUMMARIZE_PROMPT not in b.text
        for m in request.messages[:-1]
        for b in m.content
        if isinstance(b, TextBlock)
    )


def test_note_gate_counts_the_prompt_section_titles() -> None:
    # The gate and the prompt cannot drift: every title it counts is one the
    # prompt asks for.
    for title in _SUMMARY_SECTION_TITLES:
        assert title in _SUMMARIZE_PROMPT
    # A real note (the head of one a relay-served model produced once the
    # instruction rode as the trailing user turn): markdown-decorated, mostly
    # Chinese, English headings.
    real = (
        "# 调查笔记：BotMux 的「飞书开放平台权限申请」自动化\n\n"
        "## 1. Primary Request & Intent\n\n用户要求对工作区里的源码做只读调查。\n\n"
        "## 2. Key Technical Concepts\n\n- Bun / TypeScript\n"
    )
    assert looks_like_summary_note(real)
    assert looks_like_summary_note(_NOTE)
    assert looks_like_summary_note(_NOTE.upper())
    # One title in passing is prose, not a note.
    assert not looks_like_summary_note(
        "The user's primary request & intent was a refactor; carrying on."
    )
    assert not looks_like_summary_note(_NARRATION)
    assert not looks_like_summary_note("")
