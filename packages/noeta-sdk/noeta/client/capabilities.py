"""capabilities — the SDK-side projections the app's ``/capabilities`` reads.

The app product drives the engine only through ``noeta.sdk``; the composer's
selectable enums (permission /
effort modes) and the per-model vision gate are runtime facts the SDK already
depends on, so they are projected here and re-exported through ``noeta.sdk``
rather than letting the backend reach for a runtime internal (``noeta.providers``
is forbidden to ``noeta.agent.backend``).

These are small, pure projections — no state, no I/O — kept together so the one
public capabilities surface is legible.
"""

from __future__ import annotations

from typing import Sequence

from noeta.client.options import _EFFORT_MODES, _PERMISSION_MODES


def permission_modes() -> tuple[str, ...]:
    """The legal :attr:`Options.permission_mode` values, sorted (composer enum)."""
    return tuple(sorted(_PERMISSION_MODES))


def effort_modes() -> tuple[str, ...]:
    """The legal :attr:`Options.effort` values, sorted (composer enum)."""
    return tuple(sorted(_EFFORT_MODES))


def model_capabilities(models: Sequence[str]) -> dict[str, dict[str, bool]]:
    """Per-model ``{supports_vision: bool}`` for the image-attach gate.

    Parallel to the plain ``models`` string list. Each selector is resolved the
    SAME way the Responses vision guard does
    (``resolve_alias`` → ``CATALOG.get``): a friendly alias (``opus`` / ``sonnet``
    / ``haiku``) maps to its real id first; an uncatalogued / unknown selector
    (test stubs like ``stub-model``) has no spec and is conservatively reported
    non-vision — fail-closed, never advertise vision we cannot vouch for.
    """
    from noeta.providers import catalog

    out: dict[str, dict[str, bool]] = {}
    for model in models:
        spec = catalog.CATALOG.get(catalog.resolve_alias(model))
        out[model] = {
            "supports_vision": bool(spec is not None and spec.supports_vision)
        }
    return out
