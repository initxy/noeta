"""The two valves that clear old tool results from the outgoing request.

The log is never changed: clearing rewrites only the request a step sends.
Which results a step clears is a pure function of the history and two
recorded numbers, and every result once cleared is recorded (the extension's
``cleared`` entries), so a restarted runner sends the same bytes.

* **Relief** (ADR-003): once the request reaches the usable window, results
  older than the protected tail are cleared; the newest message never is.
* **Micro** (ADR-050/061/066): past ``micro_fraction`` of the usable window,
  the clearable results older than the newest five are cut, oldest first, into
  batches of at least 20,000 estimated tokens; every complete batch is
  cleared and the last, incomplete one is not, so the cached prefix changes
  once per batch rather than once per step.

Only results of tools that declare themselves re-fetchable are cleared
(ADR-066), only outputs of 400 characters or more, and a tool call's
arguments never (ADR-063). The marker is Claude Code's (ADR-061).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

from noeta.ext.compaction.estimate import IMAGE_TOKENS, blocks_tokens, messages_tokens
from noeta.types import Image, Message, Text, ToolResult, ToolUse

#: Claude Code's text for a cleared result, byte for byte.
CLEARED_MARKER = "[Old tool result content cleared]"
#: An output under this many characters is not bulk and may not be
#: re-fetchable (an answer, an ack): it is never cleared.
MIN_CLEARABLE_OUTPUT_CHARS = 400
#: The micro valve keeps this many newest clearable results verbatim.
KEEP_RECENT = 5
#: The micro valve clears in batches of at least this many estimated tokens.
BATCH_TOKENS = 20_000
#: Claude Code's re-fetchable tools (ADR-066).
CLAUDE_CODE_CLEARABLE = frozenset({
    "Read", "Glob", "Grep", "Bash", "BashOutput", "Edit", "Write", "WebFetch", "WebSearch",
})

#: Sanity band for real tokens per estimated token (ADR-003): wide enough for
#: CJK text (~4-7), narrow enough that a gateway reporting nonsense usage
#: cannot blow the protected tail past the whole history.
DENSITY_MIN = 0.25
DENSITY_MAX = 8.0

Position = tuple[int, int]


def density(real: int, estimated: int) -> float:
    """Real tokens per estimated token on the last round-trip; ``1.0`` when
    there is nothing to measure."""
    if real <= 0 or estimated <= 0:
        return 1.0
    return min(DENSITY_MAX, max(DENSITY_MIN, real / estimated))


@dataclass(frozen=True)
class Valves:
    usable: int
    tail_budget: int
    micro_fraction: float
    #: ``None`` switches the micro valve off.
    keep_recent: int | None = KEEP_RECENT
    batch_tokens: int = BATCH_TOKENS


def rendered_size(result: ToolResult) -> int:
    """How many characters the model reads for a result (an image counts as
    its estimate's worth)."""
    images = sum(1 for b in result.content if isinstance(b, Image))
    return len(result.text) + images * IMAGE_TOKENS * 4


def is_cleared(result: ToolResult) -> bool:
    return (len(result.content) == 1 and isinstance(result.content[0], Text)
            and result.content[0].text == CLEARED_MARKER)


def worth_clearing(result: ToolResult) -> bool:
    return (bool(result.content) and not is_cleared(result)
            and rendered_size(result) >= MIN_CLEARABLE_OUTPUT_CHARS)


def clearable_positions(
    messages: Sequence[Message], is_clearable: Callable[[str], bool]
) -> list[Position]:
    """Every result a valve may clear, by (message, block), oldest first: the
    results of tools that declare themselves re-fetchable. Others never take a
    keep-recent place either."""
    tool_of: dict[str, str] = {}
    for m in messages:
        for b in m.content:
            if isinstance(b, ToolUse):
                tool_of[b.id] = b.name
    found: list[Position] = []
    for i, m in enumerate(messages):
        for j, b in enumerate(m.content):
            if isinstance(b, ToolResult) and is_clearable(tool_of.get(b.call_id, "")):
                found.append((i, j))
    return found


def micro_batches(messages: Sequence[Message], positions: Sequence[Position],
                  keep_recent: int, batch_tokens: int) -> set[Position]:
    """The complete batches older than the newest ``keep_recent`` results."""
    older = positions[: max(0, len(positions) - keep_recent)]
    cleared: set[Position] = set()
    batch: list[Position] = []
    size = 0
    for pos in older:
        batch.append(pos)
        size += blocks_tokens([messages[pos[0]].content[pos[1]]])
        if size >= batch_tokens:
            cleared.update(batch)
            batch = []
            size = 0
    return cleared


def relief_cutoff(messages: Sequence[Message], budget: int) -> int:
    """Messages at or past the returned index are the protected tail; the
    newest message always is."""
    acc = 0
    cutoff = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        acc += messages_tokens([messages[i]])
        if acc > budget:
            cutoff = i + 1
            break
        cutoff = i
    if messages:
        cutoff = min(cutoff, len(messages) - 1)
    return cutoff


def select(
    messages: Sequence[Message],
    *,
    is_clearable: Callable[[str], bool],
    size: int,
    valves: Valves,
    real_baseline: int,
    estimated: int,
) -> list[str]:
    """Call ids of the results the open valves clear, in history order.

    ``size`` is ``max(estimate of the uncleared request, last real input
    count)``; ``real_baseline`` / ``estimated`` give the density that turns
    the real-token tail budget into estimate units.
    """
    relief_open = size >= valves.usable
    micro_open = (valves.keep_recent is not None
                  and size >= valves.micro_fraction * valves.usable)
    if not relief_open and not micro_open:
        return []
    positions = clearable_positions(messages, is_clearable)
    micro = (micro_batches(messages, positions, valves.keep_recent or 0, valves.batch_tokens)
             if micro_open else set())
    cutoff = 0
    if relief_open:
        budget = int(valves.tail_budget / density(real_baseline, estimated))
        cutoff = relief_cutoff(messages, budget)
    ids: list[str] = []
    for pos in positions:
        if pos[0] < cutoff or pos in micro:
            block = messages[pos[0]].content[pos[1]]
            assert isinstance(block, ToolResult)
            if worth_clearing(block):
                ids.append(block.call_id)
    return ids


def apply_cleared(messages: Sequence[Message], ids: Collection[str]) -> list[Message]:
    """``messages`` with each result in ``ids`` replaced by the marker
    (``is_error`` kept, so the call still reads as failed or not)."""
    if not ids:
        return list(messages)
    out: list[Message] = []
    for m in messages:
        if not any(isinstance(b, ToolResult) and b.call_id in ids for b in m.content):
            out.append(m)
            continue
        blocks = [
            ToolResult(call_id=b.call_id, content=[Text(text=CLEARED_MARKER)],
                       is_error=b.is_error)
            if isinstance(b, ToolResult) and b.call_id in ids and worth_clearing(b) else b
            for b in m.content
        ]
        out.append(Message(role=m.role, content=blocks, origin=m.origin))
    return out


def raw_boundary(history: Sequence[Message], ids: Collection[str]) -> int:
    """One past the newest message of ``history`` holding a result in ``ids``
    (the range ``RecallHistory`` can page cleared outputs back from)."""
    if not ids:
        return 0
    for i in range(len(history) - 1, -1, -1):
        for b in history[i].content:
            if isinstance(b, ToolResult) and b.call_id in ids:
                return i + 1
    return 0
