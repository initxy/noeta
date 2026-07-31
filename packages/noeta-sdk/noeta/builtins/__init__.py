"""The catalogue of noeta's own capabilities, each expressed as a built-in plugin.

One directory per built-in, each a manifest declaration whose contributions carry
``ref`` strings the loader (:mod:`noeta.client.plugin_set`) resolves only at its
execution boundary — so listing a built-in's contributions runs **zero** capability
code, and nothing here imports an implementation. noeta is its own first plugin
author: a first-party capability is a directory here, ridden through the identical
loader / validation / merge path as any external plugin. Declarations are
programmatic rather than shipped ``noeta-plugin.toml`` files because a built-in is
*inside* the SDK wheel — no third-party trust boundary, no package-data discovery.
"""

from __future__ import annotations

from noeta.builtins.app import MANIFEST as _APP
from noeta.builtins.ask_user_question import MANIFEST as _ASK_USER_QUESTION
from noeta.builtins.browser import MANIFEST as _BROWSER
from noeta.builtins.delegation import MANIFEST as _DELEGATION
from noeta.builtins.fs import MANIFEST as _FS
from noeta.builtins.governance import MANIFEST as _GOVERNANCE
from noeta.builtins.mcp import MANIFEST as _MCP
from noeta.builtins.memory import MANIFEST as _MEMORY
from noeta.builtins.presets import MANIFEST as _PRESETS
from noeta.builtins.providers import MANIFEST as _PROVIDERS
from noeta.builtins.react import MANIFEST as _REACT
from noeta.builtins.reminders import MANIFEST as _REMINDERS
from noeta.builtins.sandbox import MANIFEST as _SANDBOX
from noeta.builtins.skills import MANIFEST as _SKILLS
from noeta.builtins.storage import MANIFEST as _STORAGE
from noeta.builtins.todo_write import MANIFEST as _TODO_WRITE
from noeta.builtins.web import MANIFEST as _WEB
from noeta.builtins.workspace import MANIFEST as _WORKSPACE
from noeta.client.options import BUILTIN_ACTIVATIONS
from noeta.client.plugin_manifest import PluginManifest


__all__ = [
    "builtin_manifests",
    "BUILTIN_PLUGIN_NAMES",
    "assert_activation_vocabulary",
]


#: Catalogue order — the loader's discovery order. Presentational only: the
#: downstream merge is ``(plugin, name)``-deterministic regardless.
_BUILTINS: tuple[PluginManifest, ...] = (
    _FS,
    _WEB,
    _MEMORY,
    _BROWSER,
    _APP,
    _MCP,
    _SKILLS,
    _REACT,
    _REMINDERS,
    _GOVERNANCE,
    _PROVIDERS,
    _SANDBOX,
    _PRESETS,
    _WORKSPACE,
    # Declaration-only: zero contributions, because storage is not a surface.
    # The directory exists to house the durable backend impls, which are reached
    # via ``noeta.sdk.storage``.
    _STORAGE,
    _TODO_WRITE,
    _ASK_USER_QUESTION,
    _DELEGATION,
)

#: Every built-in plugin's name — learnable without executing anything.
BUILTIN_PLUGIN_NAMES: frozenset[str] = frozenset(m.name for m in _BUILTINS)


def assert_activation_vocabulary() -> None:
    """Fail loudly when a built-in exists that no agent could activate.

    :data:`~noeta.client.options.BUILTIN_ACTIVATIONS` must duplicate this
    catalogue's names because ``noeta.client`` sits *below* ``noeta.builtins``
    and cannot read them. Drift is therefore possible in one direction — a
    directory added here but not named there — and its symptom is baffling: the
    plugin loads and lists contributions fine, then
    ``Options(plugins=("newthing",))`` calls it an *unknown activation*. This
    import is the one place both sides are in scope. The reverse direction is
    legitimate: the vocabulary also carries capability flags with no catalogue
    entry.
    """
    missing = sorted(BUILTIN_PLUGIN_NAMES - BUILTIN_ACTIVATIONS)
    if missing:
        raise RuntimeError(
            f"built-in plugin(s) {missing} are in the catalogue but not in "
            f"noeta.client.options.BUILTIN_ACTIVATIONS, so no agent can "
            f"activate them — add each name to _ACTIVATION_CAPABILITY_FLAG (if "
            f"it maps onto a feature flag) or _INERT_BUILTIN_ACTIVATIONS"
        )


assert_activation_vocabulary()


def builtin_manifests() -> tuple[PluginManifest, ...]:
    """The built-in catalogue as static manifests — safe to share, zero execution."""
    return _BUILTINS
