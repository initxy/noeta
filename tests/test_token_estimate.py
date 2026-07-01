"""Deterministic, provider-neutral token estimation (③ D-3d).

The estimator is a cheap chars/4-style heuristic — NOT a real tokenizer
(those drift across vendor/library versions, breaking the stable prompt
prefix the cross-host prompt cache depends on). It is shared by ③ (compaction trigger / tail-window budget)
and is provider-neutral: it counts the canonical text surface
of Noeta-shape :class:`Message` / :class:`Block`, never a vendor wire shape.
"""

from __future__ import annotations

from noeta.protocols.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.token_estimate import (
    estimate_blocks_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
)


def test_estimate_text_is_chars_over_four_ceil() -> None:
    # 0 chars → 0 tokens; 1..4 chars → 1 token; 5..8 → 2 tokens (ceil).
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("a") == 1
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("abcde") == 2
    assert estimate_text_tokens("a" * 8) == 2
    assert estimate_text_tokens("a" * 9) == 3


def test_estimate_is_deterministic_same_input_same_output() -> None:
    text = "the quick brown fox jumps over the lazy dog " * 7
    assert estimate_text_tokens(text) == estimate_text_tokens(text)


def test_estimate_blocks_sums_text_surface() -> None:
    blocks = [
        TextBlock(text="hello world"),  # 11 chars → 3
        ThinkingBlock(text="abcd"),  # 4 chars → 1
    ]
    assert estimate_blocks_tokens(blocks) == 4


def test_estimate_blocks_counts_tool_use_name_and_args() -> None:
    block = ToolUseBlock(
        call_id="c1", tool_name="read_file", arguments={"path": "/a/b.py"}
    )
    # Deterministic: name text + canonical-args text, both via chars/4.
    n = estimate_blocks_tokens([block])
    assert n > 0
    # idempotent
    assert estimate_blocks_tokens([block]) == n


def test_estimate_blocks_counts_tool_result_output_text() -> None:
    big = ToolResultBlock(call_id="c1", output="x" * 400, success=True)
    nulled = ToolResultBlock(call_id="c1", output="", success=True)
    assert estimate_blocks_tokens([big]) > estimate_blocks_tokens([nulled])
    # A nulled tool result is cheap (close to its envelope cost).
    assert estimate_blocks_tokens([nulled]) <= 2


def test_estimate_messages_sums_blocks_with_role_overhead() -> None:
    msgs = [
        Message(role="user", content=[TextBlock(text="abcd")]),
        Message(role="assistant", content=[TextBlock(text="abcd")]),
    ]
    one = estimate_messages_tokens([msgs[0]])
    both = estimate_messages_tokens(msgs)
    assert both == 2 * one
    # Per-message overhead means a message costs more than its bare text.
    assert one >= estimate_text_tokens("abcd")
