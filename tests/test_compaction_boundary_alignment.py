"""Policy boundary ↔ composer slice must live in the SAME index space.

``ThreeSegmentComposer._apply_summary`` slices the RAW ``task.runtime.messages``,
so the summarise ``boundary`` ``ReActPolicy`` computes must be an index into
that same list — which is why the View exposes ``rolling_history`` +
``summary_boundary`` rather than letting the policy count over
``iter_messages()`` (``semi_stable + dynamic_suffix``, post-summary, post-prune,
tail-truncated). A boundary computed in the projection points at a *different*
message the moment ``semi_stable`` is non-empty (skills active) or a prior
summary already collapsed a prefix, and the wrong slice gets summarised.

The setup is deliberately the case that separates the two coordinate systems: a
REAL composer with a non-empty skill renderer AND a prior summary. ``fake_view``
cannot exercise it — it puts every message in ``dynamic_suffix`` with an empty
``semi_stable`` and no prior summary, making the two lists equal.
"""

from __future__ import annotations


from noeta.context.composer import ThreeSegmentComposer, RenderedContent
from noeta.builtins.react.impl import ReActPolicy

# ``_carries_tool_result`` is a react.py internal with no package-level door;
# this boundary test reaches past ``impl`` on purpose.
from noeta.builtins.react.impl.react import _carries_tool_result
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


def _skill_renderer(_: list[str], __=None) -> RenderedContent:
    # A non-empty semi_stable segment: the prefix that makes iter_messages()
    # diverge from the raw runtime history.
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

    # Prior summary already collapsed the first 4 raw messages — the condition
    # that desynchronises iter_messages() from rolling_history.
    prior_summary_ref = store.put(
        to_canonical_bytes("earlier summary"), media_type="application/json"
    )
    task.context.summary_ref = prior_summary_ref
    task.context.summary_boundary = 4

    view = composer.compose(task)

    # Sanity: iter_messages() (semi + post-summary dynamic) is a DIFFERENT
    # list/length from the raw runtime history. If these were equal the two
    # coordinate systems would coincide and the test would prove nothing.
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
    result at index 2. The boundary must snap FORWARD to 3 so the
    ``tool_use``/``tool_result`` pair travels together into the collapsed
    prefix — never left straddling the boundary."""
    history = _paired_history()
    policy = _bare_policy(tail_token_budget=20)

    boundary = policy._summary_boundary(history)

    # The raw cutoff is 2 (the tool-result message); snapping forward gives 3.
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
