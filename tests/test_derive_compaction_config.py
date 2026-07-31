"""``derive_compaction_config`` resolves a model alias before the catalog lookup.

Hosts usually pass a friendly alias (``'opus'``) rather than a catalog id. An
alias that reaches ``spec_for`` unresolved raises ``KeyError``, and the helper
answers ``COMPACTION_OFF`` — silently disabling compaction for the most common
selector there is, with no error to notice. Resolving the alias first keeps
that failure mode closed, while a genuinely un-catalogued id still resolves to
itself and is legitimately left with compaction off.
"""

from __future__ import annotations

from noeta.client.parts import derive_compaction_config
from noeta.execution.builder import COMPACTION_OFF
from noeta.builtins.providers.impl.catalog import resolve_alias, spec_for


def test_alias_opus_turns_compaction_on() -> None:
    cfg = derive_compaction_config("opus")
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
    """Lock the default tail fraction: the verbatim tail is a THIRD of the
    usable window. A smaller tail frees context — the summary keeps file paths
    and the model re-reads with ``read`` — at the cost of less recent verbatim
    fidelity, so the ratio is a deliberate trade-off, not an arbitrary number.
    """
    from noeta.builtins.providers.impl.catalog import _COMPACTION_BUFFER_TOKENS

    spec = spec_for(resolve_alias("opus"))
    available = (
        spec.context_window - spec.max_output_tokens - _COMPACTION_BUFFER_TOKENS
    )
    cfg = derive_compaction_config("opus")
    assert cfg.tail_token_budget == available // 3


def test_stub_model_stays_off() -> None:
    """``stub-model`` is neither a catalog id nor an alias → resolves to
    itself → ``COMPACTION_OFF``, which is the correct answer when no context
    window is known."""
    assert derive_compaction_config("stub-model") == COMPACTION_OFF
    assert derive_compaction_config("stub-model").context_window is None


def test_unknown_model_stays_off() -> None:
    assert derive_compaction_config("totally-made-up-model") == COMPACTION_OFF
    assert derive_compaction_config("totally-made-up-model").context_window is None
