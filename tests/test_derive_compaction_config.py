"""``derive_compaction_config`` must resolve a friendly ALIAS to its real
model-id before consulting the catalog (fix B).

The bug: ``derive_compaction_config('opus')`` called ``spec_for('opus')``
directly. ``'opus'`` is an alias, not a catalog id, so ``spec_for`` raised
``KeyError`` and the helper fell back to ``COMPACTION_OFF`` — silently
DISABLING compaction for the most common selector a host passes. The fix
resolves the alias first (``resolve_alias`` then ``spec_for`` on the real id).

Resume consistency is preserved because live (``AgentSessionRunner.prepare``)
and resume (``build_code_replay_inputs``) both call this same helper on the
same ``model`` string, so the derived knobs stay identical on both paths; and
genuinely un-catalogued ids (the test-only ``stub-model``, any future unpriced
id) still resolve to themselves and yield ``COMPACTION_OFF`` ⇒ legacy
behaviour ⇒ existing stub-model recordings resume identically.
"""

from __future__ import annotations

from noeta.execution.builder import COMPACTION_OFF, derive_compaction_config
from noeta.providers.catalog import resolve_alias, spec_for


def test_alias_opus_turns_compaction_on() -> None:
    cfg = derive_compaction_config("opus")
    # Compaction is ON: the context window is populated from the resolved spec.
    assert cfg.context_window is not None
    assert cfg.context_window == spec_for(resolve_alias("opus")).context_window
    assert cfg.tail_token_budget is not None
    assert cfg.tail_token_budget > 0
    assert cfg.max_output_tokens > 0
    assert cfg.composer_version != ""


def test_alias_matches_real_id_derivation() -> None:
    """An alias and its resolved real id must derive identical knobs — the
    alias is just a label, not a different model."""
    assert derive_compaction_config("opus") == derive_compaction_config(
        resolve_alias("opus")
    )


def test_real_id_still_works() -> None:
    cfg = derive_compaction_config("claude-sonnet-4-6")
    assert cfg.context_window is not None
    assert cfg.tail_token_budget is not None and cfg.tail_token_budget > 0


def test_tail_is_a_third_of_available_window() -> None:
    """Lock the default tail fraction: the
    verbatim tail is a THIRD of the usable window, not half. A smaller tail
    frees window — the summary keeps file paths and the model re-reads with
    ``read`` — at the cost of less recent verbatim fidelity.
    """
    from noeta.execution.builder import _COMPACTION_BUFFER_TOKENS

    spec = spec_for(resolve_alias("opus"))
    available = (
        spec.context_window - spec.max_output_tokens - _COMPACTION_BUFFER_TOKENS
    )
    cfg = derive_compaction_config("opus")
    assert cfg.tail_token_budget == available // 3


def test_stub_model_stays_off() -> None:
    """``stub-model`` is not in the catalog and is not an alias → resolves to
    itself → COMPACTION_OFF (byte-equal-safe legacy behaviour)."""
    assert derive_compaction_config("stub-model") == COMPACTION_OFF
    assert derive_compaction_config("stub-model").context_window is None


def test_unknown_model_stays_off() -> None:
    assert derive_compaction_config("totally-made-up-model") == COMPACTION_OFF
    assert derive_compaction_config("totally-made-up-model").context_window is None
