"""Every built-in entry point loads, and keeps loading the same object once
the module behind it has been imported."""

from __future__ import annotations

import importlib

import pytest

from noeta.core import registry


@pytest.mark.parametrize("group", [registry.PLUGINS, registry.PROVIDERS, registry.EXECENVS,
                                   registry.STORES])
def test_every_entry_point_loads_a_callable(group: str) -> None:
    assert registry.available(group)
    for name in registry.available(group):
        assert callable(registry.load(group, name)), (group, name)


def test_a_provider_twice() -> None:
    first = registry.provider("anthropic", api_key="k")
    for family in ("anthropic", "openai_compat", "openai_responses"):
        importlib.import_module(f"noeta.backends.providers.{family}")
    assert type(registry.provider("anthropic", api_key="k")) is type(first)


def test_unknown_names() -> None:
    with pytest.raises(registry.UnknownName, match="available: anthropic"):
        registry.provider("nope")
    with pytest.raises(ValueError, match="unknown store url"):
        registry.store("mysql://x")
