"""MessageSelection prune/summarize provenance (③ D-3f).

③ extends the event-only :class:`MessageSelection` provenance with two
additive counters: ``pruned`` (tool outputs nullified outside the
protected tail window) and ``summarized`` (messages collapsed into a
summary). Both are optional with byte-safe defaults so old recordings
restore cleanly via ``.get`` — additive, no ``schema_version`` bump
(the MS1 convention). The field is NOT part of ``request_ref`` (it is
observability metadata), so it carries zero byte-equal cost on the LLM
request line.
"""

from __future__ import annotations

from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.events import MessageSelection


def test_new_fields_default_to_zero() -> None:
    sel = MessageSelection(
        strategy="tail_window", candidates=3, selected=3, dropped=0, limit=50
    )
    assert sel.pruned == 0
    assert sel.summarized == 0


def test_canonical_round_trip_preserves_new_fields() -> None:
    sel = MessageSelection(
        strategy="prune",
        candidates=10,
        selected=10,
        dropped=0,
        limit=50,
        pruned=4,
        summarized=0,
    )
    restored = from_canonical_bytes(to_canonical_bytes(sel))
    assert restored == sel
    assert restored.pruned == 4


def test_old_shape_restores_with_defaults() -> None:
    """An old (pre-③) canonical body has no pruned/summarized keys; the
    registered restorer must default them rather than crash."""
    from noeta.protocols.events import _restore_message_selection

    old_body = {
        "strategy": "tail_window",
        "candidates": 5,
        "selected": 5,
        "dropped": 0,
        "limit": 50,
    }
    sel = _restore_message_selection(old_body)
    assert sel.pruned == 0
    assert sel.summarized == 0
    assert sel.strategy == "tail_window"


def test_sqlite_restorer_defaults_old_selection_on_request_started() -> None:
    """The sqlite LLMRequestStarted restorer rebuilds a dict-shaped
    selection (old recording) with the new fields defaulted."""
    from noeta.storage.sqlite.eventlog import (
        _restore_llm_request_started_payload,
    )

    body = {
        "call_id": "c1",
        "model": "m",
        "request_ref": None,
        "input_tokens": 0,
        "selection": {
            "strategy": "tail_window",
            "candidates": 5,
            "selected": 5,
            "dropped": 0,
            "limit": 50,
        },
    }
    payload = _restore_llm_request_started_payload(body)
    assert payload.selection is not None
    assert payload.selection.pruned == 0
    assert payload.selection.summarized == 0


def test_prune_strategy_counts_are_self_consistent() -> None:
    sel = MessageSelection(
        strategy="prune",
        candidates=12,
        selected=12,
        dropped=0,
        limit=50,
        pruned=3,
        summarized=0,
    )
    # prune nullifies in place (selected unchanged); dropped is for the
    # tail-window cut, pruned is for the nullified tool outputs.
    assert sel.selected == sel.candidates
    assert sel.pruned == 3
