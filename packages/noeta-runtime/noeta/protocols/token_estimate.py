"""Deterministic, provider-neutral token estimation (③ D-3d, L0).

A cheap ``chars/4`` heuristic that approximates how many tokens a
Noeta-shape :class:`~noeta.protocols.messages.Message` / Block surface
costs. It is **not** a real tokenizer on purpose:

* a real tokenizer (tiktoken / a vendor SDK) drifts across library and
  vocabulary versions, so the same history would estimate differently on
  two runs / two hosts — that breaks the Composer's determinism: a
  non-byte-equal View shifts the prompt prefix bytes (so the stable-prefix
  prompt cache misses) and a resume re-derives a different View;
* a heuristic on the canonical text surface is reproducible forever: the
  same input always yields the same integer.

Provider neutrality: the estimator reads only Noeta-shape
fields (``TextBlock.text`` / ``ToolUseBlock.tool_name`` + ``arguments`` /
``ToolResultBlock.output`` …); it never touches a vendor wire shape. The
returned number is a *budgeting* unit, not a billed token count — it is
used by ③ to decide a tail-window cutoff and a compaction trigger, both
of which only need a stable, monotone proxy for "how big is this".

Shared with ① (cost accounting reads the recorded :class:`Usage`, but the
*pre-call* size estimate — when no provider count exists yet — uses this
same neutral function), so it lives at L0 next to ``messages``.

import-linter: ``noeta.protocols`` may import only stdlib + sibling
protocols modules. This module imports ``noeta.protocols.messages`` only.
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


#: Heuristic divisor: ~4 characters per token (the well-worn English
#: rule of thumb). Kept as a module constant so the unit is documented and
#: a single place rotates it should the heuristic ever be retuned (a retune
#: would change estimates → it must be coordinated with a Composer version
#: bump, exactly like any other determinism-affecting change).
_CHARS_PER_TOKEN = 4

#: Flat per-message overhead (role markers / delimiters a provider adds
#: around every turn). A small constant so a many-short-message history is
#: not estimated as ~free. Deterministic, provider-neutral.
_PER_MESSAGE_OVERHEAD_TOKENS = 1


def estimate_text_tokens(text: str) -> int:
    """Estimate the token cost of a raw string as ``ceil(len / 4)``.

    ``""`` → 0; ``1..4`` chars → 1; ``5..8`` → 2; … The ceiling means any
    non-empty text costs at least one token. Deterministic and pure.
    """
    n = len(text)
    if n <= 0:
        return 0
    return (n + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _block_text(block: Block) -> str:
    """The canonical text surface of one Block, for estimation only.

    Text/Thinking contribute their visible text. A ToolUse contributes its
    tool name plus the canonical (sorted-key) bytes of its arguments so a
    bigger argument payload estimates bigger, reproducibly. A ToolResult
    contributes the stringified output (a nullified output → empty string →
    near-zero, which is the whole point of prune).
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
    # Unknown block kind (forward-compat): fall back to its repr surface so
    # it still contributes a deterministic, non-negative estimate.
    return str(block)


def estimate_blocks_tokens(blocks: Iterable[Block]) -> int:
    """Sum :func:`estimate_text_tokens` over each block's text surface."""
    return sum(estimate_text_tokens(_block_text(b)) for b in blocks)


def estimate_messages_tokens(messages: Iterable[Message]) -> int:
    """Estimate the token cost of a message history (③ trigger / budget).

    Each message costs its blocks' text estimate plus a flat per-message
    overhead. Deterministic and provider-neutral, so the Composer can use
    it inside a pure compose call without breaking byte-equal.
    """
    total = 0
    for m in messages:
        total += _PER_MESSAGE_OVERHEAD_TOKENS + estimate_blocks_tokens(m.content)
    return total
