"""noeta.builtins — noeta's own capabilities, re-expressed as built-in plugins.

The **top-of-stack band** beside :mod:`noeta.presets` (spec D11): **one
directory per built-in**, each a thin manifest declaration (its ``MANIFEST``)
whose contributions carry ``ref`` strings into runtime implementations that
stay untouched in their own import-linter bands. Nothing here imports those
implementations: a manifest is inert data, and the loader
(:mod:`noeta.client.plugin_set`) resolves a ``ref`` only at its execution
boundary (:meth:`~noeta.client.plugin_set.LoadedPlugin.resolve`). So listing a
built-in's contributions — the D5 / acceptance-2 guarantee — runs **zero**
runtime code.

noeta is its own first plugin author: every standard surface (D3) has a
built-in reference declaration here, ridden through the identical loader /
validation / merge path as any external plugin. **Adding a first-party
capability = adding a directory here** (plus a ``SurfaceSpec`` registration
only when a genuinely new surface is needed).

The declarations are deliberately programmatic rather than shipped
``noeta-plugin.toml`` files: a built-in is *inside* the SDK wheel, so there is
no third-party trust boundary to read across and no package-data discovery to
perform — the thin declaration is the manifest.

The plugin loader reaches this package by ``ref`` string / a dynamic import
from :mod:`noeta.client.plugin_set` — there is **no static import edge** from
the loader up into ``noeta.builtins`` (the import-linter band forbids anything
below from importing it). Only consumers import it.
"""

from __future__ import annotations

from noeta.builtins.browser import MANIFEST as _BROWSER
from noeta.builtins.fs import MANIFEST as _FS
from noeta.builtins.governance import MANIFEST as _GOVERNANCE
from noeta.builtins.memory import MANIFEST as _MEMORY
from noeta.builtins.presets import MANIFEST as _PRESETS
from noeta.builtins.providers import MANIFEST as _PROVIDERS
from noeta.builtins.reminders import MANIFEST as _REMINDERS
from noeta.builtins.sandbox import MANIFEST as _SANDBOX
from noeta.builtins.skills import MANIFEST as _SKILLS
from noeta.builtins.web import MANIFEST as _WEB
from noeta.client.options import BUILTIN_ACTIVATIONS
from noeta.client.plugin_manifest import PluginManifest


__all__ = [
    "builtin_manifests",
    "BUILTIN_PLUGIN_NAMES",
    "assert_activation_vocabulary",
]


#: Catalogue order — the loader's source-0 discovery order (stable; merge is
#: ``(plugin, name)``-deterministic downstream, so this order is presentational).
_BUILTINS: tuple[PluginManifest, ...] = (
    _FS,
    _WEB,
    _MEMORY,
    _BROWSER,
    _SKILLS,
    _REMINDERS,
    _GOVERNANCE,
    _PROVIDERS,
    _SANDBOX,
    _PRESETS,
)

#: Every built-in plugin's name, for enable/disable bookkeeping and the
#: activation vocabulary check (no execution to learn them).
BUILTIN_PLUGIN_NAMES: frozenset[str] = frozenset(m.name for m in _BUILTINS)


def assert_activation_vocabulary() -> None:
    """Fail loudly when a built-in exists that no agent could activate.

    ``compile_options`` validates an activation name against
    :data:`~noeta.client.options.BUILTIN_ACTIVATIONS`, a constant that must
    duplicate this catalogue's names because ``noeta.client`` sits *below*
    ``noeta.builtins`` in the import bands and cannot read them. That makes drift
    possible in exactly one direction — adding a built-in directory here without
    adding the name there — and the symptom is baffling: the plugin loads fine,
    lists its contributions fine, and then ``Options(plugins=("newthing",))``
    reports it as an *unknown activation*.

    So the containment is checked here, at the one import where both sides are in
    scope. The reverse direction is not an error: the vocabulary legitimately
    carries capability flags with no catalogue entry (``todo_write``, ``mcp``, …).
    """
    missing = sorted(BUILTIN_PLUGIN_NAMES - BUILTIN_ACTIVATIONS)
    if missing:
        raise RuntimeError(
            f"built-in plugin(s) {missing} are in the catalogue but not in "
            f"noeta.client.options.BUILTIN_ACTIVATIONS, so no agent can "
            f"activate them — add each name to _ACTIVATION_CAPABILITY_FLAG (if "
            f"it flips a Capabilities flag) or _INERT_BUILTIN_ACTIVATIONS"
        )


assert_activation_vocabulary()


def builtin_manifests() -> tuple[PluginManifest, ...]:
    """The built-in plugin catalogue as static manifests — zero execution.

    Called by the loader's source-0 discovery. Returns fresh references to the
    module-level immutable manifests (safe to share; they carry no live code).
    """
    return _BUILTINS
