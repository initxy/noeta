"""Provider-neutral model spec catalog + pricing.

Covers the :class:`ModelSpec` shape (real model-id / context window / output
cap / per-MTok prices / capability flags), the pure ``price(model_id, usage)``
function that turns a typed :class:`Usage` into USD, and the alias→real-id
table. Neutrality is the invariant worth guarding: one dataclass holds both
Anthropic and OpenAI rows, and **no** vendor wire key
(``cache_creation_input_tokens`` / ``total_tokens`` / ``prompt_tokens``) may
leak in as a field name, or the cost math would fork per vendor.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from noeta.protocols.messages import Usage
from noeta.builtins.providers.impl import catalog as catalog_mod
from noeta.builtins.providers.impl.catalog import (
    ALIASES,
    CATALOG,
    ModelSpec,
    price,
    resolve_alias,
    spec_for,
)


# ---------------------------------------------------------------------------
# ModelSpec shape
# ---------------------------------------------------------------------------


def test_spec_for_alias_resolves_real_anthropic_model() -> None:
    """``spec_for`` accepts the resolved real model-id and returns a full
    spec carrying context window / output cap / prices / reasoning flag."""
    real = resolve_alias("opus")
    spec = spec_for(real)
    assert isinstance(spec, ModelSpec)
    assert spec.real_model_id == real
    assert spec.context_window > 0
    assert spec.max_output_tokens > 0
    assert spec.input_price_per_mtok > 0
    assert spec.output_price_per_mtok > 0
    assert isinstance(spec.is_reasoning, bool)


def test_spec_for_accepts_real_model_id_directly() -> None:
    """The catalog is keyed by real model-id, not by alias."""
    spec = spec_for("claude-opus-4-8")
    assert spec.real_model_id == "claude-opus-4-8"


def test_modelspec_is_frozen() -> None:
    spec = spec_for("claude-opus-4-8")
    with pytest.raises(Exception):
        spec.input_price_per_mtok = 0.0  # type: ignore[misc]


def test_provider_neutral_field_names_no_vendor_wire_keys() -> None:
    """The spec shape must not pin any vendor wire key."""
    field_names = set(ModelSpec.__dataclass_fields__)  # type: ignore[attr-defined]
    forbidden = {
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }
    assert field_names.isdisjoint(forbidden)


def test_catalog_holds_both_anthropic_and_openai_rows() -> None:
    """One dataclass holds both vendors -- proves provider neutrality structurally."""
    anthropic_spec = spec_for("claude-opus-4-8")
    openai_spec = spec_for("gpt-4o")
    assert isinstance(anthropic_spec, ModelSpec)
    assert isinstance(openai_spec, ModelSpec)
    assert anthropic_spec.real_model_id != openai_spec.real_model_id


# ---------------------------------------------------------------------------
# supports_vision capability flag
# ---------------------------------------------------------------------------


def test_supports_vision_defaults_to_false() -> None:
    """``supports_vision`` is opt-in (same nature as ``is_reasoning``): an
    unmarked model must read as non-vision so the adapter's vision guard blocks
    an image request rather than shipping it to a text-only model."""
    spec = ModelSpec(
        real_model_id="x",
        context_window=1,
        max_output_tokens=1,
        input_price_per_mtok=0.0,
        output_price_per_mtok=0.0,
        cache_read_price_per_mtok=0.0,
        cache_write_price_per_mtok=0.0,
    )
    assert spec.supports_vision is False


def test_existing_text_only_rows_are_not_vision() -> None:
    """The text-only rows must stay on the non-vision side of the guard."""
    for model_id in ("gpt-4o", "gpt-4o-mini"):
        assert spec_for(model_id).supports_vision is False


def test_claude_rows_are_vision_capable() -> None:
    """Modern Claude models are multimodal: opus / sonnet / haiku all carry
    supports_vision=True so the Anthropic adapter's vision guard recognises they
    can read images (otherwise an image chain would always degrade/block)."""
    for model_id in (
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ):
        assert spec_for(model_id).supports_vision is True


def test_gpt_5_4_2026_03_05_entry_present_with_vision_and_reasoning() -> None:
    """A reasoning + vision row: both flags set, window and output cap
    positive, so the guards and the budget math have real numbers to work
    with."""
    spec = spec_for("gpt-5.4-2026-03-05")
    assert spec.real_model_id == "gpt-5.4-2026-03-05"
    assert spec.is_reasoning is True
    assert spec.supports_vision is True
    assert spec.context_window > 0
    assert spec.max_output_tokens > 0


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------


def test_aliases_cover_opus_sonnet_haiku() -> None:
    assert set(ALIASES) >= {"opus", "sonnet", "haiku"}
    for alias in ("opus", "sonnet", "haiku"):
        assert resolve_alias(alias) == ALIASES[alias]
        assert spec_for(resolve_alias(alias)).real_model_id == ALIASES[alias]


def test_aliases_point_at_the_current_generation() -> None:
    """``opus`` / ``sonnet`` follow the vendor forward so a host that binds the
    friendly name gets the current model without a code change. ``haiku`` stays
    on 4.5 — there is no haiku 5."""
    assert ALIASES["opus"] == "claude-opus-5"
    assert ALIASES["sonnet"] == "claude-sonnet-5"
    assert ALIASES["haiku"] == "claude-haiku-4-5"


def test_superseded_rows_stay_addressable_by_full_id() -> None:
    """Repointing an alias must not retire the generation it left behind: a
    caller who deliberately pinned ``claude-opus-4-8`` keeps its real window,
    output cap and prices."""
    for model_id in ("claude-opus-4-8", "claude-sonnet-4-6"):
        spec = spec_for(model_id)
        assert spec.real_model_id == model_id
        assert spec.context_window > 0
        assert spec.is_priced


def test_5_generation_rows_carry_window_output_and_prices() -> None:
    """The new rows are complete: guards, budget math and cost accounting all
    read from them, and a half-filled row degrades each of those silently."""
    for model_id in ("claude-opus-5", "claude-sonnet-5"):
        spec = spec_for(model_id)
        assert spec.real_model_id == model_id
        assert spec.context_window == 1_000_000
        assert spec.max_output_tokens == 128_000
        assert spec.is_reasoning is True
        assert spec.supports_vision is True
        assert spec.is_priced
        # Anthropic's published tiering: read ≈ 0.1x input, write ≈ 1.25x.
        assert spec.cache_read_price_per_mtok == pytest.approx(
            spec.input_price_per_mtok * 0.1
        )
        assert spec.cache_write_price_per_mtok == pytest.approx(
            spec.input_price_per_mtok * 1.25
        )


def test_resolve_alias_passes_through_unknown_value() -> None:
    """A value that is not an alias is returned unchanged, so a real model-id
    or the stub model passes through; the driver allowlist gates it
    separately."""
    assert resolve_alias("claude-opus-4-8") == "claude-opus-4-8"
    assert resolve_alias("stub-model") == "stub-model"


# ---------------------------------------------------------------------------
# Pricing math
# ---------------------------------------------------------------------------


def test_price_one_mtok_input_equals_input_price() -> None:
    spec = spec_for("claude-opus-4-8")
    cost = price("claude-opus-4-8", Usage(uncached=1_000_000, output=0))
    assert cost == pytest.approx(spec.input_price_per_mtok)


def test_price_one_mtok_output_equals_output_price() -> None:
    spec = spec_for("claude-opus-4-8")
    cost = price("claude-opus-4-8", Usage(uncached=0, output=1_000_000))
    assert cost == pytest.approx(spec.output_price_per_mtok)


def test_price_mixed_usage_sums_each_component() -> None:
    spec = spec_for("claude-opus-4-8")
    usage = Usage(uncached=500_000, output=200_000)
    expected = (
        500_000 / 1_000_000 * spec.input_price_per_mtok
        + 200_000 / 1_000_000 * spec.output_price_per_mtok
    )
    assert price("claude-opus-4-8", usage) == pytest.approx(expected)


def test_price_cache_read_and_write_priced_distinctly() -> None:
    """Cache read is cheaper than uncached input and cache write is more
    expensive, so the same token count must cost differently depending on which
    bucket the GovernanceState split put it in."""
    spec = spec_for("claude-opus-4-8")
    read_cost = price("claude-opus-4-8", Usage(cache_read=1_000_000))
    write_cost = price("claude-opus-4-8", Usage(cache_write=1_000_000))
    uncached_cost = price("claude-opus-4-8", Usage(uncached=1_000_000))
    assert read_cost == pytest.approx(spec.cache_read_price_per_mtok)
    assert write_cost == pytest.approx(spec.cache_write_price_per_mtok)
    # cache read < uncached input < cache write (Anthropic economics)
    assert read_cost < uncached_cost < write_cost


def test_price_empty_usage_is_zero() -> None:
    assert price("claude-opus-4-8", Usage()) == 0.0


def test_spec_for_unknown_model_raises_keyerror() -> None:
    """``spec_for`` stays strict — it hands back a spec or nothing, and every
    caller that can survive a miss goes through ``find_spec`` instead."""
    with pytest.raises(KeyError):
        spec_for("totally-unknown-model")


# ---------------------------------------------------------------------------
# Unknown / unpriced models: warn, never silently charge $0
# ---------------------------------------------------------------------------


def _price_warnings(
    caplog: pytest.LogCaptureFixture, model: str, usage: Usage
) -> list[str]:
    """Price ``model`` twice with a cleared warn-once slot; return the lines."""
    catalog_mod._WARNED.discard(("pricing", resolve_alias(model)))
    caplog.clear()
    with caplog.at_level(
        logging.WARNING, logger="noeta.builtins.providers.impl.catalog"
    ):
        price(model, usage)
        price(model, usage)
    return [r.getMessage() for r in caplog.records if model in r.getMessage()]


def test_price_uncatalogued_model_warns_once_and_charges_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """0.0 keeps the return type and the fold arithmetic stable — the WARNING
    is what stops "unknown" from being indistinguishable from "free". Once per
    model id, not once per round-trip, or a long session drowns in it."""
    usage = Usage(uncached=1_000_000, output=1_000_000)
    assert price("totally-unknown-model", usage) == 0.0
    lines = _price_warnings(caplog, "totally-unknown-model", usage)
    assert len(lines) == 1
    assert "not in the catalog" in lines[0]


def test_price_catalogued_but_unpriced_row_warns_once_and_charges_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gateway rows carry a window and a vision bit but no rate card. They
    used to fake $0.00, which reads as "this model is free"; absent prices are
    now a distinct state that takes the same warn-and-zero path as an
    uncatalogued id."""
    spec = spec_for("gpt-5.5-2026-04-24")
    assert spec.is_priced is False
    assert spec.input_price_per_mtok is None
    usage = Usage(uncached=1_000_000, output=1_000_000)
    assert price("gpt-5.5-2026-04-24", usage) == 0.0
    lines = _price_warnings(caplog, "gpt-5.5-2026-04-24", usage)
    assert len(lines) == 1
    assert "without published prices" in lines[0]


def test_priced_rows_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """The warning is scoped to the unknown states: a fully-priced row must
    stay silent, or the signal is worthless."""
    usage = Usage(uncached=1_000_000, output=1_000_000)
    lines = _price_warnings(caplog, "claude-opus-5", usage)
    assert lines == []


def test_every_catalogued_row_is_either_priced_or_wholly_unpriced() -> None:
    """``is_priced`` is a row-level state, so a half-filled row (two rates set,
    two absent) would charge 0.0 while looking priced at a glance. None exist."""
    for model_id, spec in CATALOG.items():
        rates = (
            spec.input_price_per_mtok,
            spec.output_price_per_mtok,
            spec.cache_read_price_per_mtok,
            spec.cache_write_price_per_mtok,
        )
        assert all(r is None for r in rates) or all(r is not None for r in rates), (
            f"{model_id} is partially priced"
        )


# ---------------------------------------------------------------------------
# Import diet — what the catalog is allowed to reach for
# ---------------------------------------------------------------------------


def test_catalog_module_import_diet() -> None:
    """The catalog keeps a narrow diet: protocols, plus the two kernel modules
    ``derive_compaction_config`` reaches downward for — ``noeta.execution.builder``
    for the ``CompactionConfig`` type and ``noeta.context.composer`` for the
    composer version. Built-in→kernel is the allowed direction; anything else
    appearing here is a smell."""
    import noeta.builtins.providers.impl.catalog as catalog_mod

    allowed = (
        "from noeta.protocols",
        "import noeta.protocols",
        "from noeta.execution.builder import",
        "from noeta.context.composer import",
    )
    src = inspect.getsource(catalog_mod)
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("from noeta") or line.startswith("import noeta"):
            assert line.startswith(allowed), (
                f"catalog imports an unexpected noeta module: {line}"
            )
