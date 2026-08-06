"""``RecallHistory`` — the collapsed-history escape hatch.

Translate-level coverage (the tool is answered entirely at translate time, so
the seam under test is ``translate_recall_history`` over a
``ControlTranslateContext``), plus the ``collapsed-context`` reminder, the
mount gating, and the trigger-hygiene pin: no model-visible string of the
feature may contain a ``_CONSTRAINT_TRIGGERS`` phrase, because rendered recall
results re-enter the summarize input and a trigger substring there would be
re-injected into every future note by ``enforce_verbatim_constraints``.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.builtins.react.impl.react import _CONSTRAINT_TRIGGERS
from noeta.builtins.react.impl.recall_history import (
    RECALL_HISTORY_TOOL,
    _RECALL_HISTORY_DESCRIPTION,
    build_recall_history_control_tool,
    collapsed_context_reminder,
    recall_history_tool_schema,
    render_collapsed_slice,
    translate_recall_history,
)
from noeta.context.reminders import ReminderView
from noeta.execution.control_tool import ControlToolBuildContext
from noeta.policies.control_semantics import ControlTranslateContext
from noeta.protocols.decisions import StatePatchDecision, ToolCallsDecision
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.view import View


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _history(n: int) -> list[Message]:
    """``n`` alternating user/assistant messages with recognizable bodies."""
    out: list[Message] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(Message(role=role, content=[TextBlock(text=f"m{i} body")]))
    return out


def _view(n: int = 6, boundary: int = 4) -> View:
    return View(rolling_history=_history(n), summary_boundary=boundary)


def _recall_use(
    arguments: dict[str, Any], call_id: str = "rh"
) -> ToolUseBlock:
    return ToolUseBlock(
        call_id=call_id, tool_name=RECALL_HISTORY_TOOL, arguments=arguments
    )


def _ctx(
    *blocks: ToolUseBlock,
    view: Optional[View] = None,
    control_tool_names: frozenset[str] = frozenset({RECALL_HISTORY_TOOL}),
) -> ControlTranslateContext:
    response = LLMResponse(
        stop_reason="tool_use",
        content=list(blocks),
        usage=Usage(uncached=1, output=1),
        raw={"id": "t"},
    )
    return ControlTranslateContext(
        response=response,
        assistant_message=Message(role="assistant", content=list(blocks)),
        assistant_thinking=(),
        content_store=None,
        control_tool_names=control_tool_names,
        view=view,
    )


def _ack_output(decision: StatePatchDecision) -> ToolResultBlock:
    """First result block of the ack message (error acks answer EVERY
    tool_use in the turn with the same text, so there may be several)."""
    (ack,) = decision.messages_after
    block = ack.content[0]
    assert isinstance(block, ToolResultBlock)
    return block


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_solo_call_renders_the_requested_slice() -> None:
    decision = translate_recall_history(
        _ctx(_recall_use({"offset": 0, "limit": 2}), view=_view())
    )
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is None
    result = _ack_output(decision)
    assert result.success is True
    out = result.output
    assert "Collapsed range: 4 messages (indices 0..3)" in out
    assert "Showing 0..1" in out
    assert "[0] user:" in out and "m0 body" in out
    assert "[1] assistant:" in out and "m1 body" in out
    # The slice stops at the limit and reports how to continue.
    assert "m2 body" not in out
    assert "continue with offset=2" in out


def test_render_stops_at_the_boundary_never_the_tail() -> None:
    out = render_collapsed_slice(_view(n=6, boundary=4), 0, 50)
    assert "m3 body" in out
    # Messages 4 and 5 are the live tail — outside the collapsed range.
    assert "m4 body" not in out and "m5 body" not in out
    assert "continue with" not in out


def test_offset_past_boundary_is_informational() -> None:
    out = render_collapsed_slice(_view(), 9, 5)
    assert "valid offsets are 0..3" in out


def test_boundary_zero_says_nothing_collapsed() -> None:
    out = render_collapsed_slice(_view(boundary=0), 0, 5)
    assert "No messages have been collapsed yet" in out


def test_tool_blocks_render_type_tagged() -> None:
    history = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    call_id="c1", tool_name="Read", arguments={"path": "x.py"}
                )
            ],
        ),
        Message(
            role="tool",
            content=[
                ToolResultBlock(call_id="c1", output="file body", success=True)
            ],
        ),
    ]
    view = View(rolling_history=history, summary_boundary=2)
    out = render_collapsed_slice(view, 0, 10)
    assert 'tool_use Read: {"path": "x.py"}' in out
    assert "tool_result(ok): file body" in out


# ---------------------------------------------------------------------------
# Translate routing
# ---------------------------------------------------------------------------


def test_no_recall_call_returns_none() -> None:
    other = ToolUseBlock(call_id="x", tool_name="Read", arguments={})
    assert translate_recall_history(_ctx(other, view=_view())) is None


def test_malformed_offset_is_a_recoverable_error() -> None:
    for bad in ({}, {"offset": -1}, {"offset": True}, {"offset": "0"}):
        decision = translate_recall_history(
            _ctx(_recall_use(bad), view=_view())
        )
        assert isinstance(decision, StatePatchDecision)
        assert decision.patch is None
        assert _ack_output(decision).success is False


def test_missing_view_degrades_recoverably() -> None:
    decision = translate_recall_history(
        _ctx(_recall_use({"offset": 0}), view=None)
    )
    assert isinstance(decision, StatePatchDecision)
    assert _ack_output(decision).success is False


def test_double_recall_is_a_recoverable_error() -> None:
    decision = translate_recall_history(
        _ctx(
            _recall_use({"offset": 0}, call_id="a"),
            _recall_use({"offset": 1}, call_id="b"),
            view=_view(),
        )
    )
    assert isinstance(decision, StatePatchDecision)
    assert _ack_output(decision).success is False


def test_batched_with_runtime_tool_preacks_the_recall() -> None:
    read = ToolUseBlock(
        call_id="r1", tool_name="Read", arguments={"path": "x.py"}
    )
    decision = translate_recall_history(
        _ctx(_recall_use({"offset": 0}), read, view=_view())
    )
    assert isinstance(decision, ToolCallsDecision)
    assert [c.tool_name for c in decision.calls] == ["Read"]
    (pre,) = decision.preacked_results
    assert pre.call_id == "rh" and pre.success is True
    assert "Collapsed range" in pre.output


def test_batched_with_another_control_tool_is_refused() -> None:
    todo = ToolUseBlock(call_id="t1", tool_name="TodoWrite", arguments={})
    decision = translate_recall_history(
        _ctx(
            _recall_use({"offset": 0}),
            todo,
            view=_view(),
            control_tool_names=frozenset({RECALL_HISTORY_TOOL, "TodoWrite"}),
        )
    )
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is None
    assert _ack_output(decision).success is False


def test_translate_is_deterministic() -> None:
    a = translate_recall_history(
        _ctx(_recall_use({"offset": 0, "limit": 3}), view=_view())
    )
    b = translate_recall_history(
        _ctx(_recall_use({"offset": 0, "limit": 3}), view=_view())
    )
    assert a == b


# ---------------------------------------------------------------------------
# Mount gating
# ---------------------------------------------------------------------------


def _build_ctx(flags: dict[str, bool]) -> ControlToolBuildContext:
    return ControlToolBuildContext(
        capability_flags=flags,
        subtask_agent_directory=(),
        structured_output_schema=None,
    )


def test_mount_gates_on_the_recall_history_flag() -> None:
    assert build_recall_history_control_tool(_build_ctx({})) is None
    mount = build_recall_history_control_tool(
        _build_ctx({"recall_history": True})
    )
    assert mount is not None
    assert mount.name == RECALL_HISTORY_TOOL
    assert (mount.routing_priority, mount.schema_priority) == (550, 550)


# ---------------------------------------------------------------------------
# Reminder
# ---------------------------------------------------------------------------


def test_reminder_is_silent_without_a_boundary() -> None:
    assert collapsed_context_reminder(ReminderView()) is None
    assert (
        collapsed_context_reminder(ReminderView(summary_boundary=0)) is None
    )


def test_reminder_points_at_the_live_boundary() -> None:
    text = collapsed_context_reminder(ReminderView(summary_boundary=7))
    assert text is not None
    assert "first 7 original messages" in text
    assert "indices 0..6" in text
    assert RECALL_HISTORY_TOOL in text


def test_reminder_ships_in_the_default_base_specs() -> None:
    from noeta.client.parts import default_reminder_specs

    names = [s.name for s in default_reminder_specs()]
    assert "collapsed-context" in names
    # The classic three are still there — the widened loader dropped nothing.
    for classic in ("unfinished-todos", "delegation-nudge", "read-suggestion"):
        assert classic in names


# ---------------------------------------------------------------------------
# Trigger hygiene
# ---------------------------------------------------------------------------


def test_model_visible_strings_carry_no_constraint_trigger() -> None:
    """A trigger phrase in recall/reminder text would be detected by
    ``extract_safety_constraints`` once it re-enters the summarize input and
    then re-injected into every future note — keep every surface clean."""
    reminder = collapsed_context_reminder(ReminderView(summary_boundary=5))
    assert reminder is not None
    surfaces = [
        _RECALL_HISTORY_DESCRIPTION,
        reminder,
        render_collapsed_slice(_view(boundary=0), 0, 5),
        render_collapsed_slice(_view(), 9, 5),
        # Error ack texts:
        "RecallHistory may appear at most once per turn",
        "the collapsed history is unavailable this turn",
        "offset must be a non-negative integer",
        "limit must be a positive integer",
    ]
    for text in surfaces:
        lowered = text.lower()
        for trigger in _CONSTRAINT_TRIGGERS:
            assert trigger not in lowered, (trigger, text)


def test_schema_strings_carry_no_constraint_trigger() -> None:
    import json

    lowered = json.dumps(recall_history_tool_schema()).lower()
    for trigger in _CONSTRAINT_TRIGGERS:
        assert trigger not in lowered, trigger
