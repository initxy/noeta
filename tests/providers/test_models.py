"""The provider-neutral model catalog (:mod:`noeta.models`): shipped rows,
aliases, pricing, the operator extension overlay and the conservative limits
an unknown model gets."""

from __future__ import annotations

import dataclasses
import inspect
import logging
from typing import Any

import pytest

from noeta import models
from noeta.models import (
    ALIASES,
    CATALOG,
    UNKNOWN_MODEL_CONTEXT_WINDOW,
    UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
    ModelSpec,
    catalog_models,
    find_spec,
    model_family,
    model_limits,
    price,
    register_models,
    resolve_alias,
    spec_for,
)
from noeta.types import Usage

#: A neutral stand-in for an internal gateway routing name — carries no vendor
#: prefix, so nothing about it is inferable from the id.
GATEWAY_ID = "gateway/relay-large"


@pytest.fixture(autouse=True)
def _clean_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic overlay + warn-once state per test."""
    monkeypatch.setattr(models, "_EXTENSIONS", {})
    monkeypatch.setattr(models, "_EXTENSION_ALIASES", {})
    monkeypatch.setattr(models, "_WARNED", set())


def _gateway_spec(**overrides: Any) -> ModelSpec:
    base: dict[str, Any] = {
        "real_model_id": GATEWAY_ID,
        "context_window": 1_000_000,
        "max_output_tokens": 128_000,
        "is_reasoning": True,
        "supports_vision": True,
        "family": "anthropic",
    }
    base.update(overrides)
    return ModelSpec(**base)


def _price(spec: ModelSpec, field: str) -> float:
    value = getattr(spec, field)
    assert isinstance(value, float)
    return value


# ---------------------------------------------------------------------------
# ModelSpec shape
# ---------------------------------------------------------------------------


def test_spec_for_alias_resolves_real_anthropic_model() -> None:
    real = resolve_alias("opus")
    spec = spec_for(real)
    assert spec.real_model_id == real
    assert spec.context_window > 0
    assert spec.max_output_tokens > 0
    assert _price(spec, "input_price_per_mtok") > 0
    assert _price(spec, "output_price_per_mtok") > 0
    assert isinstance(spec.is_reasoning, bool)


def test_spec_for_accepts_real_model_id_directly() -> None:
    assert spec_for("claude-opus-4-8").real_model_id == "claude-opus-4-8"


def test_spec_for_and_find_spec_resolve_alias() -> None:
    assert spec_for("opus").real_model_id == ALIASES["opus"]
    assert spec_for("sonnet") is spec_for(ALIASES["sonnet"])
    assert find_spec("haiku") is CATALOG[ALIASES["haiku"]]


def test_modelspec_is_frozen() -> None:
    spec = spec_for("claude-opus-4-8")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.input_price_per_mtok = 0.0  # type: ignore[misc]


def test_provider_neutral_field_names_no_vendor_wire_keys() -> None:
    field_names = {f.name for f in dataclasses.fields(ModelSpec)}
    forbidden = {
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }
    assert field_names.isdisjoint(forbidden)


def test_catalog_holds_both_anthropic_and_openai_rows() -> None:
    assert model_family("claude-opus-4-8") == "anthropic"
    assert model_family("gpt-4o") == "openai"


# ---------------------------------------------------------------------------
# supports_vision
# ---------------------------------------------------------------------------


def test_supports_vision_defaults_to_false() -> None:
    """Opt-in, like ``is_reasoning``: an unmarked row reads as text-only."""
    spec = ModelSpec(real_model_id="x", context_window=1, max_output_tokens=1)
    assert spec.supports_vision is False


def test_gpt_4o_rows_are_vision_capable() -> None:
    for model_id in ("gpt-4o", "gpt-4o-mini"):
        assert spec_for(model_id).supports_vision is True


def test_claude_rows_are_vision_capable() -> None:
    for model_id in (
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ):
        assert spec_for(model_id).supports_vision is True


def test_gpt_5_4_2026_03_05_entry_present_with_vision_and_reasoning() -> None:
    spec = spec_for("gpt-5.4-2026-03-05")
    assert spec.real_model_id == "gpt-5.4-2026-03-05"
    assert spec.is_reasoning is True
    assert spec.supports_vision is True
    assert spec.context_window > 0
    assert spec.max_output_tokens > 0


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


def test_aliases_cover_opus_sonnet_haiku() -> None:
    assert set(ALIASES) >= {"opus", "sonnet", "haiku"}
    for alias in ("opus", "sonnet", "haiku"):
        assert resolve_alias(alias) == ALIASES[alias]
        assert spec_for(resolve_alias(alias)).real_model_id == ALIASES[alias]


def test_aliases_point_at_the_current_generation() -> None:
    assert ALIASES["opus"] == "claude-opus-5"
    assert ALIASES["sonnet"] == "claude-sonnet-5"
    assert ALIASES["haiku"] == "claude-haiku-4-5"


def test_superseded_rows_stay_addressable_by_full_id() -> None:
    for model_id in ("claude-opus-4-8", "claude-sonnet-4-6"):
        spec = spec_for(model_id)
        assert spec.real_model_id == model_id
        assert spec.context_window > 0
        assert spec.is_priced


def test_5_generation_rows_carry_window_output_and_prices() -> None:
    for model_id in ("claude-opus-5", "claude-sonnet-5"):
        spec = spec_for(model_id)
        assert spec.context_window == 1_000_000
        assert spec.max_output_tokens == 128_000
        assert spec.is_reasoning is True
        assert spec.supports_vision is True
        assert spec.is_priced
        # Anthropic's published tiering: read ≈ 0.1x input, write ≈ 1.25x.
        base = _price(spec, "input_price_per_mtok")
        assert _price(spec, "cache_read_price_per_mtok") == pytest.approx(base * 0.1)
        assert _price(spec, "cache_write_price_per_mtok") == pytest.approx(base * 1.25)


def test_resolve_alias_passes_through_unknown_value() -> None:
    assert resolve_alias("claude-opus-4-8") == "claude-opus-4-8"
    assert resolve_alias("stub-model") == "stub-model"


# ---------------------------------------------------------------------------
# Pricing math
# ---------------------------------------------------------------------------


def test_price_one_mtok_input_equals_input_price() -> None:
    spec = spec_for("claude-opus-4-8")
    cost = price("claude-opus-4-8", Usage(uncached=1_000_000))
    assert cost == pytest.approx(_price(spec, "input_price_per_mtok"))


def test_price_one_mtok_output_equals_output_price() -> None:
    spec = spec_for("claude-opus-4-8")
    cost = price("claude-opus-4-8", Usage(output=1_000_000))
    assert cost == pytest.approx(_price(spec, "output_price_per_mtok"))


def test_price_mixed_usage_sums_each_component() -> None:
    spec = spec_for("claude-opus-4-8")
    expected = 0.5 * _price(spec, "input_price_per_mtok") + 0.2 * _price(
        spec, "output_price_per_mtok"
    )
    assert price("claude-opus-4-8", Usage(uncached=500_000, output=200_000)) == pytest.approx(
        expected
    )


def test_price_cache_read_and_write_priced_distinctly() -> None:
    spec = spec_for("claude-opus-4-8")
    read_cost = price("claude-opus-4-8", Usage(cache_read=1_000_000))
    write_cost = price("claude-opus-4-8", Usage(cache_write=1_000_000))
    uncached_cost = price("claude-opus-4-8", Usage(uncached=1_000_000))
    assert read_cost == pytest.approx(_price(spec, "cache_read_price_per_mtok"))
    assert write_cost == pytest.approx(_price(spec, "cache_write_price_per_mtok"))
    assert read_cost < uncached_cost < write_cost


def test_price_ignores_reasoning_tokens() -> None:
    """Reasoning tokens are already inside ``output``; adding them would
    double-charge."""
    base = price("claude-opus-4-8", Usage(output=1_000))
    assert price("claude-opus-4-8", Usage(output=1_000, reasoning=1_000)) == base


def test_price_resolves_alias() -> None:
    usage = Usage(uncached=1_000_000, output=1_000_000)
    assert price("opus", usage) == price(ALIASES["opus"], usage)


def test_price_empty_usage_is_zero() -> None:
    assert price("claude-opus-4-8", Usage()) == 0.0


def test_spec_for_unknown_model_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        spec_for("totally-unknown-model")


# ---------------------------------------------------------------------------
# Unknown / unpriced models: warn, never silently charge $0
# ---------------------------------------------------------------------------


def _price_warnings(caplog: pytest.LogCaptureFixture, model: str, usage: Usage) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="noeta.models"):
        price(model, usage)
        price(model, usage)
    return [r.getMessage() for r in caplog.records if model in r.getMessage()]


def test_price_uncatalogued_model_warns_once_and_charges_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    usage = Usage(uncached=1_000_000, output=1_000_000)
    lines = _price_warnings(caplog, "totally-unknown-model", usage)
    assert price("totally-unknown-model", usage) == 0.0
    assert len(lines) == 1
    assert "not in the catalog" in lines[0]


def test_price_catalogued_but_unpriced_row_warns_once_and_charges_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = spec_for("gpt-5.5-2026-04-24")
    assert spec.is_priced is False
    assert spec.input_price_per_mtok is None
    usage = Usage(uncached=1_000_000, output=1_000_000)
    lines = _price_warnings(caplog, "gpt-5.5-2026-04-24", usage)
    assert price("gpt-5.5-2026-04-24", usage) == 0.0
    assert len(lines) == 1
    assert "without published prices" in lines[0]


def test_priced_rows_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    usage = Usage(uncached=1_000_000, output=1_000_000)
    assert _price_warnings(caplog, "claude-opus-5", usage) == []


def test_every_catalogued_row_is_either_priced_or_wholly_unpriced() -> None:
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


def test_models_module_import_diet() -> None:
    """The catalog depends on nothing but the neutral value types."""
    src = inspect.getsource(models)
    for line in src.splitlines():
        line = line.strip()
        if line.startswith(("from noeta", "import noeta")):
            assert line == "from noeta.types import Usage", line


# ---------------------------------------------------------------------------
# model_limits — the data compaction derives from
# ---------------------------------------------------------------------------


def test_model_limits_catalogued_row() -> None:
    assert model_limits("claude-haiku-4-5") == (200_000, 64_000)


def test_model_limits_resolves_alias() -> None:
    spec = spec_for(ALIASES["opus"])
    assert model_limits("opus") == (spec.context_window, spec.max_output_tokens)


def test_model_limits_unknown_model_is_conservative_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="noeta.models"):
        first = model_limits("no-such-model")
        second = model_limits("no-such-model")
    assert first == second == (128_000, 16_384)
    assert (UNKNOWN_MODEL_CONTEXT_WINDOW, UNKNOWN_MODEL_MAX_OUTPUT_TOKENS) == first
    lines = [r.getMessage() for r in caplog.records if "no-such-model" in r.getMessage()]
    assert len(lines) == 1


def test_model_limits_registered_row_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    with caplog.at_level(logging.WARNING, logger="noeta.models"):
        assert model_limits(GATEWAY_ID) == (1_000_000, 128_000)
    assert not caplog.records


# ---------------------------------------------------------------------------
# Operator extensions
# ---------------------------------------------------------------------------


def test_registered_row_answers_spec_lookups() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    assert find_spec(GATEWAY_ID) is not None
    assert spec_for(GATEWAY_ID).context_window == 1_000_000
    assert spec_for("claude-opus-5").real_model_id == "claude-opus-5"
    assert find_spec("no-such-model") is None


def test_family_explicit_field_beats_prefix_inference() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    assert model_family(GATEWAY_ID) == "anthropic"


def test_extension_without_family_stays_unknown() -> None:
    register_models({GATEWAY_ID: _gateway_spec(family=None)})
    assert model_family(GATEWAY_ID) is None


def test_uncatalogued_model_has_no_family() -> None:
    assert model_family("claude-not-catalogued") is None


def test_unpriced_extension_row_warns_once_and_charges_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    usage = Usage(uncached=1_000_000)
    with caplog.at_level(logging.WARNING, logger="noeta.models"):
        assert price(GATEWAY_ID, usage) == 0.0
        assert price(GATEWAY_ID, usage) == 0.0
    unpriced = [r for r in caplog.records if "without published prices" in r.getMessage()]
    assert len(unpriced) == 1


def test_priced_extension_row_charges_real_money() -> None:
    register_models(
        {
            GATEWAY_ID: _gateway_spec(
                input_price_per_mtok=1.00,
                output_price_per_mtok=2.00,
                cache_read_price_per_mtok=0.10,
                cache_write_price_per_mtok=1.25,
            )
        }
    )
    usage = Usage(uncached=1_000_000, output=1_000_000)
    assert price(GATEWAY_ID, usage) == pytest.approx(3.00)


def test_extension_alias_resolves_to_registered_row() -> None:
    register_models({GATEWAY_ID: _gateway_spec()}, aliases={"relay": GATEWAY_ID})
    assert resolve_alias("relay") == GATEWAY_ID
    assert spec_for("relay").real_model_id == GATEWAY_ID


def test_alias_collision_and_unknown_target_refuse() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    with pytest.raises(ValueError, match="opus"):
        register_models({}, aliases={"opus": GATEWAY_ID})
    with pytest.raises(ValueError, match="unknown model"):
        register_models({}, aliases={"relay": "never-registered"})


def test_collision_with_shipped_row_or_alias_refuses() -> None:
    with pytest.raises(ValueError, match="claude-opus-5"):
        register_models({"claude-opus-5": _gateway_spec(real_model_id="claude-opus-5")})
    with pytest.raises(ValueError, match="opus"):
        register_models({"opus": _gateway_spec(real_model_id="opus")})


def test_identical_reregistration_is_a_noop() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    register_models({GATEWAY_ID: _gateway_spec()})
    assert find_spec(GATEWAY_ID) is not None


def test_changed_reregistration_refuses() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    with pytest.raises(ValueError, match="different spec"):
        register_models({GATEWAY_ID: _gateway_spec(context_window=64_000)})


def test_non_modelspec_value_refuses() -> None:
    with pytest.raises(ValueError, match="expected a ModelSpec"):
        register_models({GATEWAY_ID: {"context_window": 5}})  # type: ignore[dict-item]


def test_model_name_cannot_shadow_extension_alias() -> None:
    register_models({GATEWAY_ID: _gateway_spec()}, aliases={"big": GATEWAY_ID})
    with pytest.raises(ValueError, match="big"):
        register_models({"big": _gateway_spec(real_model_id="big")})


def test_refused_call_leaves_overlay_untouched() -> None:
    with pytest.raises(ValueError, match="opus"):
        register_models(
            {
                "gateway/ok": _gateway_spec(real_model_id="gateway/ok"),
                "opus": _gateway_spec(real_model_id="opus"),
            }
        )
    assert find_spec("gateway/ok") is None


def test_alias_retarget_refuses() -> None:
    register_models(
        {
            GATEWAY_ID: _gateway_spec(),
            "gateway/other": _gateway_spec(real_model_id="gateway/other"),
        },
        aliases={"big": GATEWAY_ID},
    )
    with pytest.raises(ValueError, match="already registered"):
        register_models({}, aliases={"big": "gateway/other"})


def test_alias_may_target_shipped_shorthand() -> None:
    register_models({}, aliases={"default-large": "opus"})
    assert resolve_alias("default-large") == resolve_alias("opus")
    assert spec_for("default-large") is spec_for("opus")


def test_alias_across_calls_targets_earlier_row() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    register_models({}, aliases={"relay": GATEWAY_ID})
    assert spec_for("relay").real_model_id == GATEWAY_ID


def test_unknown_family_refuses() -> None:
    with pytest.raises(ValueError, match="unknown family"):
        register_models({GATEWAY_ID: _gateway_spec(family="antropic")})


def test_empty_model_id_refuses() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        register_models({"": _gateway_spec(real_model_id="x")})


def test_catalog_models_lists_the_merged_view() -> None:
    shipped = set(catalog_models())
    register_models({GATEWAY_ID: _gateway_spec()})
    merged = catalog_models()
    assert set(merged) == shipped | {GATEWAY_ID}
    merged.pop("claude-opus-5")
    assert find_spec("claude-opus-5") is not None
