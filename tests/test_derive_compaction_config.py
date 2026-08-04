"""``derive_compaction_config`` resolves a model alias before the catalog lookup,
and answers an UNKNOWN model with conservative knobs rather than silence.

Hosts usually pass a friendly alias (``'opus'``) rather than a catalog id. An
alias that reached the lookup unresolved would miss the catalog and take the
unknown-model path for a model the table actually knows, so it is resolved
first.

An id that is genuinely un-catalogued used to answer ``COMPACTION_OFF``, which
disables compaction *and* the composer's tail prune with no signal at all —
for exactly the population most likely to need them (a gateway or self-hosted
model nobody wrote a row for). It now logs once and derives from a conservative
128 K / 16 K pair instead.
"""

from __future__ import annotations

import logging

from noeta.client.parts import derive_compaction_config
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


def test_unknown_model_still_compacts_with_conservative_window() -> None:
    """An un-catalogued id keeps compaction ON, sized from the conservative
    defaults. Off would disable the tail prune too, letting the history grow
    until the provider rejects it — the failure this path exists to prevent."""
    from noeta.builtins.providers.impl.catalog import (
        _COMPACTION_BUFFER_TOKENS,
        _UNKNOWN_MODEL_CONTEXT_WINDOW,
        _UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
    )

    cfg = derive_compaction_config("totally-made-up-model")
    assert cfg.context_window == _UNKNOWN_MODEL_CONTEXT_WINDOW
    assert cfg.max_output_tokens == _UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
    available = (
        _UNKNOWN_MODEL_CONTEXT_WINDOW
        - _UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
        - _COMPACTION_BUFFER_TOKENS
    )
    assert cfg.tail_token_budget == available // 3
    assert cfg.composer_version != ""


def test_stub_model_takes_the_same_conservative_path() -> None:
    """``stub-model`` is neither a catalog id nor an alias → resolves to itself
    → the unknown-model path, same as any other unregistered selector."""
    assert derive_compaction_config("stub-model") == derive_compaction_config(
        "totally-made-up-model"
    )


def test_unknown_model_logs_one_warning_per_model_id(
    caplog: object,
) -> None:
    """The whole point of the conservative default is that it is NOT silent —
    but a per-round-trip log line would be noise, so it warns once per id."""
    from noeta.builtins.providers.impl import catalog as catalog_mod

    model = "unknown-model-for-warn-once-test"
    catalog_mod._WARNED.discard(("compaction", model))
    with caplog.at_level(  # type: ignore[attr-defined]
        logging.WARNING, logger="noeta.builtins.providers.impl.catalog"
    ):
        derive_compaction_config(model)
        derive_compaction_config(model)
    records = [
        r
        for r in caplog.records  # type: ignore[attr-defined]
        if model in r.getMessage()
    ]
    assert len(records) == 1
    assert "not in the catalog" in records[0].getMessage()
