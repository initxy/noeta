"""Finding 2 — policy boundary ↔ composer slice index-space alignment.

The bug: ``ReActPolicy`` computed its summarise ``boundary`` over
``view.iter_messages()`` (``semi_stable + dynamic_suffix``, post-summary,
post-prune, tail-truncated) while ``ThreeSegmentComposer._apply_summary``
slices the RAW ``task.runtime.messages``. When ``semi_stable`` is non-empty
(skills active) or a prior summary already collapsed a prefix, the two indices
point at *different* messages → the wrong slice gets summarised. The old unit
tests hid this because ``fake_view`` put every message in ``dynamic_suffix``
with an empty ``semi_stable`` and no prior summary, making the two lists equal.

The fix exposes ``view.rolling_history`` (raw ``task.runtime.messages``) +
``view.summary_boundary`` so the policy computes the boundary in the SAME
coordinate the composer slices. These tests use a REAL ``ThreeSegmentComposer``
with a non-empty skill renderer AND a prior summary, then assert the
policy-computed boundary indexes into ``task.runtime.messages`` and the
composer applies it to exactly those messages.
"""

from __future__ import annotations

from typing import Any

from noeta.context.composer import ThreeSegmentComposer, RenderedSkills
from noeta.policies.react import ReActPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import CompactionRequestedDecision
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.task import Task
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider


def _ctx() -> StepContext:
    return StepContext(task_id="t-1", lease_id="l-1", trace_id="tr-1")


def _skill_renderer(_: list[str]) -> RenderedSkills:
    # A non-empty semi_stable segment: this is the prefix that made
    # iter_messages() diverge from the raw runtime history.
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


def _policy(client: RuntimeLLMClient) -> ReActPolicy:
    # Window deliberately tiny so even the modest synthetic history trips the
    # proactive estimate; tail_token_budget protects a couple of recent turns.
    return ReActPolicy(
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


def test_boundary_indexes_raw_runtime_not_view_projection() -> None:
    """With a non-empty semi_stable AND a prior summary, the boundary the
    policy records must be a RAW ``task.runtime.messages`` index — the exact
    coordinate the composer's ``_apply_summary`` slices. We prove it by
    checking the composer, fed that boundary as ``summary_boundary``, drops
    precisely ``task.runtime.messages[:boundary]``."""
    store = InMemoryContentStore()
    log = InMemoryEventLog()
    composer = ThreeSegmentComposer(
        system_prompt="sys",
        tools={},
        content_store=store,
        skill_renderer=_skill_renderer,
        tail_token_budget=300,
    )

    raw = _runtime_messages(12)
    task = Task(task_id="t-1")
    task.runtime.messages = list(raw)

    # Prior summary already collapsed the first 4 raw messages — this is the
    # condition that desynchronised iter_messages() from rolling_history.
    prior_summary_ref = store.put(
        to_canonical_bytes("earlier summary"), media_type="application/json"
    )
    task.context.summary_ref = prior_summary_ref
    task.context.summary_boundary = 4

    view = composer.compose(task)

    # Sanity: iter_messages() (semi + post-summary dynamic) is a DIFFERENT
    # list/length from the raw runtime history. If these were equal the test
    # would not be exercising the bug.
    assert view.iter_messages() != list(task.runtime.messages)
    assert view.rolling_history == list(task.runtime.messages)
    assert view.summary_boundary == 4

    # Drive the policy compaction path. The summarize call is served by the
    # fake provider; the decision carries the boundary.
    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="fresh summary")],
            )
        ]
    )
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=store
    )
    policy = _policy(client)
    decision = policy.decide(_ctx(), view)
    assert isinstance(decision, CompactionRequestedDecision)
    boundary = decision.boundary_count

    # The boundary is a raw-history index: strictly inside the raw list and
    # NOT equal to a view-projection index (the view list is shorter because
    # the prior summary collapsed 4 → 1 and a skill prefix was prepended).
    assert 0 < boundary <= len(task.runtime.messages)

    # The summarize request the policy sent must be raw_runtime[:boundary] —
    # NOT iter_messages()[:something]. The fake provider records what it saw.
    summarize_req = provider.received_requests[0]
    assert summarize_req.messages == list(task.runtime.messages[:boundary])

    # Now feed that boundary back through the composer the way fold would, and
    # confirm it drops exactly raw_runtime[:boundary] and prepends ONE summary.
    task.context.summary_boundary = boundary
    fresh_summary_ref = store.put(
        to_canonical_bytes("fresh summary"), media_type="application/json"
    )
    task.context.summary_ref = fresh_summary_ref
    dynamic_after = composer._apply_summary(task)
    # First message is the single summary; the rest is raw_runtime[boundary:].
    assert dynamic_after[1:] == list(task.runtime.messages[boundary:])
    assert dynamic_after[0].content[0].text == "fresh summary"


def test_boundary_protects_the_tail_window() -> None:
    """The tail window (sized by ``tail_token_budget``) stays out of the
    summary: the boundary leaves the newest messages whose cumulative estimate
    fits the budget, computed over RAW history."""
    store = InMemoryContentStore()
    log = InMemoryEventLog()
    composer = ThreeSegmentComposer(
        system_prompt="sys",
        tools={},
        content_store=store,
        skill_renderer=_skill_renderer,
        tail_token_budget=300,
    )
    raw = _runtime_messages(12)
    task = Task(task_id="t-1")
    task.runtime.messages = list(raw)
    view = composer.compose(task)

    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn", content=[TextBlock(text="sum")]
            )
        ]
    )
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=store
    )
    decision = _policy(client).decide(_ctx(), view)
    assert isinstance(decision, CompactionRequestedDecision)
    # Some tail messages are protected → boundary strictly less than total.
    assert 0 < decision.boundary_count < len(raw)
