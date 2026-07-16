"""ThreeSegmentComposer prune + compaction awareness (③ D-3e).

Prune is a deterministic, pure transform of the dynamic
segment: tool-result outputs of messages OUTSIDE a protected tail window
(sized by a token budget, not a hard-coded count) are cleared — the
message stays in place with the same role / call_id / success, only its
``output`` is replaced by the LEAN cleared-marker
(``[tool output cleared]``) so the model
reads "content elided", never "tool returned nothing" — and never a hash it
cannot deref. The kept / cleared message refs are recorded in
``ContextPlan.selected_messages`` / ``dropped_messages``, and each cleared
output's full-body ref in ``ContextPlan.cleared_outputs`` (internal audit, off
the prompt). When the task carries a Compacted summary slice, the covered
prefix is swapped for a single summary message.

Because prune changes the composed bytes, ``_COMPOSER_VERSION`` rotates to
``three_segment.v5`` — the version tag records which composer produced a
recording, so an older recording simply carries the older tag
(D-3e).
"""

from __future__ import annotations

from typing import Any

from noeta.context.composer import _COMPOSER_VERSION, ThreeSegmentComposer
from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.token_estimate import estimate_messages_tokens
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.messages import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.task import Task, TaskState
from noeta.protocols.values import ContentRef
from noeta.storage.memory import InMemoryContentStore


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.risk_level = "low"
        self.input_schema = {"type": "object", "additionalProperties": True}

    def invoke(self, arguments: Any, ctx: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _tool_turn(call_id: str, output: str) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(call_id=call_id, tool_name="read", arguments={})
            ],
        ),
        Message(
            role="tool",
            content=[
                ToolResultBlock(call_id=call_id, output=output, success=True)
            ],
        ),
    ]


def _composer(
    store: InMemoryContentStore,
    *,
    tail_token_budget: int | None = None,
    available_window: int | None = None,
) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="sys",
        tools={"read": _FakeTool("read")},
        content_store=store,
        tail_token_budget=tail_token_budget,
        available_window=available_window,
    )


def _task(messages: list[Message], *, summary_ref=None, boundary=0) -> Task:
    t = Task(task_id="t-1", state=TaskState())
    t.runtime.messages.extend(messages)
    t.context.summary_ref = summary_ref
    t.context.summary_boundary = boundary
    return t


def test_composer_version_is_v5() -> None:
    assert _COMPOSER_VERSION == "three_segment.v5"


def test_no_budget_means_no_prune() -> None:
    store = InMemoryContentStore()
    msgs = _tool_turn("c1", "x" * 400) + _tool_turn("c2", "y" * 400)
    view = _composer(store).compose(_task(msgs))
    dynamic = view.segments[2].content
    outputs = [
        b.output
        for m in dynamic
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert outputs == ["x" * 400, "y" * 400]  # untouched


def test_prune_clears_old_tool_outputs_outside_tail() -> None:
    store = InMemoryContentStore()
    # Three tool turns; a tight budget keeps only the last turn's output.
    msgs = (
        _tool_turn("c1", "a" * 400)
        + _tool_turn("c2", "b" * 400)
        + _tool_turn("c3", "c" * 400)
    )
    view = _composer(store, tail_token_budget=120).compose(_task(msgs))
    dynamic = view.segments[2].content
    # Structure is unchanged: same number of messages, same roles/call_ids.
    assert len(dynamic) == len(msgs)
    by_call = {
        b.call_id: b
        for m in dynamic
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }
    # The newest output is preserved; older ones cleared to an explicit
    # marker — NOT an empty string (which reads as "no output").
    assert by_call["c3"].output == "c" * 400
    for cid in ("c1", "c2"):
        out = by_call[cid].output
        assert out != ""  # never blanked to empty
        # Lean marker: no hash leaked into the model-facing string.
        assert out == "[tool output cleared]"
    # role / call_id / success preserved on the cleared blocks.
    assert by_call["c1"].success is True


def test_cleared_output_is_dereferenceable_via_plan() -> None:
    """Invariant: a cleared output stays
    recoverable for audit — its full body derefs from
    ``ContextPlan.cleared_outputs`` (internal provenance), NOT from a hash
    leaked into the model-facing marker (which carries none).
    """
    store = InMemoryContentStore()
    msgs = _tool_turn("c1", "a" * 400) + _tool_turn("c2", "b" * 400)
    view = _composer(store, tail_token_budget=120).compose(_task(msgs))
    # The model-facing marker carries NO hash — just the lean placeholder.
    dynamic = view.segments[2].content
    cleared = next(
        b
        for m in dynamic
        for b in m.content
        if isinstance(b, ToolResultBlock)
        and b.output == "[tool output cleared]"
    )
    assert cleared.output == "[tool output cleared]"
    # The full body is recoverable through the plan's cleared_outputs refs.
    plan = from_canonical_bytes(store.get(view.plan_ref))
    assert isinstance(plan, ContextPlan)
    assert plan.cleared_outputs  # at least one cleared body recorded
    bodies = {
        from_canonical_bytes(store.get(ref)) for ref in plan.cleared_outputs
    }
    assert "a" * 400 in bodies  # full original output recoverable off-prompt


def test_prune_marker_is_idempotent_on_recompose() -> None:
    """Re-composing a View whose old outputs are already markers must not
    double-wrap them (byte-equal replay relies on this)."""
    store = InMemoryContentStore()
    msgs = (
        _tool_turn("c1", "a" * 400)
        + _tool_turn("c2", "b" * 400)
        + _tool_turn("c3", "c" * 400)
    )
    composer = _composer(store, tail_token_budget=120)
    once = composer.compose(_task(msgs))
    # Feed the already-pruned dynamic messages back through compose.
    pruned_msgs = list(once.segments[2].content)
    twice = composer.compose(_task(pruned_msgs))
    by_call = {
        b.call_id: b
        for m in twice.segments[2].content
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }
    for cid in ("c1", "c2"):
        out = by_call[cid].output
        # Idempotent: still exactly the lean marker, not a nested re-wrap.
        assert out == "[tool output cleared]"


def test_prune_is_deterministic_byte_equal() -> None:
    store = InMemoryContentStore()
    msgs = _tool_turn("c1", "a" * 400) + _tool_turn("c2", "b" * 400)
    t1 = _task(list(msgs))
    t2 = _task(list(msgs))
    v1 = _composer(store, tail_token_budget=120).compose(t1)
    v2 = _composer(store, tail_token_budget=120).compose(t2)
    assert v1.segments[2].segment_hash == v2.segments[2].segment_hash
    assert v1.plan_ref == v2.plan_ref  # content-addressed → byte-equal plan


def test_prune_records_selected_and_dropped_in_plan() -> None:
    store = InMemoryContentStore()
    msgs = (
        _tool_turn("c1", "a" * 400)
        + _tool_turn("c2", "b" * 400)
        + _tool_turn("c3", "c" * 400)
    )
    view = _composer(store, tail_token_budget=120).compose(_task(msgs))
    plan = from_canonical_bytes(store.get(view.plan_ref))
    assert isinstance(plan, ContextPlan)
    # dropped_messages records the nullified turns; not an empty placeholder.
    assert len(plan.dropped_messages) >= 1
    assert all(isinstance(r, ContentRef) for r in plan.dropped_messages)
    assert len(plan.selected_messages) >= 1


def test_prune_does_not_touch_text_messages() -> None:
    store = InMemoryContentStore()
    msgs = [
        Message(role="user", content=[TextBlock(text="z" * 400)]),
    ] + _tool_turn("c1", "a" * 400)
    view = _composer(store, tail_token_budget=120).compose(_task(msgs))
    dynamic = view.segments[2].content
    texts = [
        b.text
        for m in dynamic
        for b in m.content
        if isinstance(b, TextBlock)
    ]
    assert texts == ["z" * 400]  # text content never nullified by prune


def test_compaction_summary_swaps_covered_prefix() -> None:
    store = InMemoryContentStore()
    msgs = _tool_turn("c1", "a" * 40) + _tool_turn("c2", "b" * 40)
    summary_ref = store.put(b'"earlier conversation summary"', media_type="application/json")
    # boundary=2 → the first two messages are covered by the summary.
    task = _task(msgs, summary_ref=summary_ref, boundary=2)
    view = _composer(store).compose(task)
    dynamic = view.segments[2].content
    # First message is the summary; the covered prefix (2 msgs) is gone.
    assert dynamic[0].role == "user"
    assert any(
        isinstance(b, TextBlock) and "summary" in b.text
        for b in dynamic[0].content
    )
    # The two messages after the boundary remain.
    assert len(dynamic) == 1 + (len(msgs) - 2)


def test_relief_valve_keeps_all_outputs_below_window() -> None:
    """③ (D-3e relief-valve gate): a tight tail budget would clear old outputs,
    but with the window far above the history estimate prune must NOT fire —
    every tool output stays verbatim so the model never re-reads content it
    already fetched. This is the fix for the explore-agent re-read thrash."""
    store = InMemoryContentStore()
    msgs = (
        _tool_turn("c1", "a" * 400)
        + _tool_turn("c2", "b" * 400)
        + _tool_turn("c3", "c" * 400)
    )
    window = estimate_messages_tokens(msgs) + 1000  # plenty of headroom
    view = _composer(
        store, tail_token_budget=120, available_window=window
    ).compose(_task(msgs))
    dynamic = view.segments[2].content
    by_call = {
        b.call_id: b
        for m in dynamic
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }
    # All three outputs untouched despite the tight tail budget.
    assert by_call["c1"].output == "a" * 400
    assert by_call["c2"].output == "b" * 400
    assert by_call["c3"].output == "c" * 400
    # Nothing cleared / dropped is recorded in the plan.
    plan = from_canonical_bytes(store.get(view.plan_ref))
    assert isinstance(plan, ContextPlan)
    assert plan.cleared_outputs == []
    assert plan.dropped_messages == []


def test_relief_valve_prunes_once_history_exceeds_window() -> None:
    """Above the window the relief valve opens and prune behaves exactly like
    the ungated tail clamp: only the freshest output survives, older ones are
    cleared to the lean marker."""
    store = InMemoryContentStore()
    msgs = (
        _tool_turn("c1", "a" * 400)
        + _tool_turn("c2", "b" * 400)
        + _tool_turn("c3", "c" * 400)
    )
    # Window below the history estimate → valve opens; tight tail keeps only c3.
    view = _composer(
        store, tail_token_budget=120, available_window=10
    ).compose(_task(msgs))
    dynamic = view.segments[2].content
    by_call = {
        b.call_id: b
        for m in dynamic
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }
    assert by_call["c3"].output == "c" * 400  # newest verbatim
    assert by_call["c1"].output == "[tool output cleared]"
    assert by_call["c2"].output == "[tool output cleared]"


def test_relief_valve_opens_on_real_tokens_not_chars_over_four() -> None:
    """The gate compares against ``available_window``, which counts REAL
    provider tokens — so a payload that tokenises denser than the chars/4
    heuristic assumes must still open the valve.

    Measured in production: CJK + JSON + base64 thinking signatures run ~1.2
    chars/token against the assumed 4. The estimate read ~42k while the real
    request sat at ~182k against a ~182k window, so this gate stayed shut for an
    entire session (0 of 99 composes cleared anything) and the only relief left
    was a full summarize — which then re-read a file it had already fetched.
    ``last_input_tokens`` is the provider's own count for the previous
    round-trip; the gate takes the max of the two so a dense payload cannot hide
    behind the heuristic.
    """
    store = InMemoryContentStore()
    msgs = (
        _tool_turn("c1", "a" * 400)
        + _tool_turn("c2", "b" * 400)
        + _tool_turn("c3", "c" * 400)
    )
    estimate = estimate_messages_tokens(msgs)
    window = estimate + 1000  # chars/4 says: plenty of headroom, stay shut

    task = _task(msgs)
    # …but the provider billed the previous round-trip AT the window.
    task.runtime.last_input_tokens = window + 1

    view = _composer(
        store, tail_token_budget=120, available_window=window
    ).compose(task)
    dynamic = view.segments[2].content
    by_call = {
        b.call_id: b
        for m in dynamic
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }
    assert by_call["c3"].output == "c" * 400  # newest always verbatim
    assert by_call["c1"].output == "[tool output cleared]"
    assert by_call["c2"].output == "[tool output cleared]"


def test_no_baseline_reproduces_pure_estimate_behaviour() -> None:
    """``last_input_tokens == 0`` (first turn, or a compaction just zeroed it)
    → the gate falls back to exactly the pre-existing chars/4 arithmetic.

    This is what keeps the change byte-equal for an unobserved session, and it
    is the same fallback the Policy's density takes.
    """
    store = InMemoryContentStore()
    msgs = (
        _tool_turn("c1", "a" * 400)
        + _tool_turn("c2", "b" * 400)
        + _tool_turn("c3", "c" * 400)
    )
    window = estimate_messages_tokens(msgs) + 1000
    task = _task(msgs)
    assert task.runtime.last_input_tokens == 0

    view = _composer(
        store, tail_token_budget=120, available_window=window
    ).compose(task)
    plan = from_canonical_bytes(store.get(view.plan_ref))
    assert isinstance(plan, ContextPlan)
    assert plan.cleared_outputs == []  # valve stays shut, as it did before
