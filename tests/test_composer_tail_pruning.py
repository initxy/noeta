"""``_clear_tool_outputs`` and ``_prune_tail`` edge cases that mislead the model.

``_clear_tool_outputs`` must treat every falsy output (``""`` / ``None`` / ``[]``
/ ``{}`` / ``0`` / ``False``) as already-empty: wrapping one in an "output
cleared" marker both lies to the model and forces a redundant ContentStore
write. ``_prune_tail`` must always keep the single newest tool result intact,
even when that one message alone exceeds ``tail_token_budget`` — clearing it
elides the freshest context exactly when it matters most.
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
# falsy non-string outputs are already-empty, never wrapped / written.
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
# the newest tool result is never cleared by the tail-budget rule.
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
