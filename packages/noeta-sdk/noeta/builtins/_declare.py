"""Shared declaration helper for the built-in subpackages (package-private).

Every built-in directory declares its manifest with :func:`c`, one contribution
per line. Pure data construction: nothing here — or in any built-in
declaration — imports a runtime implementation, which is what lets the
catalogue be listed without executing capability code.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import ManifestContribution


def c(surface: str, name: str, ref: str | None = None, **params: object) -> ManifestContribution:
    """One contribution line for a built-in's ``MANIFEST``."""
    return ManifestContribution(surface=surface, name=name, ref=ref, params=params)
