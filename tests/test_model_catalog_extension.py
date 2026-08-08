"""Operator-extensible model catalog — the overlay, collisions, HostConfig.

The shipped CATALOG is a transcription of public vendor pages; deployments
also run models it cannot know (internal gateway routing names, self-hosted
models). ``register_models`` puts operator rows into a module-level overlay
that every lookup consults — spec resolution, pricing, compaction derivation,
family classification, vision projection — and ``HostConfig.extra_models`` is
the declarative form, applied at Client construction. Collisions with shipped
data refuse loudly (extensions never override shipped rows); identical
re-registration is a no-op so several Clients may share one HostConfig.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from noeta.builtins.providers.impl import catalog as catalog_mod
from noeta.builtins.providers.impl.catalog import (
    ModelSpec,
    catalog_models,
    derive_compaction_config,
    find_spec,
    price,
    provider_family,
    register_models,
    resolve_alias,
    spec_for,
)
from noeta.client import Client, Options
from noeta.client.capabilities import model_capabilities
from noeta.client.host_config import HostConfig
from noeta.protocols.messages import Usage
from noeta.testing.fake_llm import FakeLLMProvider


#: A neutral stand-in for an internal gateway routing name — carries no
#: vendor prefix, so nothing about it is inferable from the id.
GATEWAY_ID = "gateway/relay-large"


@pytest.fixture(autouse=True)
def _clean_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic overlay + warn-once state per test."""
    monkeypatch.setattr(catalog_mod, "_EXTENSIONS", {})
    monkeypatch.setattr(catalog_mod, "_EXTENSION_ALIASES", {})
    monkeypatch.setattr(catalog_mod, "_WARNED", set())


def _gateway_spec(**overrides: object) -> ModelSpec:
    base: dict = dict(
        real_model_id=GATEWAY_ID,
        context_window=1_000_000,
        max_output_tokens=128_000,
        is_reasoning=True,
        supports_vision=True,
        provider_family="anthropic",
    )
    base.update(overrides)
    return ModelSpec(**base)


# ---------------------------------------------------------------------------
# The overlay reaches every consumer
# ---------------------------------------------------------------------------


def test_registered_row_answers_spec_lookups() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    assert find_spec(GATEWAY_ID) is not None
    assert spec_for(GATEWAY_ID).context_window == 1_000_000
    # Shipped rows are untouched and unknowns still miss.
    assert spec_for("claude-opus-5").real_model_id == "claude-opus-5"
    assert find_spec("no-such-model") is None


def test_registered_row_drives_compaction_derivation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real window replaces the conservative unknown-model fallback — the
    exact failure the extension exists to fix."""
    register_models({GATEWAY_ID: _gateway_spec()})
    with caplog.at_level(logging.WARNING):
        config = derive_compaction_config(GATEWAY_ID)
    assert config.context_window == 1_000_000
    assert config.max_output_tokens == 128_000
    assert not caplog.records  # no unknown-model warning


def test_provider_family_explicit_field_beats_prefix_inference() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    assert provider_family(GATEWAY_ID) == "anthropic"


def test_extension_without_family_stays_unfiltered() -> None:
    register_models(
        {GATEWAY_ID: _gateway_spec(provider_family=None)}
    )
    assert provider_family(GATEWAY_ID) is None


def test_unpriced_extension_row_warns_once_and_charges_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    register_models({GATEWAY_ID: _gateway_spec()})  # prices default None
    usage = Usage(uncached=1_000_000, cache_read=0, cache_write=0, output=0)
    with caplog.at_level(logging.WARNING):
        assert price(GATEWAY_ID, usage) == 0.0
        assert price(GATEWAY_ID, usage) == 0.0
    unpriced = [r for r in caplog.records if "without published prices" in r.message]
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
    usage = Usage(
        uncached=1_000_000, cache_read=0, cache_write=0, output=1_000_000
    )
    assert price(GATEWAY_ID, usage) == pytest.approx(3.00)


def test_vision_projection_sees_extension_row() -> None:
    """A registered ``supports_vision=False`` is a decision and refuses; an
    unregistered unknown stays let-through — the shipped asymmetry, extended."""
    register_models(
        {GATEWAY_ID: _gateway_spec(supports_vision=False)}
    )
    caps = model_capabilities([GATEWAY_ID, "totally-unknown"])
    assert caps[GATEWAY_ID]["supports_vision"] is False
    assert caps["totally-unknown"]["supports_vision"] is True


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


def test_extension_alias_resolves_to_registered_row() -> None:
    register_models(
        {GATEWAY_ID: _gateway_spec()}, aliases={"relay": GATEWAY_ID}
    )
    assert resolve_alias("relay") == GATEWAY_ID
    assert spec_for("relay").real_model_id == GATEWAY_ID


def test_alias_collision_and_unknown_target_refuse() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    with pytest.raises(ValueError, match="opus"):
        register_models({}, aliases={"opus": GATEWAY_ID})
    with pytest.raises(ValueError, match="unknown model"):
        register_models({}, aliases={"relay": "never-registered"})


# ---------------------------------------------------------------------------
# Collisions and idempotency
# ---------------------------------------------------------------------------


def test_collision_with_shipped_row_or_alias_refuses() -> None:
    with pytest.raises(ValueError, match="claude-opus-5"):
        register_models(
            {"claude-opus-5": _gateway_spec(real_model_id="claude-opus-5")}
        )
    with pytest.raises(ValueError, match="opus"):
        register_models({"opus": _gateway_spec(real_model_id="opus")})


def test_identical_reregistration_is_a_noop() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    register_models({GATEWAY_ID: _gateway_spec()})  # same spec — silent
    assert find_spec(GATEWAY_ID) is not None


def test_changed_reregistration_refuses() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    with pytest.raises(ValueError, match="different spec"):
        register_models({GATEWAY_ID: _gateway_spec(context_window=64_000)})


def test_non_modelspec_value_refuses() -> None:
    with pytest.raises(ValueError, match="expected a ModelSpec"):
        register_models({GATEWAY_ID: {"context_window": 5}})  # type: ignore[dict-item]


def test_model_name_cannot_shadow_extension_alias() -> None:
    """The collision check is symmetric: a later model registration must not
    silently sit behind an existing extension alias (which would make every
    lookup answer with the alias target's spec, not the new row's)."""
    register_models({GATEWAY_ID: _gateway_spec()}, aliases={"big": GATEWAY_ID})
    with pytest.raises(ValueError, match="big"):
        register_models({"big": _gateway_spec(real_model_id="big")})


def test_refused_call_leaves_overlay_untouched() -> None:
    """Registration is all-or-nothing: the valid rows of a refused call must
    not land, or the overlay ends in a state no single call could produce."""
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
    """A target is resolved through the shipped aliases at registration and
    stored resolved, so ``resolve_alias`` stays single-hop."""
    register_models({}, aliases={"default-large": "opus"})
    assert resolve_alias("default-large") == resolve_alias("opus")
    assert spec_for("default-large") is spec_for("opus")


def test_alias_across_calls_targets_earlier_row() -> None:
    register_models({GATEWAY_ID: _gateway_spec()})
    register_models({}, aliases={"relay": GATEWAY_ID})
    assert spec_for("relay").real_model_id == GATEWAY_ID


def test_unknown_provider_family_refuses() -> None:
    with pytest.raises(ValueError, match="provider_family"):
        register_models({GATEWAY_ID: _gateway_spec(provider_family="antropic")})


def test_empty_model_id_refuses() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        register_models({"": _gateway_spec(real_model_id="x")})


def test_catalog_models_lists_the_merged_view() -> None:
    shipped = set(catalog_models())
    register_models({GATEWAY_ID: _gateway_spec()})
    merged = catalog_models()
    assert set(merged) == shipped | {GATEWAY_ID}
    # A fresh copy — mutating it changes nothing.
    merged.pop("claude-opus-5")
    assert find_spec("claude-opus-5") is not None


# ---------------------------------------------------------------------------
# HostConfig — the declarative form
# ---------------------------------------------------------------------------


def test_host_config_extra_models_registers_at_client_build(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    Client(
        Options(system_prompt="test prompt"),
        provider=FakeLLMProvider(responses=[]),
        workspace_dir=ws,
        model="stub-model",
        host_config=HostConfig(extra_models={GATEWAY_ID: _gateway_spec()}),
    )
    assert find_spec(GATEWAY_ID) is not None
    assert provider_family(GATEWAY_ID) == "anthropic"


def test_two_clients_from_one_host_config_are_idempotent(
    tmp_path: Path,
) -> None:
    """The exact case identical-re-registration exists for."""
    ws = tmp_path / "ws"
    ws.mkdir()
    hc = HostConfig(extra_models={GATEWAY_ID: _gateway_spec()})
    for _ in range(2):
        Client(
            Options(system_prompt="test prompt"),
            provider=FakeLLMProvider(responses=[]),
            workspace_dir=ws,
            model="stub-model",
            host_config=hc,
        )
    assert find_spec(GATEWAY_ID) is not None


def test_host_config_extra_models_collision_fails_the_build(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError, match="claude-opus-5"):
        Client(
            Options(system_prompt="test prompt"),
            provider=FakeLLMProvider(responses=[]),
            workspace_dir=ws,
            model="stub-model",
            host_config=HostConfig(
                extra_models={
                    "claude-opus-5": _gateway_spec(
                        real_model_id="claude-opus-5"
                    )
                }
            ),
        )


def test_sdk_providers_doorway_exports_the_registration_api() -> None:
    from noeta.sdk import providers as sdk_providers

    assert sdk_providers.register_models is catalog_mod.register_models
    assert sdk_providers.find_spec is catalog_mod.find_spec
