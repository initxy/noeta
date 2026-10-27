"""``RecallHistory`` and the reminders that point at it (ADR-003, 050).

A compaction collapses old messages into a note and the valves clear old
tool outputs, but the log keeps every original. ``RecallHistory`` renders a
bounded slice of them by message index — indices into the whole uncompacted
history, which never shift. The model-visible texts are 0.6's byte for byte
and avoid every constraint trigger phrase (a recalled result re-enters the
next summarize input).
"""

from __future__ import annotations

import bisect
import json
from typing import TYPE_CHECKING, Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, Text, ToolResult, ToolUse
from noeta.plugins.compaction.clearing import CLEARED_MARKER
from noeta.plugins.compaction.summary import load_prompt

if TYPE_CHECKING:
    from noeta.core.loop.state import TaskState

RECALL_HISTORY_TOOL = "RecallHistory"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
#: Per-block character cap (text, tool arguments and tool output alike).
BLOCK_CHAR_CAP = 2_000
#: Whole-result cap; rendering stops at the message that would cross it.
OUTPUT_CHAR_CAP = 24_000
TRUNCATION_MARKER = " …[truncated]"

RECALL_HISTORY_DESCRIPTION = load_prompt("recall_history")

READ_SUGGESTION = (
    "The context window keeps getting refilled to the limit by what "
    "looks like a single large file or large tool output, so compaction "
    "is spinning without freeing real headroom. Consider a different "
    "reading strategy: read in chunks, read only the relevant section, "
    "or extract the key points once and re-read on demand instead of "
    "pulling the whole large content back into context each time."
)


def summary_boundary(state: TaskState) -> int:
    """How many messages of the uncompacted history the summary covers."""
    if state.compaction is None:
        return 0
    return bisect.bisect_right(state.history_seqs, state.compaction.through)


def _collapsed_line(n: int) -> str:
    return (
        f"The note at the head of this conversation stands in for the first "
        f"{n} original messages (indices 0..{n - 1}), which remain retained. "
        "When the note lacks a detail you need — the exact text of an "
        "earlier error, code produced or discussed before compaction, the "
        "precise wording of an earlier exchange — call RecallHistory with an "
        "offset in that range to view the originals."
    )


def collapsed_context(collapsed: int, cleared: int) -> str | None:
    """The pointer at the collapsed range and the cleared outputs, while
    either exists."""
    parts: list[str] = []
    if collapsed > 0:
        parts.append(_collapsed_line(collapsed))
    if cleared > collapsed:
        parts.append(
            f"Earlier tool outputs shown as {CLEARED_MARKER} are retained "
            f"too (in messages 0..{cleared - 1}): view one with RecallHistory rather "
            "than re-running the tool; limit 1 shows a single message in full."
        )
    return " ".join(parts) or None


def _cap(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + TRUNCATION_MARKER


def _render_block(block: Any, cap: int) -> str:
    if isinstance(block, Text):
        return f"text: {_cap(block.text, cap)}"
    if isinstance(block, ToolUse):
        args = json.dumps(dict(block.input), ensure_ascii=False, sort_keys=True)
        return f"tool_use {block.name}: {_cap(args, cap)}"
    if isinstance(block, ToolResult):
        status = "error" if block.is_error else "ok"
        return f"tool_result({status}): {_cap(block.text, cap)}"
    return type(block).__name__


def _render_message(index: int, message: Message, cap: int) -> str:
    lines = [f"[{index}] {message.role}:"]
    lines.extend("  " + _render_block(b, cap) for b in message.content)
    return "\n".join(lines)


def render_slice(history: list[Message], collapsed: int, cleared: int,
                 offset: int, limit: int) -> str:
    """The tool's answer: messages ``offset..`` of the recallable range
    ``[0, max(collapsed, cleared))``. ``limit`` 1 shows one message whole."""
    collapsed_end = max(0, collapsed)
    boundary = min(max(collapsed_end, cleared), len(history))
    if boundary == 0:
        return ("No messages have been collapsed yet — the summary note is either "
                "absent or empty, and the full conversation is still present "
                "verbatim.")
    if offset >= boundary:
        return (f"offset {offset} is past the collapsed range: valid offsets are "
                f"0..{boundary - 1} ({boundary} collapsed messages).")
    end = min(offset + limit, boundary)
    if boundary == collapsed_end:
        header = (f"Collapsed range: {boundary} messages (indices 0..{boundary - 1}). "
                  f"Showing {offset}..{end - 1}.")
    else:
        header = (f"Recallable range: {boundary} messages (indices "
                  f"0..{boundary - 1}; the first {collapsed_end} are collapsed into "
                  f"the note, later ones may have cleared tool outputs). "
                  f"Showing {offset}..{end - 1}.")
    cap = OUTPUT_CHAR_CAP if limit == 1 else BLOCK_CHAR_CAP
    parts = [header]
    used = len(header)
    shown_to = offset
    for i in range(offset, end):
        rendered = _render_message(i, history[i], cap)
        if used + len(rendered) > OUTPUT_CHAR_CAP and shown_to > offset:
            parts.append(f"(output cap reached — continue with offset={i}.)")
            break
        parts.append(rendered)
        used += len(rendered)
        shown_to = i + 1
    else:
        if end < boundary:
            parts.append(f"(more collapsed messages follow — continue with offset={end}.)")
    return "\n\n".join(parts)


def _validate(arguments: Any) -> tuple[int, int] | str:
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    offset = arguments.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return "offset must be a non-negative integer"
    limit = arguments.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return "limit must be a positive integer"
    return offset, min(limit, MAX_LIMIT)


class RecallHistoryTool:
    """Bound to one running task's folded state (the extension hands out a
    fresh one per ``tools(view)``)."""

    name = RECALL_HISTORY_TOOL
    description = RECALL_HISTORY_DESCRIPTION
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "offset": {
                "type": "integer",
                "description": ("0-based index of the first collapsed message to "
                                "view; the valid range is [0, boundary)."),
            },
            "limit": {
                "type": "integer",
                "description": (f"How many messages to view (default "
                                f"{DEFAULT_LIMIT}, max {MAX_LIMIT})."),
            },
        },
        "required": ["offset"],
    }
    read_only = True
    #: A recalled page cannot be fetched again by re-running anything else.
    clearable = False

    def __init__(self, state: TaskState, extension: str) -> None:
        self._state = state
        self._extension = extension

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        parsed = _validate(input)
        if isinstance(parsed, str):
            return text_output(parsed, is_error=True)
        offset, limit = parsed
        ext = ctx.state.get(self._extension)
        cleared = int(getattr(ext, "cleared_boundary", 0) or 0)
        return text_output(render_slice(self._state.history, summary_boundary(self._state),
                                        cleared, offset, limit))
