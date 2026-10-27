"""Auto-recall: what a user message brings back from the store.

Tier 1 (the message named the page) brings the body, within 4 KiB per body
and 16 KiB per message, else a pointer; tier 2 (a shared word, prose
overlap, a keyword) brings a pointer line; a named page brings its
``related`` neighbours as pointers; when nothing was named a judge model may
pick pointers. Pages already in the conversation, or excluded by the host,
are silent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Collection, Iterable

from noeta.core.helpers import estimate_tokens
from noeta.core.types import Message, ToolResult, ToolUse
from noeta.plugins.memory.index import (
    RECALL_BODY_MAX_BYTES,
    RECALL_TOTAL_MAX_BYTES,
    RecallHit,
)
from noeta.plugins.memory.matching import (
    DEFAULT_RECALL_MAX_HITS,
    MemoryEntries,
    match_memories_tiered,
)
from noeta.plugins.memory.store import MemoryStore

__all__ = [
    "Judge",
    "parse_judge_reply",
    "read_memory_names",
    "recall_memories",
    "render_judge_system",
]

#: ``(candidates, message text) -> names``; may be async.
Judge = Callable[[MemoryEntries, str], Awaitable[tuple[str, ...]]]


def read_memory_names(history: Iterable[Message], tool: str = "memory_read") -> frozenset[str]:
    """Pages the model loaded with a successful ``memory_read`` in
    ``history`` (the visible conversation, after compaction)."""
    requested: dict[str, str] = {}
    names: set[str] = set()
    for message in history:
        for block in message.content:
            if (isinstance(block, ToolUse) and block.name == tool
                    and isinstance(block.input.get("name"), str)):
                requested[block.id] = block.input["name"]
            elif (isinstance(block, ToolResult) and not block.is_error
                  and block.call_id in requested):
                names.add(requested[block.call_id])
    return frozenset(names)


async def recall_memories(
    store: MemoryStore,
    text: str,
    *,
    resident: Collection[str] = (),
    exclude: Collection[str] = (),
    judge: Judge | None = None,
    index_name: str = "index",
) -> tuple[RecallHit, ...]:
    """At most five hits, best first (see the module note)."""
    entries, updated = await asyncio.to_thread(store.index_snapshot)
    skip = frozenset(resident) | frozenset(exclude)
    summaries = {name: summary for name, summary, _t, _k in entries}

    def pointer(name: str) -> RecallHit:
        return RecallHit(name=name, text=summaries.get(name, ""), full=False)

    hits: list[RecallHit] = []
    named: list[str] = []
    spent = 0
    for name, by_name in match_memories_tiered(entries, text, exclude=skip):
        if by_name and name != index_name:
            body = await asyncio.to_thread(store.read, name)
            if body is None:
                continue
            size = len(body.encode("utf-8"))
            if size <= RECALL_BODY_MAX_BYTES and spent + size <= RECALL_TOTAL_MAX_BYTES:
                spent += size
                hits.append(RecallHit(name=name, text=body, full=True))
            else:
                hits.append(pointer(name))
        else:
            hits.append(pointer(name))
        if by_name:
            named.append(name)

    taken = {hit.name for hit in hits}
    room = DEFAULT_RECALL_MAX_HITS - len(hits)
    if named:
        for source in named:
            for other in await asyncio.to_thread(store.related, source):
                if room <= 0:
                    break
                if other in taken or other in skip or other not in summaries:
                    continue
                hits.append(pointer(other))
                taken.add(other)
                room -= 1
    elif judge is not None and room > 0:
        candidates = tuple(sorted(
            (e for e in entries if e[0] not in skip and e[0] not in taken),
            key=lambda e: updated.get(e[0], ""), reverse=True))
        if candidates:
            offered = {e[0] for e in candidates}
            picks = dict.fromkeys(n for n in await judge(candidates, text) if n in offered)
            hits.extend(pointer(n) for n in list(picks)[:room])
    return tuple(hits)


def render_judge_system(entries: MemoryEntries, instructions: str, *,
                        budget_tokens: int | None = None) -> str:
    """The judge's system text: instructions, then the index lines (with
    keyword aliases) that fit the budget, in the order given, name-sorted."""
    rendered: list[tuple[str, str]] = []
    for name, summary, mem_type, keywords in entries:
        label = f"{name} ({mem_type})" if mem_type else name
        line = f"- {label}: {summary}" if summary else f"- {label}"
        if keywords:
            line += f" [aliases: {keywords}]"
        rendered.append((name, line))
    if budget_tokens is not None:
        kept: list[tuple[str, str]] = []
        remaining = budget_tokens
        for name, line in rendered:
            cost = estimate_tokens(line + "\n")
            if cost <= remaining:
                remaining -= cost
                kept.append((name, line))
        rendered = kept
    lines = [instructions.strip(), "", "Memory index:"]
    lines.extend(line for _name, line in sorted(rendered))
    return "\n".join(lines)


def parse_judge_reply(reply: str, entries: MemoryEntries) -> tuple[str, ...]:
    """The first ``[...]`` span as JSON, kept to names that exist, deduped,
    at most five. Anything unparseable is ``()``."""
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end <= start:
        return ()
    try:
        picked = json.loads(reply[start: end + 1])
    except ValueError:
        return ()
    if not isinstance(picked, list):
        return ()
    known = {name for name, _s, _t, _k in entries}
    out: list[str] = []
    for item in picked:
        if isinstance(item, str) and item in known and item not in out:
            out.append(item)
        if len(out) >= DEFAULT_RECALL_MAX_HITS:
            break
    return tuple(out)
