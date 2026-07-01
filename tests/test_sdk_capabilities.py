"""noeta.sdk capability projections — composer enums + per-model vision gate.

The app's ``/capabilities`` reads these through ``noeta.sdk`` only (D2).
"""

from __future__ import annotations

from noeta.sdk import (
    Options,
    effort_modes,
    model_capabilities,
    permission_modes,
)


def test_permission_modes_match_options_enum() -> None:
    assert permission_modes() == ("acceptEdits", "bypassPermissions", "default")
    # Every advertised mode is actually accepted by Options.
    for mode in permission_modes():
        Options(system_prompt="x", permission_mode=mode)


def test_effort_modes_match_options_enum() -> None:
    assert effort_modes() == ("high", "low", "max", "medium", "xhigh")
    for mode in effort_modes():
        Options(system_prompt="x", effort=mode)


def test_model_capabilities_projects_catalog_vision() -> None:
    from noeta.providers import catalog

    # A known vision-capable id, an alias, and an uncatalogued stub.
    vision_ids = [k for k, v in catalog.CATALOG.items() if v.supports_vision]
    assert vision_ids, "catalog should advertise at least one vision model"
    sample = [vision_ids[0], "opus", "stub-model"]
    caps = model_capabilities(sample)

    # Every requested selector is present with a boolean vision flag.
    assert set(caps) == set(sample)
    assert all(isinstance(c["supports_vision"], bool) for c in caps.values())
    # The known vision id reports True; the uncatalogued stub fails closed.
    assert caps[vision_ids[0]]["supports_vision"] is True
    assert caps["stub-model"]["supports_vision"] is False


def test_model_capabilities_empty_list() -> None:
    assert model_capabilities([]) == {}
