"""Shared declaration helper for the built-in subpackages (package-private).

Every built-in directory declares its manifest with :func:`c` — one
contribution per line, extra keywords packed into ``params``. Pure data
construction; nothing here (or in any built-in declaration) imports a runtime
implementation.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import ManifestContribution


def c(surface: str, name: str, ref: str | None = None, **params: object) -> ManifestContribution:
    """One contribution line — packs extra keywords into ``params``."""
    return ManifestContribution(surface=surface, name=name, ref=ref, params=params)
