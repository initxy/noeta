"""Provider-neutral model spec catalog + pricing.

Covers the Noeta-shape :class:`ModelSpec` (real model-id / context window /
output cap / per-MTok prices / reasoning flag), the ``price(model_id,
usage)`` pure function that turns a typed :class:`Usage` into USD, and the
alias->real-id table. The catalog is provider-neutral: one dataclass holds both Anthropic and
OpenAI rows, and **no** vendor wire key
(``cache_creation_input_tokens`` / ``total_tokens`` / ``prompt_tokens``)
appears as a field name.
"""

from __future__ import annotations

import inspect

import pytest

from noeta.protocols.messages import Usage
from noeta.providers.catalog import (
    ALIASES,
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
    """``supports_vision``
    is an opt-in capability flag (same nature as ``is_reasoning``), default
    False. A model not explicitly marked as vision is treated as non-vision; an
    image request hitting it must trip the flag (the vision guard relies on it)."""
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
    """Text-only rows (gpt-4o / gpt-4o-mini) aren't marked vision ->
    supports_vision False. Adding the field must not change a non-vision row's
    semantics (red line: zero impact on old recordings)."""
    for model_id in ("gpt-4o", "gpt-4o-mini"):
        assert spec_for(model_id).supports_vision is False


def test_claude_rows_are_vision_capable() -> None:
    """Modern Claude models are multimodal: opus / sonnet / haiku all carry
    supports_vision=True so the Anthropic adapter's vision guard recognises they
    can read images (otherwise an image chain would always degrade/block)."""
    for model_id in ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"):
        assert spec_for(model_id).supports_vision is True


def test_gpt_5_4_2026_03_05_entry_present_with_vision_and_reasoning() -> None:
    """New model
    gpt-5.4-2026-03-05 is registered in the catalog with is_reasoning=True /
    supports_vision=True and positive window and output cap."""
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


def test_resolve_alias_passes_through_unknown_value() -> None:
    """A value that is not an alias is returned unchanged — so a real
    model-id or the stub model is left alone (driver allowlist still gates)."""
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
    """Cache read is cheaper than uncached input; cache write is more
    expensive -- the GovernanceState split must produce different cost for the
    same token count placed in different cache buckets."""
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


def test_price_unknown_model_raises_keyerror() -> None:
    """Unknown model-id semantics are fixed: spec_for / price raise KeyError."""
    with pytest.raises(KeyError):
        spec_for("totally-unknown-model")
    with pytest.raises(KeyError):
        price("totally-unknown-model", Usage(uncached=10))


# ---------------------------------------------------------------------------
# import-linter invariant (catalog may only import noeta.protocols + stdlib)
# ---------------------------------------------------------------------------


def test_catalog_module_only_imports_protocols_and_stdlib() -> None:
    import noeta.providers.catalog as catalog_mod

    src = inspect.getsource(catalog_mod)
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("from noeta") or line.startswith("import noeta"):
            assert line.startswith("from noeta.protocols") or line.startswith(
                "import noeta.protocols"
            ), f"catalog imports a non-protocols noeta module: {line}"
