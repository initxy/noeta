"""Regression tests for two ``_prune_tail`` / ``_clear_tool_outputs`` bugs.

#13 (composer.py:662): ``_clear_tool_outputs`` only treated the literal
empty string ``""`` as already-empty, so a tool that legitimately returned a
falsy non-string value (``None`` / ``[]`` / ``{}`` / ``0`` / ``False``) was
rewritten into a misleading "output cleared" marker AND triggered a redundant
ContentStore write. The docstring promises already-empty outputs are skipped.

#31 (composer.py:547): the protected tail window cleared even the single
newest tool result whenever that one message alone exceeded
``tail_token_budget`` — eliding the freshest context exactly when it mattered
most. The newest message must always be kept intact.
"""

from __future__ import annotations

from noeta.context.composer import (
    ThreeSegmentComposer,
    _clear_tool_outputs,
    _is_cleared_marker,
)
from noeta.protocols.messages import Message, ToolResultBlock
from noeta.storage.memory import InMemoryContentStore


def _result_msg(output: object) -> Message:
    return Message(
        role="tool",
        content=[ToolResultBlock(call_id="c1", output=output, success=True)],
    )


# ---------------------------------------------------------------------------
# #13 — falsy non-string outputs are already-empty, never wrapped / written.
# ---------------------------------------------------------------------------


def test_clear_skips_empty_equivalent_outputs_without_store_write() -> None:
    puts: list[object] = []

    def put_full(output: object) -> str:
        puts.append(output)
        return "HASH"

    for empty in ("", None, [], {}, 0, False, 0.0):
        msg = _result_msg(empty)
        out, cleared_refs = _clear_tool_outputs(msg, put_full)
        assert cleared_refs == [], f"{empty!r} should be treated as already-empty"
        # message returned verbatim, output untouched (no cleared-marker).
        block = out.content[0]
        assert isinstance(block, ToolResultBlock)
        assert block.output == empty
        assert not _is_cleared_marker(block.output)
    # No redundant ContentStore writes for any empty-equivalent output.
    assert puts == []


def test_clear_still_wraps_nonempty_output() -> None:
    puts: list[object] = []

    def put_full(output: object) -> str:
        puts.append(output)
        return "HASH"

    msg = _result_msg("real output")
    out, cleared_refs = _clear_tool_outputs(msg, put_full)
    assert cleared_refs == ["HASH"]  # one ref returned for the plan
    block = out.content[0]
    assert isinstance(block, ToolResultBlock)
    assert _is_cleared_marker(block.output)
    assert puts == ["real output"]


# ---------------------------------------------------------------------------
# #31 — the newest tool result is never cleared by the tail-budget rule.
# ---------------------------------------------------------------------------


def _composer(budget: int) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="sys",
        tools={},
        content_store=InMemoryContentStore(),
        tail_token_budget=budget,
    )


def test_newest_tool_result_survives_oversized_tail_budget() -> None:
    # A single newest message whose tool output alone blows the tiny budget.
    big = "x" * 4000
    messages = [_result_msg(big)]
    composer = _composer(budget=10)
    out, _selected, _dropped, _cleared = composer._prune_tail(messages)
    block = out[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    # Freshest result kept intact, NOT swept into a cleared-marker.
    assert block.output == big
    assert not _is_cleared_marker(block.output)


def test_older_results_still_cleared_but_newest_kept() -> None:
    big = "x" * 4000
    older = _result_msg(big)
    newest = _result_msg(big)
    composer = _composer(budget=10)
    out, _selected, _dropped, _cleared = composer._prune_tail([older, newest])
    older_block = out[0].content[0]
    newest_block = out[1].content[0]
    assert isinstance(older_block, ToolResultBlock)
    assert isinstance(newest_block, ToolResultBlock)
    # Older message is pruned to a marker; newest stays verbatim.
    assert _is_cleared_marker(older_block.output)
    assert newest_block.output == big


def test_prune_tail_empty_messages_is_noop() -> None:
    composer = _composer(budget=10)
    out, selected, dropped, cleared = composer._prune_tail([])
    assert out == []
    assert selected == []
    assert dropped == []
    assert cleared == []
