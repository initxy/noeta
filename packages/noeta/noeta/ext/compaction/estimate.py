"""A deterministic, provider-neutral token estimate (0.6's ``chars/4``).

Deliberately not a tokenizer: a tokenizer drifts across library versions, so
the same history would estimate differently on two hosts and a resumed task
would clear or compact differently. The number is a budgeting unit, not a
billed count; real counts enter through the newest reply's recorded usage.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from noeta.types import (
    Block,
    Image,
    Message,
    SystemPart,
    Text,
    Thinking,
    ToolResult,
    ToolSpec,
    ToolUse,
)

#: ~4 characters per token. Changing it changes every estimate.
CHARS_PER_TOKEN = 4
#: Flat overhead per message for the role markers a provider adds.
PER_MESSAGE_OVERHEAD_TOKENS = 1
#: An image's estimate (Claude Code's per-image figure). 0.6 estimated an
#: image by the repr of its content reference; a 1.0 image carries its bytes,
#: whose repr would count megabytes.
IMAGE_TOKENS = 2_000


def text_tokens(text: str) -> int:
    """``ceil(len / 4)``: any non-empty text costs at least one token."""
    n = len(text)
    if n <= 0:
        return 0
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def block_tokens(block: Block) -> int:
    if isinstance(block, (Text, Thinking)):
        return text_tokens(block.text)
    if isinstance(block, ToolUse):
        return text_tokens(block.name + _canonical(block.input))
    if isinstance(block, ToolResult):
        images = sum(1 for b in block.content if isinstance(b, Image))
        return text_tokens(block.text) + images * IMAGE_TOKENS
    if isinstance(block, Image):
        return IMAGE_TOKENS
    return text_tokens(str(block))  # pragma: no cover - every block kind is above


def blocks_tokens(blocks: Iterable[Block]) -> int:
    return sum(block_tokens(b) for b in blocks)


def messages_tokens(messages: Iterable[Message]) -> int:
    return sum(PER_MESSAGE_OVERHEAD_TOKENS + blocks_tokens(m.content) for m in messages)


def prefix_tokens(system: Sequence[SystemPart], tools: Sequence[ToolSpec]) -> int:
    """The system prompt and tool schemas: the part of a request that is not
    the conversation."""
    total = sum(text_tokens(p.text) for p in system)
    for t in tools:
        total += text_tokens(t.name + t.description + _canonical(t.input_schema))
    return total
