"""What a host may offer its users: selectable modes and per-model vision.

A host drives the engine only through ``noeta.sdk``, so the facts it needs to
build a picker — the legal permission/effort values, whether a model accepts
images — are projected here and re-exported rather than letting the host reach
into an internal. Pure projections: no state, no I/O.
"""

from __future__ import annotations

from typing import Sequence

from noeta.client.options import EFFORT_MODES, PERMISSION_MODES


def permission_modes() -> tuple[str, ...]:
    """The legal :attr:`Options.permission_mode` values, in widening-trust order.

    The order is the contract, not an accident: a host renders these straight
    into a picker, and alphabetical would offer ``bypassPermissions`` first.
    """
    return PERMISSION_MODES


def effort_modes() -> tuple[str, ...]:
    """The legal :attr:`Options.effort` values, in increasing-intensity order.

    Same reason as :func:`permission_modes`: a picker showing
    ``high, low, max, medium, xhigh`` is unreadable.
    """
    return EFFORT_MODES


def model_capabilities(models: Sequence[str]) -> dict[str, dict[str, bool]]:
    """Per-model ``{supports_vision: bool}`` for the image-attach gate.

    Each selector resolves the same way the provider's vision guard resolves it
    (the merged-catalog ``find_spec``), so the gate a host shows matches the
    gate the request hits. An uncatalogued selector reports vision-capable: the
    adapter admits its images and lets the provider be the authority, so the
    gate must not block what the request would accept. Only a catalogued
    ``supports_vision=False`` row refuses.
    """
    import importlib

    catalog = importlib.import_module("noeta.builtins.providers.impl.catalog")

    out: dict[str, dict[str, bool]] = {}
    for model in models:
        spec = catalog.find_spec(model)
        out[model] = {
            "supports_vision": bool(spec is None or spec.supports_vision)
        }
    return out
