"""Deterministic, provider-neutral token estimation.

A cheap ``chars/4`` heuristic over the Noeta-shape message surface, and
deliberately **not** a real tokenizer: tiktoken or a vendor SDK drifts across
library and vocabulary versions, so the same history would estimate differently
on two hosts, and a non-byte-equal View shifts the prompt prefix (missing the
stable-prefix cache) and makes a resume re-derive a different View. The
returned number is a budgeting unit, not a billed token count — its consumers
(tail-window cutoff, compaction trigger) need only a stable, monotone proxy for
"how big is this".
"""

from __future__ import annotations

from typing import Iterable

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.messages import (
    Block,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


#: Heuristic divisor: ~4 characters per token. Retuning it changes every
#: estimate, so it must be coordinated with a Composer version bump like any
#: other determinism-affecting change.
_CHARS_PER_TOKEN = 4

#: Flat per-message overhead for the role markers / delimiters a provider adds
#: around every turn, so a many-short-message history is not estimated as free.
_PER_MESSAGE_OVERHEAD_TOKENS = 1


def estimate_text_tokens(text: str) -> int:
    """Estimate the token cost of a raw string as ``ceil(len / 4)``.

    The ceiling means any non-empty text costs at least one token.
    """
    n = len(text)
    if n <= 0:
        return 0
    return (n + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _block_text(block: Block) -> str:
    """The canonical text surface of one Block, for estimation only.

    A ToolUse hashes its arguments through the canonical (sorted-key) encoder
    so a bigger payload estimates bigger reproducibly. A nullified ToolResult
    output collapses to near-zero, which is the point of prune.
    """
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, ThinkingBlock):
        return block.text
    if isinstance(block, ToolUseBlock):
        args_bytes = to_canonical_bytes(block.arguments)
        return block.tool_name + args_bytes.decode("utf-8", "ignore")
    if isinstance(block, ToolResultBlock):
        out = block.output
        return out if isinstance(out, str) else str(out)
    # An unknown block kind still has to contribute a deterministic,
    # non-negative estimate, so fall back to its repr surface.
    return str(block)


def estimate_blocks_tokens(blocks: Iterable[Block]) -> int:
    return sum(estimate_text_tokens(_block_text(b)) for b in blocks)


def estimate_messages_tokens(messages: Iterable[Message]) -> int:
    """Estimate the token cost of a message history.

    Deterministic and provider-neutral, so the Composer may call it inside a
    pure compose without breaking byte-equality.
    """
    total = 0
    for m in messages:
        total += _PER_MESSAGE_OVERHEAD_TOKENS + estimate_blocks_tokens(m.content)
    return total
