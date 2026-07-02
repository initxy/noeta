"""P1-2 — a failed/empty summarize response must NOT be recorded as a compaction.

The bug: ``ReActPolicy._compaction_decision`` built ``summary`` from the
summarize response's text blocks and emitted a ``CompactionRequestedDecision``
unconditionally. When the summarize round-trip returned an ``error`` stop_reason
(the LLM client's transient retries already exhausted, or a fatal error) OR came
back with no text, ``summary`` was ``""``; fold then set ``summary_ref`` to the
empty note and the Composer's ``_apply_summary`` REPLACED the whole collapsed
prefix with a single empty ``user`` message — the early intent + accumulated
context were destroyed, and the empty text block 400'd the very next request.

The fix: detect the failed/empty summarize and return a clean
``FailDecision(compaction_summary_failed)``, leaving the durable history intact.

The setup mirrors ``test_compaction_boundary_alignment`` (a real
``ThreeSegmentComposer`` + a history large enough to trip the proactive trigger,
so the summarize call is the first — and only — LLM round-trip of the step).
"""

from __future__ import annotations

from noeta.context.composer import RenderedSkills, ThreeSegmentComposer
from noeta.policies.react import ReActPolicy
from noeta.protocols.decisions import CompactionRequestedDecision, FailDecision
from noeta.protocols.messages import LLMResponse, Message, TextBlock
from noeta.protocols.step_context import StepContext
from noeta.protocols.task import Task
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider


def _ctx() -> StepContext:
    return StepContext(task_id="t-1", lease_id="l-1", trace_id="tr-1")


def _skill_renderer(_: list[str]) -> RenderedSkills:
    return RenderedSkills(
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
    assert decision.reason == "compaction_summary_failed"


def test_empty_summary_fails_cleanly_without_recording_compaction() -> None:
    # A "successful" stop_reason but no usable text (whitespace only / a model
    # that emitted only a thinking block) is just as destructive.
    decision, provider = _drive(
        LLMResponse(stop_reason="end_turn", content=[TextBlock(text="   ")])
    )
    assert len(provider.received_requests) == 1
    assert isinstance(decision, FailDecision)
    assert decision.reason == "compaction_summary_failed"


def test_nonempty_summary_still_compacts() -> None:
    # Guard the happy path: a real summary is still recorded as a compaction.
    decision, _ = _drive(
        LLMResponse(
            stop_reason="end_turn", content=[TextBlock(text="real note")]
        )
    )
    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.summary == "real note"
