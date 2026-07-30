"""Five-source loader, deterministic merge, and ``PluginSet`` (spec D4 / D5).

The loader is **surface-agnostic**: it reads static manifests
(:mod:`noeta.client.plugin_manifest`) from five sources, gates them, dedups
plugin names, and returns a :class:`PluginSet` — the loaded, host-level set that
is **listable and collision-checkable without executing plugin code**. Merge and
collision detection run over the :class:`~noeta.client.surfaces.SurfaceRegistry`,
so discovery order never affects the result (only error attribution).

Five sources (D4), each with its own gate::

    0  built-in plugins        on by default; a host may disable individually
    1  entry points            enabled allow-list, applied BEFORE any import
    2  explicit modules/files  caller-specified = authorized
    3  ~/.noeta/plugins        the user's own machine = trusted
    4  workspace .noeta/plugins trust store (untrusted dir -> warn + skip)

The pipeline for every candidate: read manifest (zero code execution for the
package / ``.toml`` forms) → ``enabled`` gate **before any import** → trust gate
(source 4) → collision check → deterministic merge sorted by ``(plugin,
contribution)``. Collisions — including cross-source duplicate plugin names —
are **errors naming both sides; there is no override**.

This is the M1 mechanism core, built additively next to the still-live 0.4.0
``noeta.client.plugins`` module (whose trust store this module reuses). Wiring
activation into ``compile_options`` and retiring ``Capabilities`` is M2.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence

from noeta.client.plugin_manifest import (
    LITERAL_PARAM,
    MANIFEST_BASENAME,
    ManifestContribution,
    PluginBuilder,
    PluginManifest,
    declared_plugin_name,
    read_distribution_manifest,
    read_manifest_file,
)
from noeta.client.options import PluginActivation
from noeta.client.plugins import (
    DEFAULT_TRUST_STORE,
    PLUGIN_ENTRY_POINT_GROUP,
    PluginError,
    UntrustedPluginDirWarning,
    is_trusted,
)
from noeta.client.surfaces import SurfaceRegistry, standard_registry


__all__ = [
    "LoadedPlugin",
    "PluginSet",
    "MergedEntry",
    "MergedContributions",
    "ResolvedContribution",
    "load_plugins",
]


@dataclass(frozen=True)
class LoadedPlugin:
    """One discovered plugin: its static manifest plus where it came from.

    "Loaded" means the manifest was read and the gates passed — **not** that any
    plugin code ran. ``resolved_objects`` is non-empty only for single-file
    plugins (whose manifest reading already executed the trusted file), letting
    :meth:`resolve` return their values without a second import.
    """

    name: str
    manifest: PluginManifest
    origin: str
    source: str
    resolved_objects: Mapping[tuple[str, str], Any] = field(default_factory=dict)

    def resolve(self, registry: SurfaceRegistry) -> tuple["ResolvedContribution", ...]:
        """Import each contribution's ``ref`` and validate it per its surface.

        This is the one step that **executes plugin code**; the loader never
        calls it, so listing and merge stay execution-free. A resolution or
        validation fault raises :class:`PluginError` naming the plugin.
        """
        out: list[ResolvedContribution] = []
        for c in self.manifest.contributions:
            spec = registry.get(c.surface)
            value = self._resolve_value(c)
            try:
                spec.validator(value)
            except PluginError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise PluginError(
                    f"plugin {self.name!r}: contribution {c.name!r} to surface "
                    f"{c.surface!r} failed validation: {exc}"
                ) from exc
            out.append(
                ResolvedContribution(
                    plugin=self.name, surface=c.surface, name=c.name, value=value
                )
            )
        return tuple(out)

    def _resolve_value(self, c: ManifestContribution) -> Any:
        cached = self.resolved_objects.get((c.surface, c.name))
        if cached is not None:
            return cached
        if c.ref is None:
            if c.path is not None:
                return c.path  # resource-only contribution
            literal = c.params.get(LITERAL_PARAM)
            if isinstance(literal, str):
                return literal  # literal-valued contribution (prompt_fragment)
            raise PluginError(
                f"plugin {self.name!r}: contribution {c.name!r} has neither a "
                f"ref, a path, nor a literal {LITERAL_PARAM!r} to resolve"
            )
        module, attr_parts = self._import_ref(c.ref)
        obj: Any = module
        for part in attr_parts:
            try:
                obj = getattr(obj, part)
            except AttributeError as exc:
                raise PluginError(
                    f"plugin {self.name!r}: ref {c.ref!r} — "
                    f"{'.'.join(attr_parts)!r} not found on module "
                    f"{module.__name__!r}"
                ) from exc
        return obj

    def _import_ref(self, ref: str) -> tuple[ModuleType, tuple[str, ...]]:
        """Import ``ref``'s module half and return it with the attribute path.

        Both manifest ref spellings resolve here, matching what
        ``plugin_manifest._derive_name`` and ``plugin_check`` already accept:

        * ``pkg.mod:attr.sub`` — the explicit form; everything left of ``:`` is
          the module.
        * ``pkg.mod.attr`` — the dotted form; the longest importable prefix is
          the module and the rest is the attribute path.

        The dotted form backs off one component at a time, but only for a
        ``ModuleNotFoundError`` naming the prefix it just tried. An import fault
        *inside* a module that does exist (a missing third-party dependency, a
        raising module body) propagates as itself — never silently reinterpreted
        as "that was an attribute, not a module".
        """
        module_name, sep, attr = ref.partition(":")
        if sep:
            return self._import_module(module_name, ref), tuple(
                p for p in attr.split(".") if p
            )
        parts = ref.split(".")
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i])
            try:
                return importlib.import_module(candidate), tuple(parts[i:])
            except ModuleNotFoundError as exc:
                missing = exc.name
                if i > 1 and missing is not None and (
                    candidate == missing or candidate.startswith(missing + ".")
                ):
                    continue  # not a module — try treating it as an attribute
                raise PluginError(
                    f"plugin {self.name!r}: ref {ref!r} — module "
                    f"{candidate!r} failed to import: {exc}"
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise PluginError(
                    f"plugin {self.name!r}: ref {ref!r} — module {candidate!r} "
                    f"failed to import: {exc}"
                ) from exc
        raise PluginError(f"plugin {self.name!r}: ref {ref!r} is not importable")

    def _import_module(self, module_name: str, ref: str) -> ModuleType:
        try:
            return importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            raise PluginError(
                f"plugin {self.name!r}: ref {ref!r} — module {module_name!r} "
                f"failed to import: {exc}"
            ) from exc


@dataclass(frozen=True)
class ResolvedContribution:
    """A contribution after its ref was imported and validated."""

    plugin: str
    surface: str
    name: str
    value: Any


@dataclass(frozen=True)
class MergedEntry:
    """One contribution's place in a surface's merged, ordered list."""

    plugin: str
    contribution: ManifestContribution


@dataclass(frozen=True)
class MergedContributions:
    """The deterministic merge: ``surface name -> ordered contributions``.

    Built without executing plugin code (collision keys and ordering come from
    the static manifests). Equal under any discovery order, so two hosts with
    the same loaded set produce the same plan.
    """

    by_surface: Mapping[str, tuple[MergedEntry, ...]]

    def surfaces(self) -> tuple[str, ...]:
        return tuple(sorted(self.by_surface))

    def for_surface(self, name: str) -> tuple[MergedEntry, ...]:
        return tuple(self.by_surface.get(name, ()))


@dataclass(frozen=True)
class PluginSet:
    """The loaded, host-level set — listable / auditable without executing code (D5).

    Holds the discovered plugins and the surface registry they were loaded
    against. :meth:`contributions` and :meth:`merged` read only static
    manifests; :meth:`resolve` is the boundary that imports plugin code (used at
    compile time in M2).
    """

    plugins: tuple[LoadedPlugin, ...]
    registry: SurfaceRegistry
    #: The built-in names the caller explicitly turned off (``load_plugins``'
    #: ``disabled_builtins``). Recorded rather than inferred from membership:
    #: ``builtins=False`` means "no built-in plugin *catalogue*" (the usual way
    #: to load a single test plugin), NOT "no built-in capabilities", so an
    #: absent name is not a disable. A host reads this to honour a disable that
    #: no contribution can express — see ``SdkHost(skills_enabled=...)``.
    disabled_builtins: frozenset[str] = frozenset()
    #: Memo of ``plugin name -> resolved contributions``. Resolution imports and
    #: validates a plugin's refs, and ``Client`` asks for several different
    #: projections of the same set during one build; without the memo each
    #: projection re-imports and re-validates every ref. Excluded from equality /
    #: repr — it is a cache, not part of the set's identity.
    _resolved: dict[str, tuple["ResolvedContribution", ...]] = field(
        default_factory=dict, compare=False, repr=False
    )

    def names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.plugins)

    def __iter__(self) -> Iterator[LoadedPlugin]:
        return iter(self.plugins)

    def __len__(self) -> int:
        return len(self.plugins)

    def __contains__(self, name: object) -> bool:
        return any(p.name == name for p in self.plugins)

    def get(self, name: str) -> LoadedPlugin:
        for p in self.plugins:
            if p.name == name:
                return p
        raise PluginError(f"no loaded plugin named {name!r}")

    def contributions(
        self, surface: Optional[str] = None
    ) -> tuple[tuple[str, ManifestContribution], ...]:
        """Every contribution as ``(plugin name, contribution)`` — **zero execution**.

        Optionally filtered to one ``surface``. This is the D5 / acceptance-2
        listing: a caller sees what an installed plugin contributes without any
        of its code running.
        """
        out: list[tuple[str, ManifestContribution]] = []
        for p in self.plugins:
            for c in p.manifest.contributions:
                if surface is None or c.surface == surface:
                    out.append((p.name, c))
        return tuple(out)

    def merged(self) -> MergedContributions:
        """Collision-check and deterministically order all contributions (zero execution).

        Contributions are grouped by surface, checked for collision per the
        surface's ``collision_key``, and ordered per its ``ordering`` — the whole
        computation over static manifests, so it is invariant under the order in
        which plugins were discovered. Any collision raises :class:`PluginError`
        naming **both** plugins; there is no override.
        """
        entries: dict[str, list[MergedEntry]] = {}
        owner: dict[tuple[str, str], str] = {}

        for plugin in sorted(self.plugins, key=lambda p: p.name):
            for c in plugin.manifest.contributions:
                spec = self.registry.get(c.surface)
                scope = _collision_scope(spec.collision_key, spec.name, c.name)
                if scope is not None:
                    prior = owner.get((c.surface, scope))
                    if prior is not None:
                        raise _collision_error(spec, c, prior, plugin.name)
                    owner[(c.surface, scope)] = plugin.name
                entries.setdefault(c.surface, []).append(MergedEntry(plugin.name, c))

        by_surface: dict[str, tuple[MergedEntry, ...]] = {}
        for surface, items in entries.items():
            spec = self.registry.get(surface)
            if spec.ordering == "priority":
                items.sort(
                    key=lambda e: (_priority(e.contribution), e.plugin, e.contribution.name)
                )
            else:
                items.sort(key=lambda e: (e.plugin, e.contribution.name))
            by_surface[surface] = tuple(items)
        return MergedContributions(by_surface=by_surface)

    # -- the execution boundary -------------------------------------------

    def _resolve_plugin(self, plugin: LoadedPlugin) -> tuple[ResolvedContribution, ...]:
        """``plugin.resolve`` behind the per-set memo — imports each ref at most once."""
        cached = self._resolved.get(plugin.name)
        if cached is None:
            cached = plugin.resolve(self.registry)
            self._resolved[plugin.name] = cached
        return cached

    def _external(self, only: Optional[Iterable[str]]) -> tuple[LoadedPlugin, ...]:
        """The external plugins whose code an *activation-scoped* projection may run.

        Built-ins are always excluded (their effect is the capability-flag
        vocabulary ``compile_options`` handles by name). ``only`` — the union of
        every agent's activation list — further restricts resolution to plugins
        some agent actually activates, so a loaded-but-unactivated plugin's module
        body never runs during a ``Client`` build. ``None`` means "no activation
        filter" (every external plugin), the shape a caller inspecting the whole
        set wants.
        """
        wanted = None if only is None else set(only)
        return tuple(
            p
            for p in self.plugins
            if p.source != "builtin" and (wanted is None or p.name in wanted)
        )

    def _declares(self, plugin: LoadedPlugin, surfaces: "frozenset[str]") -> bool:
        """Whether ``plugin``'s **static manifest** names any of ``surfaces``.

        The second half of the "loading is not running" rule. Activation scoping
        keeps a projection away from plugins nobody opted into; this keeps it away
        from plugins that have nothing to give it — a plugin contributing only a
        ``guard`` is never imported to look for reminders, and a plugin
        contributing only a ``tool`` is never imported by the governance pass.
        Decided from the manifest, so the check itself executes nothing.
        """
        return any(c.surface in surfaces for c in plugin.manifest.contributions)

    def _identity_surfaces(self) -> "frozenset[str]":
        return frozenset(
            name
            for name in self.registry.names()
            if self.registry.get(name).plane == "identity"
        )

    def identity_activations(
        self, only: Optional[Iterable[str]] = None
    ) -> dict[str, "PluginActivation"]:
        """Resolve each **external** plugin's identity-plane contributions (D5).

        Keyed by plugin name, the map ``Client`` passes to
        :func:`~noeta.client.options.compile_options`: an agent that names one of
        these in its ``plugins`` list pulls in that plugin's tools / child agents
        / content kinds / prompt fragments (D6 — feature surfaces follow
        activation). Built-in plugins are excluded (their feature effect is the
        capability-flag vocabulary compile handles by name); ``only`` restricts
        resolution to the activated names (see :meth:`_external`).

        This executes plugin code (resolve imports the refs) — the Client build
        boundary, never a mid-session turn.
        """
        out: dict[str, PluginActivation] = {}
        identity = self._identity_surfaces()
        for p in self._external(only):
            tools: list[Any] = []
            agents: list[tuple[str, Any]] = []
            kinds: list[tuple[str, Any]] = []
            frags: list[tuple[str, str]] = []
            policy: Any = None
            # Every activated external plugin gets an entry — ``compile_options``
            # reads this map as the "is that a known activation name?" vocabulary,
            # so a plugin with only wiring contributions must still appear. Only
            # the resolution is skipped.
            resolved = self._resolve_plugin(p) if self._declares(p, identity) else ()
            for rc in resolved:
                spec = self.registry.get(rc.surface)
                if spec.plane != "identity":
                    continue
                if rc.surface == "tool":
                    tools.append(rc.value)
                elif rc.surface == "agent":
                    agents.append((rc.name, rc.value))
                elif rc.surface == "content_kind":
                    kinds.append((rc.name, rc.value))
                elif rc.surface == "prompt_fragment":
                    frags.append((rc.name, rc.value))
                elif rc.surface == "policy":
                    # Single-valued surface: at most one per plugin (the merge
                    # would already reject two). Cross-plugin collisions with the
                    # base ``Options.policy`` / another active plugin are caught at
                    # compile (D10).
                    policy = rc.value
                else:
                    # A host-registered identity surface with no branch here would
                    # otherwise be dropped between resolve and compile, exactly
                    # how ``content_kind`` went missing. Fail instead.
                    raise PluginError(
                        f"plugin {p.name!r}: identity-plane surface "
                        f"{rc.surface!r} has no activation binding — "
                        f"identity_activations() must learn to carry it"
                    )
            out[p.name] = PluginActivation(
                tools=tuple(tools),
                agents=tuple(agents),
                content_kinds=tuple(v for _n, v in sorted(kinds, key=lambda e: e[0])),
                prompt_fragments=tuple(frags),
                policy=policy,
            )
        return out

    def activation_transforms(
        self, only: Optional[Iterable[str]] = None
    ) -> dict[str, tuple[tuple[int, str, Any], ...]]:
        """Resolve each **external** plugin's ``tool_result_transform`` stages (D9).

        Returns ``plugin name -> ((priority, contribution name, fn), …)`` for the
        wiring-plane, per-agent ``tool_result_transform`` surface. ``Client`` folds
        these into a per-agent stage list (an agent that activates the plugin gets
        its transforms), ordered by ``(priority, plugin, name)`` — the transformed
        ``ToolResult`` is what the ToolRuntime records, so a redaction stage keeps
        the secret out of the ledger and ContentStore. Built-ins are excluded (no
        first-party transform ships). Executes plugin code (resolve) at the Client
        build boundary, never a mid-session turn.
        """
        return self._activation_params("tool_result_transform", _priority, only)

    def activation_reminders(
        self, only: Optional[Iterable[str]] = None
    ) -> dict[str, tuple[tuple[int, str, Any], ...]]:
        """Resolve each **external** plugin's compose-time ``reminder`` renders (track B, D8).

        Returns ``plugin name -> ((priority, contribution name, render), …)``.
        ``Client`` turns these into
        :class:`~noeta.context.reminders.ReminderSpec` s for the agents that
        activate the plugin; the composer runs them at the tail of the dynamic
        suffix in ``(priority, name)`` order. Same plane / scoping / execution
        boundary as :meth:`activation_transforms`.
        """
        return self._activation_params("reminder", _priority, only)

    def activation_reminder_providers(
        self, only: Optional[Iterable[str]] = None
    ) -> dict[str, tuple[tuple[tuple[str, ...], str, Any], ...]]:
        """Resolve each **external** plugin's recorded ``reminder_provider`` s (track A, D7).

        Returns ``plugin name -> ((seams, contribution name, provider), …)``,
        where ``seams`` is the manifest's declared seam list (defaulting to
        :data:`~noeta.execution.reminders.TURN_INTAKE`, the only seam a goal
        append fires). ``Client`` folds these into a per-agent, per-seam table the
        host hands the recording path, which runs them in ``(plugin, name)``
        order. Built-ins are excluded: the built-in ``memory`` declaration is a
        *factory* bound to a live store by the host, not a provider.
        """
        return self._activation_params("reminder_provider", _seams, only)

    def _activation_params(
        self,
        surface: str,
        param: Callable[[ManifestContribution], Any],
        only: Optional[Iterable[str]],
    ) -> dict[str, tuple[tuple[Any, str, Any], ...]]:
        """Shared body of the per-agent wiring projections.

        Each returns ``plugin -> ((<ordering param>, contribution name, value), …)``
        for one surface; ``param`` reads the surface's ordering / binding param off
        the static manifest entry.
        """
        out: dict[str, tuple[tuple[Any, str, Any], ...]] = {}
        wanted = frozenset({surface})
        for p in self._external(only):
            if not self._declares(p, wanted):
                continue
            by_key = {
                (c.surface, c.name): param(c) for c in p.manifest.contributions
            }
            entries = [
                (by_key.get((rc.surface, rc.name)), rc.name, rc.value)
                for rc in self._resolve_plugin(p)
                if rc.surface == surface
            ]
            if entries:
                out[p.name] = tuple(entries)
        return out

    def process_hooks(self) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        """Resolve every loaded **external** plugin's guard + observer values (D6).

        Governance surfaces do **not** follow per-agent activation: a loaded
        guard / observer is in force for every agent in the process. This returns
        ``(guards, observers)`` in deterministic ``(plugin, name)`` order, which
        ``Client`` folds into its process-wide guard stack + observer
        subscriptions regardless of which plugins any agent activates. Built-in
        governance guards are the engine's own default stack (wired elsewhere),
        so they are excluded here.

        Unlike the activation-scoped projections above this deliberately ignores
        activation: a guard that only applied once some agent opted in would not
        be governance authority. It is still limited to the plugins whose static
        manifest *declares* a guard or observer, so a loaded plugin that governs
        nothing is not imported to discover that.
        """
        guards: list[Any] = []
        observers: list[Any] = []
        governance = frozenset({"guard", "observer"})
        for p in sorted(self._external(None), key=lambda x: x.name):
            if not self._declares(p, governance):
                continue
            resolved = sorted(self._resolve_plugin(p), key=lambda r: r.name)
            for rc in resolved:
                if rc.surface == "guard":
                    guards.append(rc.value)
                elif rc.surface == "observer":
                    observers.append(rc.value)
        return tuple(guards), tuple(observers)

    def resolve(self) -> tuple[ResolvedContribution, ...]:
        """Resolve + validate every contribution (imports plugin code).

        The order matches :meth:`merged`, so a caller folding contributions into
        ``Options`` gets the deterministic order.
        """
        by_key: dict[tuple[str, str, str], ResolvedContribution] = {}
        for plugin in self.plugins:
            for rc in self._resolve_plugin(plugin):
                by_key[(plugin.name, rc.surface, rc.name)] = rc
        out: list[ResolvedContribution] = []
        for surface_entries in self.merged().by_surface.values():
            for entry in surface_entries:
                rc = by_key.get(
                    (entry.plugin, entry.contribution.surface, entry.contribution.name)
                )
                if rc is not None:
                    out.append(rc)
        return tuple(out)


def _collision_scope(
    collision_key: str, surface_name: str, contribution_name: str
) -> Optional[str]:
    if collision_key == "none":
        return None
    if collision_key == "single-valued":
        return f"<surface {surface_name}>"
    return contribution_name


def _collision_error(spec: Any, c: ManifestContribution, a: str, b: str) -> PluginError:
    label = {
        "kind": "content kind",
        "alias": "mcp alias",
        "single-valued": "surface",
    }.get(spec.collision_key, spec.name)
    if spec.collision_key == "single-valued":
        return PluginError(
            f"surface {spec.name!r} is single-valued but contributed by both "
            f"plugin {a!r} and plugin {b!r} — no override"
        )
    return PluginError(
        f"{label} {c.name!r} on surface {spec.name!r} is contributed by both "
        f"plugin {a!r} and plugin {b!r} — no override"
    )


def _priority(c: ManifestContribution) -> int:
    value = c.params.get("priority", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


#: The recording seam a ``reminder_provider`` binds to when its manifest names none.
DEFAULT_REMINDER_SEAM = "turn_intake"


def _seams(c: ManifestContribution) -> tuple[str, ...]:
    """A ``reminder_provider``'s declared recording seams (D7).

    ``seams`` may arrive as a TOML array or a single string; an entry that
    declares none binds to :data:`DEFAULT_REMINDER_SEAM`, the seam every goal /
    follow-up append fires.
    """
    value = c.params.get("seams")
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        named = tuple(str(s) for s in value if str(s).strip())
        if named:
            return named
    return (DEFAULT_REMINDER_SEAM,)


# ---------------------------------------------------------------------------
# The five-source loader
# ---------------------------------------------------------------------------


#: Built-ins whose capability the compiled agent depends on unconditionally, so
#: ``disabled_builtins`` cannot express a removal. Refusing loudly is the honest
#: answer: before this the name was dropped from the catalogue while the
#: capability stayed wired, and the disable read as effective.
_NON_REMOVABLE_BUILTINS: dict[str, str] = {
    "react": (
        "built-in 'react' cannot be disabled: it supplies the DEFAULT decision "
        "policy, and every compiled AgentSpec pins that identity as "
        "POLICY_REF ('react', '1') — an agent with no policy has no defined "
        "identity or parity. The default brain is REPLACEABLE, not removable: "
        "activate a plugin contributing the 'policy' surface (D10) and its ref "
        "takes over both the identity and the wired factory."
    ),
}


@dataclass(frozen=True)
class _Candidate:
    name: str
    manifest: PluginManifest
    origin: str
    source: str
    resolved_objects: Mapping[tuple[str, str], Any] = field(default_factory=dict)


def load_plugins(
    *,
    builtins: "bool | Iterable[PluginManifest]" = True,
    disabled_builtins: Iterable[str] = (),
    entry_points: "bool | Iterable[Any]" = False,
    modules: Sequence[str] = (),
    user_dirs: Sequence[Any] = (),
    workspace_dirs: Sequence[Any] = (),
    enabled: Optional[Iterable[str]] = None,
    trust_store: Optional[Path] = None,
    registry: Optional[SurfaceRegistry] = None,
    entry_point_group: str = PLUGIN_ENTRY_POINT_GROUP,
) -> PluginSet:
    """Discover plugins from the five sources and return a :class:`PluginSet`.

    ``builtins`` (source 0) is on by default and disabled per-name via
    ``disabled_builtins``; ``True`` discovers the built-in catalogue
    (``noeta.builtins``), and an iterable of manifests may be injected instead
    as the testing seam. A disable is recorded on the returned set
    (:attr:`PluginSet.disabled_builtins`) so a host can honour the ones no
    contribution expresses — and a name in ``_NON_REMOVABLE_BUILTINS``
    (``react``) raises rather than pretending to take effect.
    ``entry_points`` (source 1) is off unless ``True`` (real ``noeta.plugins``
    discovery) or an iterable of entry-point-like objects (each exposing ``.name``
    + ``.dist``) is passed. ``modules`` (source 2) are explicit dotted modules or
    file paths. ``user_dirs`` (source 3) load unconditionally; ``workspace_dirs``
    (source 4) load only when trusted, else are skipped with an
    :class:`UntrustedPluginDirWarning`.

    ``enabled`` is an allow-list of plugin *names*. For the package / ``.toml``
    forms the name is known from the static manifest, so the gate is applied
    before anything imports; for a single-file plugin a statically declared name
    (``noeta_plugin_name`` / ``PluginBuilder("...")``) is honoured before the
    file is executed. A candidate whose declared name is not on the list is
    skipped **without importing**.

    A load fault (bad manifest, missing manifest, a broken file) raises
    :class:`PluginError` naming the plugin — never a silent skip, except the
    untrusted-workspace warning above.
    """
    reg = registry if registry is not None else standard_registry()
    enabled_set = set(enabled) if enabled is not None else None
    disabled = set(disabled_builtins)
    for name in sorted(disabled & _NON_REMOVABLE_BUILTINS.keys()):
        raise PluginError(_NON_REMOVABLE_BUILTINS[name])
    store = Path(trust_store) if trust_store is not None else DEFAULT_TRUST_STORE

    seen: dict[str, str] = {}
    loaded: list[LoadedPlugin] = []

    def accept(cand: Optional[_Candidate]) -> None:
        if cand is None:
            return
        if cand.name in seen:
            raise PluginError(
                f"duplicate plugin name {cand.name!r}: found in both "
                f"{seen[cand.name]} and {cand.origin}"
            )
        seen[cand.name] = cand.origin
        loaded.append(
            LoadedPlugin(
                name=cand.name,
                manifest=cand.manifest,
                origin=cand.origin,
                source=cand.source,
                resolved_objects=cand.resolved_objects,
            )
        )

    # 0. built-ins
    for cand in _read_builtins(builtins, disabled, enabled_set):
        accept(cand)

    # 1. entry points
    for cand in _read_entry_points(entry_points, entry_point_group, enabled_set):
        accept(cand)

    # 2. explicit modules / files
    for spec in modules:
        for cand in _read_explicit(spec, enabled_set):
            accept(cand)

    # 3. ~/.noeta/plugins (trusted unconditionally)
    for directory in user_dirs:
        for cand in _scan_dir(Path(directory), "user_dir", enabled_set):
            accept(cand)

    # 4. workspace dirs (trust-gated)
    for directory in workspace_dirs:
        path = Path(directory)
        if not is_trusted(path, store):
            warnings.warn(
                f"skipping untrusted workspace plugin directory {path} — "
                f"grant_trust it to load its plugins",
                UntrustedPluginDirWarning,
                stacklevel=2,
            )
            continue
        for cand in _scan_dir(path, "workspace_dir", enabled_set):
            accept(cand)

    return PluginSet(
        plugins=tuple(loaded),
        registry=reg,
        disabled_builtins=frozenset(disabled),
    )


def _enabled_pass(enabled_set: Optional[set], name: Optional[str]) -> bool:
    """Whether a candidate named ``name`` passes the allow-list."""
    return enabled_set is None or (name is not None and name in enabled_set)


def _read_builtins(
    builtins: "bool | Iterable[PluginManifest]",
    disabled: set,
    enabled_set: Optional[set],
) -> Iterator[_Candidate]:
    if builtins is False:
        return
    manifests: Iterable[PluginManifest]
    if builtins is True:
        manifests = _discover_builtins()
    else:
        manifests = builtins
    for manifest in manifests:
        if manifest.name in disabled:
            continue
        if not _enabled_pass(enabled_set, manifest.name):
            continue
        yield _Candidate(
            name=manifest.name,
            manifest=manifest,
            origin=f"builtin {manifest.name!r}",
            source="builtin",
        )


def _discover_builtins() -> tuple[PluginManifest, ...]:
    """The built-in plugin catalogue (spec D11), read via a **dynamic** import.

    ``noeta.builtins`` is the top-of-stack band; the loader sits below it, so the
    import must not be a static edge (import-linter would reject it). Importing
    the catalogue module runs no runtime capability code — it only builds the
    inert static manifests — so the zero-execution guarantee holds.
    """
    module = importlib.import_module("noeta.builtins")
    return tuple(module.builtin_manifests())


def _read_entry_points(
    entry_points: "bool | Iterable[Any]",
    group: str,
    enabled_set: Optional[set],
) -> Iterator[_Candidate]:
    for ep in _entry_point_iter(entry_points, group):
        ep_name = getattr(ep, "name", "<unnamed>")
        dist = getattr(ep, "dist", None)
        if dist is None:
            raise PluginError(
                f"entry point {ep_name!r}: no distribution attached, cannot "
                f"locate its {MANIFEST_BASENAME}"
            )
        manifest = read_distribution_manifest(dist)
        if manifest is None:
            raise PluginError(
                f"entry point {ep_name!r}: distribution ships no {MANIFEST_BASENAME} "
                f"package data"
            )
        if not _enabled_pass(enabled_set, manifest.name):
            continue
        yield _Candidate(
            name=manifest.name,
            manifest=manifest,
            origin=f"entry point {ep_name!r}",
            source="entry_point",
        )


def _entry_point_iter(entry_points: "bool | Iterable[Any]", group: str) -> Iterable[Any]:
    if entry_points is False:
        return ()
    if entry_points is True:
        eps = importlib.metadata.entry_points()
        try:
            return list(eps.select(group=group))
        except AttributeError:  # pragma: no cover — legacy mapping API
            return list(eps.get(group, []))
    return list(entry_points)


def _read_explicit(spec: str, enabled_set: Optional[set]) -> Iterator[_Candidate]:
    if _looks_like_path(spec):
        path = Path(spec)
        if path.suffix == ".toml":
            manifest = read_manifest_file(path)
            if _enabled_pass(enabled_set, manifest.name):
                yield _Candidate(
                    manifest.name, manifest, f"manifest {str(path)!r}", "module"
                )
            return
        if path.is_dir():
            yield from _scan_dir(path, "module", enabled_set)
            return
        cand = _load_py_file(path, "module", enabled_set)
        if cand is not None:
            yield cand
        return
    # A dotted module: importing it is authorized (explicit source, D4 gate).
    module = _import_module(spec)
    builder = _find_builder(module, spec)
    manifest = builder.manifest()
    if _enabled_pass(enabled_set, manifest.name):
        yield _Candidate(
            manifest.name,
            manifest,
            f"module {spec!r}",
            "module",
            dict(builder.resolved_objects),
        )


def _scan_dir(
    directory: Path, source: str, enabled_set: Optional[set]
) -> Iterator[_Candidate]:
    if not directory.is_dir():
        return
    # Sub-directories carrying a static manifest (zero execution).
    for sub in sorted(directory.iterdir()):
        if sub.is_dir():
            manifest_path = sub / MANIFEST_BASENAME
            if manifest_path.is_file():
                manifest = read_manifest_file(manifest_path)
                if _enabled_pass(enabled_set, manifest.name):
                    yield _Candidate(
                        manifest.name,
                        manifest,
                        f"manifest {str(manifest_path)!r}",
                        source,
                    )
    # Top-level single-file plugins (executed; trusted directory).
    for py in sorted(directory.glob("*.py")):
        if py.name.startswith("_"):
            continue
        cand = _load_py_file(py, source, enabled_set)
        if cand is not None:
            yield cand


def _load_py_file(
    path: Path, source: str, enabled_set: Optional[set]
) -> Optional[_Candidate]:
    # Gate on a statically declared name BEFORE executing the file. When the
    # file declares no static name, executing a trusted file to read its
    # manifest is acceptable (D1); the real name is gated after.
    declared = declared_plugin_name(path)
    if declared is not None and not _enabled_pass(enabled_set, declared):
        return None
    module = _exec_file(path)
    builder = _find_builder(module, str(path))
    manifest = builder.manifest()
    if not _enabled_pass(enabled_set, manifest.name):
        return None
    return _Candidate(
        manifest.name,
        manifest,
        f"file {str(path)!r}",
        source,
        dict(builder.resolved_objects),
    )


def _find_builder(module: ModuleType, origin: str) -> PluginBuilder:
    candidate = getattr(module, "plugin", None)
    if isinstance(candidate, PluginBuilder):
        return candidate
    found = [v for v in vars(module).values() if isinstance(v, PluginBuilder)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise PluginError(
            f"plugin {origin}: no module-level PluginBuilder found "
            f"(assign one to `plugin`)"
        )
    raise PluginError(
        f"plugin {origin}: multiple PluginBuilder instances found — expose one "
        f"as `plugin`"
    )


def _looks_like_path(spec: str) -> bool:
    import os

    if spec.endswith((".py", ".toml")):
        return True
    if os.sep in spec:
        return True
    return bool(os.altsep) and os.altsep in spec


def _import_module(spec: str) -> ModuleType:
    try:
        return importlib.import_module(spec)
    except Exception as exc:  # noqa: BLE001
        raise PluginError(
            f"plugin module {spec!r} failed to import: {exc}"
        ) from exc


def _exec_file(path: Path) -> ModuleType:
    modspec = importlib.util.spec_from_file_location(f"_noeta_plugin_{path.stem}", path)
    if modspec is None or modspec.loader is None:
        raise PluginError(f"plugin file {path} could not be loaded")
    module = importlib.util.module_from_spec(modspec)
    try:
        modspec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        raise PluginError(f"plugin file {path} failed to import: {exc}") from exc
    return module
