"""Clearing old tool results once the prompt cache has gone cold (Claude
Code's clearing after an idle gap): the results of older calls to tools
that can be called again (``Tool.clearable``) give way to a placeholder,
the newest ones kept. A cleared call stays cleared: its id is recorded, and
every request is rewritten from that record, so the request stays a
function of the folded log."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence

import msgspec

from noeta.core.types import Message, ModelRequest, Text, ToolResult
from noeta.plugins.compaction.estimate import block_tokens

#: What a cleared result reads as (Claude Code's text).
PLACEHOLDER = "[Old tool result content cleared]"
#: Claude Code's numbers: the newest clearable results kept, the least a
#: clearing must save, and the idle gap after which the cache is cold (a
#: little over the one-hour cache's life).
KEEP_RESULTS = 12
MIN_SAVED_TOKENS = 20_000
IDLE_SECONDS = 3_900
#: A result already saved to a file shows only a preview: never cleared.
_PERSISTED = "<persisted-output>"


def _placeholder(result: ToolResult) -> ToolResult:
    return msgspec.structs.replace(result, content=[Text(text=PLACEHOLDER)])


def to_clear(messages: Sequence[Message], clearable: Callable[[str], bool],
             cleared: Collection[str]) -> tuple[list[str], int]:
    """The ids of the results to clear now beyond the newest
    ``KEEP_RESULTS`` clearable ones, with the tokens that saves; nothing
    when it would save under ``MIN_SAVED_TOKENS``."""
    names = {u.id: u.name for m in messages if m.role == "assistant" for u in m.tool_uses}
    results = [b for m in messages if m.role == "user" for b in m.content
               if isinstance(b, ToolResult) and clearable(names.get(b.call_id, ""))]
    older = results[:-KEEP_RESULTS] if len(results) > KEEP_RESULTS else []
    picked = [r for r in older
              if r.call_id not in cleared and not r.text.startswith(_PERSISTED)]
    saved = sum(block_tokens(r) - block_tokens(_placeholder(r)) for r in picked)
    if saved < MIN_SAVED_TOKENS:
        return [], 0
    return [r.call_id for r in picked], saved


def cleared_request(request: ModelRequest, cleared: Collection[str]) -> ModelRequest:
    """``request`` with the results of the ``cleared`` calls replaced by the
    placeholder."""
    if not cleared:
        return request
    changed = False
    messages: list[Message] = []
    for m in request.messages:
        if m.role == "user" and any(isinstance(b, ToolResult) and b.call_id in cleared
                                    for b in m.content):
            content = [_placeholder(b) if isinstance(b, ToolResult)
                       and b.call_id in cleared else b for b in m.content]
            m = msgspec.structs.replace(m, content=content)
            changed = True
        messages.append(m)
    return msgspec.structs.replace(request, messages=messages) if changed else request


def uncleared(messages: Sequence[Message], originals: Sequence[Message],
              cleared: Collection[str]) -> list[Message]:
    """``messages`` with the results of the ``cleared`` calls given back as
    ``originals`` hold them: what a summary of the conversation reads."""
    real = {b.call_id: b for m in originals if m.role == "user" for b in m.content
            if isinstance(b, ToolResult) and b.call_id in cleared} if cleared else {}
    out: list[Message] = []
    for m in messages:
        if real and m.role == "user" and any(isinstance(b, ToolResult) and b.call_id in real
                                             for b in m.content):
            m = msgspec.structs.replace(m, content=[
                real[b.call_id] if isinstance(b, ToolResult) and b.call_id in real else b
                for b in m.content])
        out.append(m)
    return out
