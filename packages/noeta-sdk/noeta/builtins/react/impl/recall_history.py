"""``RecallHistory`` — the collapsed-history escape hatch.

Compaction replaces the oldest messages with a summary note, but the originals
are retained forever (``task.runtime.messages`` is append-only). This module
gives the model a way back to them: a control tool that renders a bounded,
deterministic slice of the collapsed prefix straight off the composed View
(``view.rolling_history[:view.summary_boundary]``), answered entirely at
translate time via a pre-answered result — no runtime handler, no new Decision
type, no event vocabulary.

The companion ``collapsed-context`` reminder (also here, declared by the react
manifest) renders only while ``summary_boundary > 0``, so a session that never
compacts never sees either surface do anything.

Determinism: rendering is a pure function of ``(view, arguments)`` and every
cap is a module constant, so a resumed run rebuilds the identical decision from
the recorded response. The rendered text enters history as a normal tool
result, which the Composer's tail prune later clears like any other tool
output — recalled content cannot permanently re-bloat the window.

Model-visible strings in this module (description, reminder, result headers)
deliberately avoid every ``_CONSTRAINT_TRIGGERS`` phrase: the rendered results
re-enter the next summarize input, and a trigger substring there would be
re-injected into every future note by ``enforce_verbatim_constraints``. A test
pins this.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from noeta.context.reminders import ReminderView
from noeta.execution.control_tool import (
    ControlToolBuildContext,
    ControlToolMount,
)
from noeta.policies.control_semantics import (
    ControlTranslateContext,
    ack_patch_decision,
)
from noeta.protocols.decisions import (
    Decision,
    ToolCall,
    ToolCallsDecision,
)
from noeta.protocols.messages import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.resources import load_markdown
from noeta.protocols.view import View


__all__ = [
    "RECALL_HISTORY_TOOL",
    "recall_history_tool_schema",
    "render_collapsed_slice",
    "translate_recall_history",
    "build_recall_history_control_tool",
    "collapsed_context_reminder",
]


#: Model-visible control-tool name for reading the collapsed prefix.
RECALL_HISTORY_TOOL = "RecallHistory"
#: Messages per call when the model sends no ``limit``.
_DEFAULT_LIMIT = 20
#: Hard per-call message ceiling; a larger ``limit`` is clamped, not refused.
_MAX_LIMIT = 50
#: Per-block character ceiling (text, tool arguments, tool output alike).
_BLOCK_CHAR_CAP = 2_000
#: Whole-result character ceiling; rendering stops at the first message that
#: would cross it and reports the resume offset.
_OUTPUT_CHAR_CAP = 24_000
#: Marker appended where a block was cut at ``_BLOCK_CHAR_CAP``.
_TRUNCATION_MARKER = " …[truncated]"


_RECALL_HISTORY_DESCRIPTION = load_markdown(__package__, "recall_history")


def recall_history_tool_schema() -> dict[str, Any]:
    """Provider-visible schema for :data:`RECALL_HISTORY_TOOL`."""
    return {
        "type": "function",
        "function": {
            "name": RECALL_HISTORY_TOOL,
            "description": _RECALL_HISTORY_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "offset": {
                        "type": "integer",
                        "description": (
                            "0-based index of the first collapsed message to "
                            "view; the valid range is [0, boundary)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"How many messages to view (default "
                            f"{_DEFAULT_LIMIT}, max {_MAX_LIMIT})."
                        ),
                    },
                },
                "required": ["offset"],
            },
        },
    }


def _validate_args(arguments: Any) -> tuple[bool, "tuple[int, int] | str"]:
    """Return ``(True, (offset, limit))`` or ``(False, error)``; never raises —
    malformed model input is data to ack, not an error to propagate."""
    if not isinstance(arguments, dict):
        return False, "arguments must be an object"
    offset = arguments.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return False, "offset must be a non-negative integer"
    limit = arguments.get("limit", _DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return False, "limit must be a positive integer"
    return True, (offset, min(limit, _MAX_LIMIT))


def _cap(text: str) -> str:
    if len(text) <= _BLOCK_CHAR_CAP:
        return text
    return text[:_BLOCK_CHAR_CAP] + _TRUNCATION_MARKER


def _render_block(block: Any) -> str:
    """One content block as one deterministic line (type-tagged, capped)."""
    if isinstance(block, TextBlock):
        return f"text: {_cap(block.text)}"
    if isinstance(block, ToolUseBlock):
        args = json.dumps(
            dict(block.arguments), ensure_ascii=False, sort_keys=True
        )
        return f"tool_use {block.tool_name}: {_cap(args)}"
    if isinstance(block, ToolResultBlock):
        status = "ok" if block.success else "error"
        return f"tool_result({status}): {_cap(str(block.output))}"
    return f"{type(block).__name__}"


def _render_message(index: int, message: Message) -> str:
    lines = [f"[{index}] {message.role}:"]
    for block in message.content:
        lines.append("  " + _render_block(block))
    return "\n".join(lines)


def render_collapsed_slice(view: View, offset: int, limit: int) -> str:
    """The tool's answer text: a bounded rendering of the collapsed prefix.

    Pure over ``(view, offset, limit)``. The header always reports the live
    boundary so the model can page without guessing; a slice cut short by the
    output cap reports the exact resume offset.
    """
    boundary = max(0, view.summary_boundary)
    if boundary == 0:
        return (
            "No messages have been collapsed yet — the summary note is either "
            "absent or empty, and the full conversation is still present "
            "verbatim."
        )
    if offset >= boundary:
        return (
            f"offset {offset} is past the collapsed range: valid offsets are "
            f"0..{boundary - 1} ({boundary} collapsed messages)."
        )
    collapsed = view.rolling_history[:boundary]
    end = min(offset + limit, boundary)
    header = (
        f"Collapsed range: {boundary} messages (indices 0..{boundary - 1}). "
        f"Showing {offset}..{end - 1}."
    )
    parts = [header]
    used = len(header)
    shown_to = offset
    for i in range(offset, end):
        rendered = _render_message(i, collapsed[i])
        if used + len(rendered) > _OUTPUT_CHAR_CAP and shown_to > offset:
            parts.append(
                f"(output cap reached — continue with offset={i}.)"
            )
            break
        parts.append(rendered)
        used += len(rendered)
        shown_to = i + 1
    else:
        if end < boundary:
            parts.append(
                f"(more collapsed messages follow — continue with "
                f"offset={end}.)"
            )
    return "\n\n".join(parts)


def _maybe_recall_history_decision(
    ctx: ControlTranslateContext,
) -> Optional[Decision]:
    tool_uses = [
        b for b in ctx.response.content if isinstance(b, ToolUseBlock)
    ]
    recall_blocks = [
        b for b in tool_uses if b.tool_name == RECALL_HISTORY_TOOL
    ]
    if not recall_blocks:
        return None
    if len(recall_blocks) != 1:
        return ack_patch_decision(
            tool_uses,
            ctx.assistant_message,
            ctx.assistant_thinking,
            patch=None,
            text="RecallHistory may appear at most once per turn",
            valid=False,
        )
    block = recall_blocks[0]
    others = [b for b in tool_uses if b is not block]
    control_others = [
        b for b in others if b.tool_name in ctx.control_tool_names
    ]
    if control_others:
        # Same stance as TodoWrite: a co-occurring CONTROL call is one the
        # ToolRuntime could never answer, so the mix stays a recoverable
        # error rather than a half-run batch.
        return ack_patch_decision(
            tool_uses,
            ctx.assistant_message,
            ctx.assistant_thinking,
            patch=None,
            text=(
                "RecallHistory cannot be batched with another control tool "
                f"({control_others[0].tool_name}); issue them in separate "
                "turns"
            ),
            valid=False,
        )
    ok, result = _validate_args(block.arguments)
    if not ok:
        assert isinstance(result, str)
        return ack_patch_decision(
            tool_uses,
            ctx.assistant_message,
            ctx.assistant_thinking,
            patch=None,
            text=result,
            valid=False,
        )
    if ctx.view is None:
        # A caller that predates the ``view`` field: degrade recoverably.
        return ack_patch_decision(
            tool_uses,
            ctx.assistant_message,
            ctx.assistant_thinking,
            patch=None,
            text="the collapsed history is unavailable this turn",
            valid=False,
        )
    assert isinstance(result, tuple)
    offset, limit = result
    text = render_collapsed_slice(ctx.view, offset, limit)
    if not others:
        return ack_patch_decision(
            [block],
            ctx.assistant_message,
            ctx.assistant_thinking,
            patch=None,
            text=text,
            valid=True,
        )
    # Mixed turn: the recall answer rides the SAME decision that carries the
    # remaining calls to the ToolRuntime — one turn, one batched result
    # message, every tool_use answered exactly once.
    return ToolCallsDecision(
        calls=[
            ToolCall(
                tool_name=b.tool_name,
                arguments=dict(b.arguments),
                call_id=b.call_id,
            )
            for b in others
        ],
        assistant_message=ctx.assistant_message,
        assistant_thinking=ctx.assistant_thinking,
        preacked_results=(
            ToolResultBlock(
                call_id=block.call_id,
                output=text,
                success=True,
            ),
        ),
    )


def translate_recall_history(
    ctx: ControlTranslateContext,
) -> Optional[Decision]:
    """The ``RecallHistory`` routing seam the mount binds into a
    ``ControlToolSpec``."""
    return _maybe_recall_history_decision(ctx)


def build_recall_history_control_tool(
    ctx: ControlToolBuildContext,
) -> Optional[ControlToolMount]:
    """The ``recall_history`` ``control_tool`` contribution factory.

    Self-gates on the ``recall_history`` flag. The SDK host computes it the
    way it computes ``workflow`` — from host wiring, not a spec activation —
    and sets it whenever it wires compaction (today: always), because the
    escape hatch belongs to every agent whose history can collapse, subagents
    included. A bare kernel build carries no flags and stays bare. Bands
    550/550 sit between ``run_workflow`` (500) and ``structured_output``'s
    schema band (600) so the terminal answer tool stays LAST — a byte-order
    contract the control-tool schema goldens pin; do not renumber.
    """
    if not ctx.flag("recall_history"):
        return None
    return ControlToolMount(
        name=RECALL_HISTORY_TOOL,
        schema=recall_history_tool_schema(),
        translate=translate_recall_history,
        routing_priority=550,
        schema_priority=550,
    )


def collapsed_context_reminder(view: ReminderView) -> Optional[str]:
    """Point at the collapsed range while one exists.

    Live only while ``summary_boundary > 0`` — a session that never compacted
    renders nothing. The boundary self-updates every compose, so the pointer
    is always current (unlike a line baked into the note, which would go stale
    and be carried forward by the summarizer).
    """
    if view.summary_boundary <= 0:
        return None
    n = view.summary_boundary
    return (
        f"The note at the head of this conversation stands in for the first "
        f"{n} original messages (indices 0..{n - 1}), which remain retained. "
        "When the note lacks a detail you need — the exact text of an "
        "earlier error, code produced or discussed before compaction, the "
        "precise wording of an earlier exchange — call RecallHistory with an "
        "offset in that range to view the originals."
    )
