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


def test_permission_modes_are_in_widening_trust_order() -> None:
    """The ORDER is the contract, not just the membership.

    A host renders these straight into a picker, so sorting them alphabetically
    (which a ``frozenset`` source silently did) put ``bypassPermissions`` first
    — the most permissive mode offered as the leading choice.
    """
    assert permission_modes() == ("default", "acceptEdits", "bypassPermissions")
    # Every advertised mode is actually accepted by Options.
    for mode in permission_modes():
        Options(system_prompt="x", permission_mode=mode)


def test_effort_modes_are_in_increasing_intensity_order() -> None:
    """Same contract: a ramp, not the alphabet.

    Alphabetical yields ``high, low, max, medium, xhigh``, which reads as
    nonsense in the composer dropdown this projection exists to fill.
    """
    assert effort_modes() == ("low", "medium", "high", "xhigh", "max")
    for mode in effort_modes():
        Options(system_prompt="x", effort=mode)


def test_model_capabilities_projects_catalog_vision() -> None:
    from noeta.builtins.providers.impl import catalog

    # A known vision-capable id, an alias, and an uncatalogued stub.
    vision_ids = [k for k, v in catalog.CATALOG.items() if v.supports_vision]
    assert vision_ids, "catalog should advertise at least one vision model"
    sample = [vision_ids[0], "opus", "stub-model"]
    caps = model_capabilities(sample)

    # Every requested selector is present with a boolean vision flag.
    assert set(caps) == set(sample)
    assert all(isinstance(c["supports_vision"], bool) for c in caps.values())
    # Exactly one key, named the way the provider's own vision guard names it.
    # Pinned because the reference docs once promised `vision` plus "…more".
    assert set(caps[vision_ids[0]]) == {"supports_vision"}
    # The known vision id reports True; the uncatalogued stub fails closed.
    assert caps[vision_ids[0]]["supports_vision"] is True
    assert caps["stub-model"]["supports_vision"] is False


def test_model_capabilities_empty_list() -> None:
    assert model_capabilities([]) == {}
