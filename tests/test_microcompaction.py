"""Microcompaction: the count-based second valve in the composer's prune.

Once the request reaches ``microcompact_fraction`` of the usable window, every
tool output older than the newest ``microcompact_keep_recent`` tool-result
blocks is cleared to the lean marker, with the same size floor and
cleared-output provenance as the relief valve — which stays the backstop at the
water mark. ``RecallHistory`` pages a cleared-but-not-summarized output back by
message index, and the collapsed-context reminder advertises it even before any
summary exists.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.builtins.react.impl.recall_history import (
    collapsed_context_reminder,
    render_collapsed_slice,
)
from noeta.context.composer import (
    _MIN_CLEARABLE_OUTPUT_CHARS,
    ThreeSegmentComposer,
)
from noeta.context.reminders import ReminderRegistry, ReminderSpec, ReminderView
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.decisions import FinishDecision, ToolCall, ToolCallsDecision
from noeta.protocols.messages import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.task import Task, TaskState
from noeta.protocols.token_estimate import estimate_messages_tokens
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.protocols.view import View
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)

_MARKER = "[tool output cleared]"


class _BulkTool:
    risk_level = "low"
    input_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}

    def __init__(self, name: str = "read") -> None:
        self.name = name
        self.description = "returns a bulky body"

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tag = str(arguments.get("tag", "x"))
        return ToolResult(success=True, output=(tag + " ") * 300)


def _tool_turn(call_id: str, output: str) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[ToolUseBlock(call_id=call_id, tool_name="read", arguments={})],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(call_id=call_id, output=output, success=True)],
        ),
    ]


def _history(n: int, size: int = 600) -> list[Message]:
    msgs: list[Message] = []
    for i in range(n):
        msgs += _tool_turn(f"c{i}", chr(ord("a") + i) * size)
    return msgs


def _task(messages: list[Message], *, boundary: int = 0) -> Task:
    t = Task(task_id="t-1", state=TaskState())
    t.runtime.messages.extend(messages)
    t.context.summary_boundary = boundary
    return t


def _composer(
    store: InMemoryContentStore,
    *,
    window: int | None,
    keep: int | None = 5,
    fraction: float = 0.5,
    tail: int | None = None,
    reminders: ReminderRegistry | None = None,
) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="sys",
        tools={"read": _BulkTool()},
        content_store=store,
        tail_token_budget=tail,
        available_window=window,
        microcompact_keep_recent=keep,
        microcompact_fraction=fraction,
        reminders=reminders,
    )


def _outputs(view: View) -> dict[str, Any]:
    return {
        b.call_id: b.output
        for m in view.segments[2].content
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }


def _plan(store: InMemoryContentStore, view: View) -> ContextPlan:
    plan = from_canonical_bytes(store.get(view.plan_ref))
    assert isinstance(plan, ContextPlan)
    return plan


def _size(msgs: list[Message]) -> int:
    # The composer's reading: stable prefix ("sys") + the dynamic history.
    return estimate_messages_tokens(
        [Message(role="system", content=[TextBlock(text="sys")])] + msgs
    )


# ---------------------------------------------------------------------------
# MC1 — the valve
# ---------------------------------------------------------------------------


def test_valve_fires_at_the_fraction_and_not_below() -> None:
    msgs = _history(8)
    size = _size(msgs)
    # Exactly at the fraction: fires.
    store = InMemoryContentStore()
    at = _composer(store, window=size * 2).compose(_task(msgs))
    assert _MARKER in _outputs(at).values()
    # One token under the fraction: stays shut.
    store = InMemoryContentStore()
    below = _composer(store, window=size * 2 + 2).compose(_task(msgs))
    assert _MARKER not in _outputs(below).values()
    assert _plan(store, below).cleared_outputs == []


def test_valve_reads_the_real_baseline_like_the_relief_gate() -> None:
    msgs = _history(8)
    window = _size(msgs) * 10  # the estimate alone is far below half
    task = _task(msgs)
    task.runtime.last_input_tokens = window // 2  # the provider says: half
    view = _composer(InMemoryContentStore(), window=window).compose(task)
    assert _MARKER in _outputs(view).values()


def test_keeps_exactly_the_newest_n_tool_results() -> None:
    msgs = _history(8)
    store = InMemoryContentStore()
    view = _composer(store, window=10, keep=5).compose(_task(msgs))
    out = _outputs(view)
    for i in range(3):
        assert out[f"c{i}"] == _MARKER
    for i in range(3, 8):
        assert out[f"c{i}"] == chr(ord("a") + i) * 600
    plan = _plan(store, view)
    assert len(plan.cleared_outputs) == 3
    # Provenance: each cleared ref derefs to the full original body.
    bodies = [from_canonical_bytes(store.get(r)) for r in plan.cleared_outputs]
    assert bodies == ["a" * 600, "b" * 600, "c" * 600]


def test_counts_tool_result_blocks_not_messages() -> None:
    # One parallel batch of four results, then two single results: with
    # keep=3 only the two singles and the LAST block of the batch survive.
    batch_use = Message(
        role="assistant",
        content=[
            ToolUseBlock(call_id=f"p{j}", tool_name="read", arguments={})
            for j in range(4)
        ],
    )
    batch_result = Message(
        role="tool",
        content=[
            ToolResultBlock(call_id=f"p{j}", output=str(j) * 600, success=True)
            for j in range(4)
        ],
    )
    msgs = [batch_use, batch_result] + _tool_turn("s0", "s" * 600) + _tool_turn(
        "s1", "t" * 600
    )
    view = _composer(InMemoryContentStore(), window=10, keep=3).compose(
        _task(msgs)
    )
    out = _outputs(view)
    assert [out[f"p{j}"] for j in range(3)] == [_MARKER] * 3
    assert out["p3"] == "3" * 600
    assert out["s0"] == "s" * 600 and out["s1"] == "t" * 600


def test_respects_the_size_floor() -> None:
    small = "q" * (_MIN_CLEARABLE_OUTPUT_CHARS - 1)
    msgs = _tool_turn("small", small) + _history(6)
    view = _composer(InMemoryContentStore(), window=10, keep=2).compose(
        _task(msgs)
    )
    out = _outputs(view)
    assert out["small"] == small  # under the floor: never cleared
    assert out["c0"] == _MARKER


def test_is_idempotent_across_composes() -> None:
    msgs = _history(8)
    store = InMemoryContentStore()
    composer = _composer(store, window=10)
    task = _task(msgs)
    first = composer.compose(task)
    second = composer.compose(task)
    assert first.segments[2].segment_hash == second.segments[2].segment_hash
    assert first.plan_ref == second.plan_ref
    # Re-composing an already-pruned history does not re-wrap the markers.
    again = composer.compose(_task(list(first.segments[2].content)))
    assert _outputs(again) == _outputs(first)
    assert _plan(store, again).cleared_outputs == []


def test_disabled_with_none() -> None:
    msgs = _history(8)
    store = InMemoryContentStore()
    view = _composer(store, window=10, keep=None).compose(_task(msgs))
    assert _MARKER not in _outputs(view).values()
    assert _plan(store, view).cleared_outputs == []
    assert view.cleared_boundary == 0


def test_invalid_knobs_are_refused() -> None:
    with pytest.raises(ValueError):
        _composer(InMemoryContentStore(), window=10, keep=0)
    with pytest.raises(ValueError):
        _composer(InMemoryContentStore(), window=10, fraction=0.0)


def test_relief_valve_still_fires_later() -> None:
    msgs = _history(8)
    size = _size(msgs)
    # Between the fraction and the water mark: only the count valve — the
    # newest five survive even though the tail budget would keep fewer.
    view = _composer(
        InMemoryContentStore(), window=size + 10, keep=5, tail=200
    ).compose(_task(msgs))
    kept = [k for k, v in _outputs(view).items() if v != _MARKER]
    assert kept == [f"c{i}" for i in range(3, 8)]
    # At the water mark the relief valve's token tail takes over: only the
    # freshest output fits the tight tail budget.
    view = _composer(
        InMemoryContentStore(), window=size, keep=5, tail=200
    ).compose(_task(msgs))
    kept = [k for k, v in _outputs(view).items() if v != _MARKER]
    assert kept == ["c7"]


def test_derived_config_carries_the_defaults() -> None:
    from noeta.builtins.providers.impl.catalog import derive_compaction_config
    from noeta.execution.builder import COMPACTION_OFF

    cfg = derive_compaction_config("claude-sonnet-4-5")
    assert cfg.microcompact_keep_recent == 5
    assert cfg.microcompact_fraction == 0.5
    assert COMPACTION_OFF.microcompact_keep_recent is None


# ---------------------------------------------------------------------------
# live vs resume
# ---------------------------------------------------------------------------


def test_resume_from_the_log_clears_the_same_blocks() -> None:
    dispatcher = InMemoryDispatcher()
    store = InMemoryContentStore()
    log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(log, dispatcher)
    steps = 8
    engine = Engine(
        event_log=log,
        content_store=store,
        composer=_composer(store, window=10),
        policy=StubScriptedPolicy(
            [
                ToolCallsDecision(
                    calls=[
                        ToolCall(
                            tool_name="read",
                            arguments={"tag": f"t{i}"},
                            call_id=f"c{i}",
                        )
                    ]
                )
                for i in range(steps)
            ]
            + [FinishDecision(answer="done")]
        ),
        tools={"read": _BulkTool()},
        tool_runtime=ToolRuntime(event_log=log, content_store=store),
    )
    task = engine.create_task(goal="read a lot", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    # One step runs the whole tool loop, through to the finish.
    engine.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "terminal"

    live = _composer(store, window=10).compose(task)
    rebuilt = fold(log, store, task.task_id)
    resumed = _composer(InMemoryContentStore(), window=10).compose(rebuilt)
    assert resumed.segments[2].segment_hash == live.segments[2].segment_hash
    assert resumed.cleared_boundary == live.cleared_boundary > 0
    assert _plan(store, live).cleared_outputs  # something was cleared
    assert resumed.plan_ref == live.plan_ref  # same markers, same refs
    cleared = [k for k, v in _outputs(live).items() if v == _MARKER]
    assert cleared == [f"c{i}" for i in range(steps - 5)]


# ---------------------------------------------------------------------------
# MC2 — RecallHistory pages a cleared output back before any summary
# ---------------------------------------------------------------------------


def test_recall_pages_back_a_microcompacted_output_before_any_summary() -> None:
    body = "line of a big file " * 200  # ~3.8 KB, over the per-block cap
    msgs = [Message(role="user", content=[TextBlock(text="go")])]
    msgs += _tool_turn("big", body) + _history(6)
    view = _composer(InMemoryContentStore(), window=10).compose(_task(msgs))
    assert view.summary_boundary == 0
    assert _outputs(view)["big"] == _MARKER
    # Raw index of the cleared message: user(0), assistant(1), tool(2).
    assert view.cleared_boundary >= 3
    text = render_collapsed_slice(view, 2, 1)
    assert "Recallable range" in text
    assert body.strip() in text  # the whole body, not a 2,000-char cut
    assert "[truncated]" not in text


def test_recall_range_is_unchanged_without_cleared_outputs() -> None:
    msgs = _history(3)
    view = View(
        plan_ref=None,  # type: ignore[arg-type]
        segments=(),  # type: ignore[arg-type]
        provider_tool_schemas=[],
        rolling_history=msgs,
        summary_boundary=0,
    )
    assert render_collapsed_slice(view, 0, 5).startswith("No messages")


def test_reminder_renders_for_cleared_outputs_without_a_summary() -> None:
    assert collapsed_context_reminder(ReminderView()) is None
    text = collapsed_context_reminder(ReminderView(cleared_boundary=9))
    assert text is not None
    assert "RecallHistory" in text and "0..8" in text
    assert "note at the head" not in text
    # A summary alone renders exactly the pre-existing text.
    summary_only = collapsed_context_reminder(ReminderView(summary_boundary=4))
    both = collapsed_context_reminder(
        ReminderView(summary_boundary=4, cleared_boundary=9)
    )
    assert summary_only is not None and both is not None
    assert both.startswith(summary_only) and both != summary_only
    # Cleared outputs inside the collapsed range add nothing.
    assert (
        collapsed_context_reminder(
            ReminderView(summary_boundary=4, cleared_boundary=4)
        )
        == summary_only
    )


def test_composer_feeds_the_reminder_its_cleared_boundary() -> None:
    registry = ReminderRegistry(
        (
            ReminderSpec(
                name="collapsed-context",
                priority=0,
                render=collapsed_context_reminder,
            ),
        )
    )
    msgs = _history(8)
    view = _composer(InMemoryContentStore(), window=10, reminders=registry).compose(
        _task(msgs)
    )
    last = view.segments[2].content[-1]
    assert last.origin == "system"
    block = last.content[0]
    assert isinstance(block, TextBlock) and "RecallHistory" in block.text
    # Below the fraction nothing is cleared and the reminder stays silent.
    quiet = _composer(
        InMemoryContentStore(), window=10**9, reminders=registry
    ).compose(_task(msgs))
    assert all(m.origin != "system" for m in quiet.segments[2].content)
