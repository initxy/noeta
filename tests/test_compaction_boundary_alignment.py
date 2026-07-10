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


from noeta.context.composer import ThreeSegmentComposer, RenderedSkills
from noeta.policies.react import ReActPolicy, _carries_tool_result
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import CompactionRequestedDecision
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
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


# ---------------------------------------------------------------------------
# Tool-pair alignment: the summary boundary must never split an
# ``assistant(tool_use)`` from its ``role="tool"`` result. Slicing there yields
# a summarize request that ends on a dangling ``tool_use`` (provider 400) AND a
# kept tail that starts on an orphan ``tool_result`` (next request 400). The
# raw all-``user`` histories above never exercise this — these do.
# ---------------------------------------------------------------------------


def _bare_policy(tail_token_budget: int) -> ReActPolicy:
    """A policy usable for calling ``_summary_boundary`` directly. The
    summarize LLM is never invoked, so a keyless fake provider is fine."""
    store = InMemoryContentStore()
    log = InMemoryEventLog()
    client = RuntimeLLMClient(
        provider=FakeLLMProvider(responses=[]),
        event_log=log,
        content_store=store,
    )
    return ReActPolicy(
        llm=client,
        tools={},
        system_prompt="sys",
        model="gpt-4o",
        context_window=600,
        max_output_tokens=50,
        compaction_buffer=50,
        tail_token_budget=tail_token_budget,
        composer_version="three_segment.v3",
    )


def _paired_history() -> list[Message]:
    """A realistic tool-using history: two ``assistant(tool_use) → tool``
    exchanges bracketed by plain turns."""
    return [
        Message(role="user", content=[TextBlock(text="g" * 400)]),          # 0
        Message(                                                             # 1
            role="assistant",
            content=[ToolUseBlock(
                call_id="c1", tool_name="read", arguments={"path": "x" * 400}
            )],
        ),
        Message(                                                             # 2
            role="tool",
            content=[ToolResultBlock(call_id="c1", output="ok", success=True)],
        ),
        Message(role="assistant", content=[TextBlock(text="hi there")]),     # 3
        Message(role="user", content=[TextBlock(text="next now")]),          # 4
    ]


def test_boundary_snaps_forward_off_a_tool_result_message() -> None:
    """A tail budget tuned so the raw token cutoff lands on the ``role="tool"``
    result at index 2. The fix must snap the boundary FORWARD to 3 so the
    ``tool_use``/``tool_result`` pair travels together into the collapsed
    prefix — never left straddling the boundary."""
    history = _paired_history()
    policy = _bare_policy(tail_token_budget=20)

    boundary = policy._summary_boundary(history)

    # Without the fix this is 2 (points at the tool-result message); with it,
    # forward-snapped to 3.
    assert boundary == 3
    # The kept tail begins on a self-contained turn, not an orphan result.
    assert not _carries_tool_result(history[boundary])
    # The collapsed prefix ends on the tool result, so its matching tool_use
    # (index 1) is paired inside what gets summarized — the summarize request
    # is well-formed.
    assert _carries_tool_result(history[boundary - 1])


def test_boundary_never_points_at_a_tool_result_across_budgets() -> None:
    """Property: for a paired history and any tail budget, the returned
    boundary is 0, len(history), or an index whose message is NOT a tool
    result — i.e. the boundary never orphans a ``tool_result``."""
    history = _paired_history()
    for budget in range(0, 60):
        boundary = _bare_policy(tail_token_budget=budget)._summary_boundary(history)
        assert 0 <= boundary <= len(history)
        if 0 < boundary < len(history):
            assert not _carries_tool_result(history[boundary]), (
                f"budget={budget} produced orphan boundary {boundary}"
            )
