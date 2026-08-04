"""The plugin loader, its deterministic merge, and :class:`PluginSet`.

The loader is **surface-agnostic**: it reads static manifests
(:mod:`noeta.client.plugin_manifest`) from five gated sources, dedups plugin
names, and returns a :class:`PluginSet` that is listable and collision-checkable
**without executing plugin code**. Merge and collision run over the
:class:`~noeta.client.surfaces.SurfaceRegistry`, so discovery order affects only
error attribution, never the result; :meth:`PluginSet.resolve` is the single
import boundary, where plugin code finally runs. Collisions — including a
duplicate plugin name across sources — are errors naming both sides, never an
override.

The five sources, each with its own gate::

    0  built-in plugins        on by default; a host may disable individually
    1  entry points            enabled allow-list, applied BEFORE any import
    2  explicit modules/files  caller-specified = authorized
    3  ~/.noeta/plugins        the user's own machine = trusted
    4  workspace .noeta/plugins trust store (untrusted dir -> warn + skip)
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence

from noeta.client.plugin_manifest import (
    LITERAL_PARAM,
    MANIFEST_BASENAME,
    ManifestContribution,
    PluginManifest,
    declared_plugin_name,
    exec_plugin_file,
    find_builder,
    read_distribution_manifest,
    read_manifest_file,
    split_ref,
)
from noeta.client.mcp_server import SdkMcpServer
from noeta.client.options import PluginActivation
from noeta.client.plugins import (
    DEFAULT_TRUST_STORE,
    PLUGIN_ENTRY_POINT_GROUP,
    PluginError,
    PluginVersionWarning,
    UnnamedPluginFileWarning,
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


_log = logging.getLogger(__name__)


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

        The spelling is parsed by the shared
        :func:`~noeta.client.plugin_manifest.split_ref`; what lives here is the
        part only an importer can do — resolving the dotted form's module
        boundary by taking the longest importable prefix as the module.

        The dotted form backs off one component at a time, but only for a
        ``ModuleNotFoundError`` naming the prefix it just tried. An import fault
        *inside* a module that does exist (a missing third-party dependency, a
        raising module body) propagates as itself — never silently reinterpreted
        as "that was an attribute, not a module".
        """
        module_name, attr_parts = split_ref(ref)
        if module_name is not None:
            return self._import_module(module_name, ref), attr_parts
        parts = list(attr_parts)
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
    """The loaded, host-level set — listable and auditable without executing code.

    Holds the discovered plugins and the surface registry they were loaded
    against. :meth:`contributions` and :meth:`merged` read only static manifests;
    :meth:`resolve` is the boundary that imports plugin code.
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

        Optionally filtered to one ``surface``. A caller sees what an installed
        plugin contributes without any of its code running.
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
                    key=lambda e: (
                        _priority(e.contribution, e.plugin),
                        e.plugin,
                        e.contribution.name,
                    )
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

    def _process_surfaces(self) -> "frozenset[str]":
        """Wiring surfaces whose effect is process-wide (governance authority).

        Derived from the registry rather than hardcoded, so :meth:`process_hooks`
        collects a host-registered process-scoped surface too.
        """
        return frozenset(
            name
            for name in self.registry.names()
            if self.registry.get(name).plane == "wiring"
            and self.registry.get(name).activation_scope == "process"
        )

    def identity_activations(
        self, only: Optional[Iterable[str]] = None
    ) -> dict[str, "PluginActivation"]:
        """Resolve each **external** plugin's identity-plane contributions.

        Keyed by plugin name, the map ``Client`` passes to
        :func:`~noeta.client.options.compile_options`: an agent that names one of
        these in its ``plugins`` list pulls in that plugin's tools / child agents
        / content kinds / prompt fragments — feature surfaces follow activation.
        Built-in plugins are excluded (their feature effect is the
        capability-flag vocabulary compile handles by name); ``only`` restricts
        resolution to the activated names (see :meth:`_external`).

        The per-surface routing is **table-driven**: each identity surface
        declares an ``activation_binding`` naming the ``PluginActivation``
        channel it feeds, so a host-registered identity surface projects without
        editing this method, and ``SurfaceSpec`` refuses an unbound identity
        surface at registration — before any plugin is even loaded.

        This executes plugin code (resolve imports the refs) — the Client build
        boundary, never a mid-session turn.
        """
        out: dict[str, PluginActivation] = {}
        identity = self._identity_surfaces()
        for p in self._external(only):
            # One bucket per ``PluginActivation`` channel, keyed by the binding
            # a surface declares. ``"elsewhere"`` has no bucket: the surface is
            # identity-plane but a per-agent projection owns its resolution
            # (``control_tool`` rides :meth:`activation_control_tools`).
            bound: dict[str, list[Any]] = {
                "tool": [],
                "agent": [],
                "content_kind": [],
                "prompt_fragment": [],
                "policy": [],
            }
            # Every activated external plugin gets an entry — ``compile_options``
            # reads this map as the "is that a known activation name?" vocabulary,
            # so a plugin with only wiring contributions must still appear. Only
            # the resolution is skipped.
            resolved = self._resolve_plugin(p) if self._declares(p, identity) else ()
            for rc in resolved:
                spec = self.registry.get(rc.surface)
                if spec.plane != "identity":
                    continue
                binding = spec.activation_binding
                if binding == "elsewhere":
                    continue
                # A registered identity surface always carries a legal binding
                # (SurfaceSpec.__post_init__ enforces it), so this lookup is
                # total; the guard keeps a hand-built spec from failing silently.
                bucket = bound.get(binding) if binding is not None else None
                if bucket is None:
                    raise PluginError(
                        f"plugin {p.name!r}: identity-plane surface "
                        f"{rc.surface!r} declares activation_binding="
                        f"{binding!r}, which names no PluginActivation channel"
                    )
                # ``tool`` / ``policy`` carry the bare value; the named channels
                # keep ``(contribution name, value)`` so downstream ordering can
                # sort by name.
                if binding in ("tool", "policy"):
                    bucket.append(rc.value)
                else:
                    bucket.append((rc.name, rc.value))
            # Single-valued surface: at most one per plugin (the merge would
            # already reject two). Cross-plugin collisions with the base
            # ``Options.policy`` / another active plugin are caught at compile.
            policies = bound["policy"]
            out[p.name] = PluginActivation(
                tools=tuple(bound["tool"]),
                agents=tuple(bound["agent"]),
                content_kinds=tuple(
                    v for _n, v in sorted(bound["content_kind"], key=lambda e: e[0])
                ),
                prompt_fragments=tuple(bound["prompt_fragment"]),
                policy=policies[0] if policies else None,
            )
        return out

    def activation_transforms(
        self, only: Optional[Iterable[str]] = None
    ) -> dict[str, tuple[tuple[int, str, Any], ...]]:
        """Resolve each **external** plugin's ``tool_result_transform`` stages.

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
        """Resolve each **external** plugin's compose-time ``reminder`` renders.

        Returns ``plugin name -> ((priority, contribution name, render), …)``.
        ``Client`` turns these into
        :class:`~noeta.context.reminders.ReminderSpec` s for the agents that
        activate the plugin; the composer runs them at the tail of the dynamic
        suffix in ``(priority, name)`` order. Same plane / scoping / execution
        boundary as :meth:`activation_transforms`.
        """
        return self._activation_params("reminder", _priority, only)

    def activation_session_packs(
        self, only: Optional[Iterable[str]] = None
    ) -> dict[str, tuple[tuple[int, str, Any], ...]]:
        """Resolve each **external** plugin's ``session_pack`` factories.

        Returns ``plugin name -> ((priority, contribution name, factory), …)``
        for the wiring-plane, per-agent ``session_pack`` surface. The host
        appends these after the built-in packs (which it resolves from the
        built-in manifests) and hands the merged, ``(priority, plugin, name)``-
        ordered entry list to the kernel builder's generic pack loop — an
        agent that activates the plugin gets its pack's tools / content kinds
        / exports assembled into the session. Same plane / scoping / execution
        boundary as :meth:`activation_transforms`.
        """
        return self._activation_params("session_pack", _priority, only)

    def activation_control_tools(
        self, only: Optional[Iterable[str]] = None
    ) -> dict[str, tuple[tuple[int, str, Any], ...]]:
        """Resolve each **external** plugin's ``control_tool`` factories.

        Returns ``plugin name -> ((priority, contribution name, factory), …)``
        for the ``control_tool`` surface. The host merges these — after the
        built-in control tools it resolves from the built-in manifests — with the
        kernel's remaining internal entries and hands the union to the builder's
        dual-priority mount loop, so an agent that activates the plugin gets its
        control tool mounted (schema rendered in band, translate routed in band).
        The ``control_tool`` surface is declared identity-plane because it enters
        durable identity, but it is RESOLVED here the SAME wiring way as
        :meth:`activation_session_packs` (a per-agent projection, ordered
        ``(priority, plugin, name)``); :meth:`identity_activations` deliberately
        skips it. Same scoping / execution boundary as
        :meth:`activation_transforms`.
        """
        return self._activation_params("control_tool", _priority, only)

    def activation_reminder_providers(
        self, only: Optional[Iterable[str]] = None
    ) -> dict[str, tuple[tuple[tuple[str, ...], str, Any], ...]]:
        """Resolve each **external** plugin's recorded ``reminder_provider`` s.

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
        param: Callable[[ManifestContribution, str], Any],
        only: Optional[Iterable[str]],
    ) -> dict[str, tuple[tuple[Any, str, Any], ...]]:
        """Shared body of the per-agent wiring projections.

        Each returns ``plugin -> ((<ordering param>, contribution name, value), …)``
        for one surface; ``param`` reads the surface's ordering / binding param off
        the static manifest entry, and is scoped to ``surface`` so a defect in an
        unrelated contribution's params is that surface's problem, not this one's.
        """
        out: dict[str, tuple[tuple[Any, str, Any], ...]] = {}
        wanted = frozenset({surface})
        for p in self._external(only):
            if not self._declares(p, wanted):
                continue
            by_key = {
                (c.surface, c.name): param(c, p.name)
                for c in p.manifest.contributions
                if c.surface == surface
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
        """Resolve every loaded **external** plugin's guard + observer values.

        Governance surfaces do **not** follow per-agent activation: a loaded
        guard / observer is in force for every agent in the process, because one
        that applied only after some agent opted in would not be governance
        authority. This returns ``(guards, observers)`` in deterministic
        ``(plugin, name)`` order for ``Client`` to fold into its process-wide
        guard stack and observer subscriptions. Built-in governance guards are
        the engine's own default stack, wired elsewhere, so they are excluded;
        resolution is still limited to plugins whose static manifest *declares* a
        guard or observer, so a plugin that governs nothing is never imported to
        discover that.

        The governance set is **derived from the registry** — every wiring
        surface scoped ``"process"`` is process-wide authority by definition — so
        a host that registers one is seen here without editing this method. It is
        not silently absorbed, though: the returned pair has exactly two buckets
        because ``Client`` wires guards and observers into two different runtime
        seams, so a third process surface has nowhere to go and is **refused
        loudly**. Routing it into ``guards`` by default would hand the engine a
        value that is not a ``Guard``, turning a build-time configuration error
        into a crash on the first tool call.
        """
        guards: list[Any] = []
        observers: list[Any] = []
        governance = self._process_surfaces()
        for p in sorted(self._external(None), key=lambda x: x.name):
            if not self._declares(p, governance):
                continue
            resolved = sorted(self._resolve_plugin(p), key=lambda r: r.name)
            for rc in resolved:
                if rc.surface not in governance:
                    continue
                if rc.surface == "observer":
                    observers.append(rc.value)
                elif rc.surface == "guard":
                    guards.append(rc.value)
                else:
                    raise PluginError(
                        f"plugin {p.name!r}: process-scoped wiring surface "
                        f"{rc.surface!r} has no runtime channel — Client wires "
                        f"only 'guard' and 'observer' process-wide. Give the "
                        f"surface a per-agent scope, or wire its channel here "
                        f"before registering it as process-scoped."
                    )
        return tuple(guards), tuple(observers)

    # -- host-plane projections (host-wired, never activation-scoped) ------

    def _host_surface(
        self, surface: str
    ) -> Iterator[tuple[LoadedPlugin, ResolvedContribution]]:
        """Every external plugin's contributions to one **host-wired** surface.

        Host-plane surfaces do not follow per-agent activation — the host binds
        them once for the process — so the scope is every loaded external plugin
        (built-ins excluded the same way every other projection excludes them),
        with no ``only`` filter. Resolution is still limited to plugins whose
        static manifest declares the surface, so a plugin that ships no skills
        is never imported to discover that.
        """
        wanted = frozenset({surface})
        for p in sorted(self._external(None), key=lambda x: x.name):
            if not self._declares(p, wanted):
                continue
            for rc in sorted(self._resolve_plugin(p), key=lambda r: r.name):
                if rc.surface == surface:
                    yield p, rc

    def host_skills_dirs(self) -> tuple[Path, ...]:
        """Resolve every external plugin's ``skills`` path contributions.

        Returns the directories in ``(plugin, contribution name)`` order. The
        host appends them to the **lowest** tier of the skill merge (the
        built-in tier), so a plugin's skill pack is shadowed by a same-named
        global (``~/.noeta/skills``) or workspace (``.noeta/skills``) skill —
        the user's own definition always wins — while still being visible in
        every session the host builds.

        **Paths must be absolute.** A manifest is read from several shapes (a
        distribution's package data, a bare ``.toml``, a single ``.py``), and
        those roots disagree about what a relative path would even be relative
        to, so resolving one would silently point at different directories
        depending on how the plugin was installed. A relative path is a loud
        :class:`PluginError` naming the plugin; an author writes
        ``str(Path(__file__).parent / "skills")`` in the single-file form, or
        resolves the path against ``__file__`` in the package form.

        A path that does not exist on disk is **not** an error: the indexer
        treats a missing root as an empty tier, so a plugin whose skills ship
        only in some installs degrades to contributing nothing.
        """
        out: list[Path] = []
        for plugin, rc in self._host_surface("skills"):
            path = Path(str(rc.value))
            if not path.is_absolute():
                raise PluginError(
                    f"plugin {plugin.name!r}: skills contribution {rc.name!r} "
                    f"declares the relative path {str(rc.value)!r} — a skills "
                    f"path must be ABSOLUTE, because a manifest read from a "
                    f"wheel, a .toml or a single .py file has no single "
                    f"directory to resolve it against. Build it from the "
                    f"module's own location (e.g. "
                    f"str(Path(__file__).parent / 'skills'))."
                )
            out.append(path)
        return tuple(out)

    def host_mcp_servers(self) -> tuple[tuple[str, str, SdkMcpServer], ...]:
        """Resolve every external plugin's ``mcp_server`` contributions.

        Returns ``((alias, plugin name, server), …)`` in ``(plugin, alias)``
        order, where the **alias** is the contribution's manifest name (the
        surface's ``alias`` collision key, so two plugins claiming one alias
        already failed at :meth:`merged`). ``Client`` folds these into the
        effective ``Options.mcp_servers`` and refuses an alias the recipe
        already uses — the same "no override" rule the merge applies.

        The value must be an :class:`~noeta.client.mcp_server.SdkMcpServer`,
        exactly what ``Options.mcp_servers`` carries: an in-process bundle of
        ``@tool`` functions. Enforced here rather than in the surface validator
        so the error names the plugin. A **remote** MCP server stays a host
        concern — it is addressed per turn by alias through
        ``HostConfig.mcp_server_resolver``, whose specs carry credentials a
        static manifest must never hold.
        """
        out: list[tuple[str, str, SdkMcpServer]] = []
        for plugin, rc in self._host_surface("mcp_server"):
            if not isinstance(rc.value, SdkMcpServer):
                raise PluginError(
                    f"plugin {plugin.name!r}: mcp_server contribution "
                    f"{rc.name!r} resolved to {type(rc.value).__name__}, not an "
                    f"SdkMcpServer — an mcp_server contribution carries the same "
                    f"value Options.mcp_servers takes (build it with "
                    f"noeta.sdk.create_sdk_mcp_server). A REMOTE server is not a "
                    f"plugin contribution: wire it through "
                    f"HostConfig.mcp_server_resolver."
                )
            out.append((rc.name, plugin.name, rc.value))
        return tuple(out)

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


def _priority(c: ManifestContribution, plugin: str) -> int:
    """A priority-ordered contribution's integer ``priority`` param.

    An absent ``priority`` is 0 — the surface's documented default. A *present*
    one that is not an integer is a loud :class:`PluginError`, the same
    strictness the built-in accessors (:mod:`noeta.client.parts`) apply to the
    first-party manifests: silently coercing ``priority = "high"`` to 0 puts the
    contribution at the front of a band the author meant to sit at the back of,
    and every symptom of that shows up somewhere else (a reminder rendering
    first, a pack's tools inserted before another's) with nothing pointing back
    at the manifest. ``True`` is not an integer here for the same reason it is
    not one in the built-in path.
    """
    if "priority" not in c.params:
        return 0
    value = c.params["priority"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PluginError(
            f"plugin {plugin!r}: contribution {c.name!r} to surface "
            f"{c.surface!r} declares priority={value!r}, which is not an "
            f"integer — a priority-ordered surface orders on an int, so this "
            f"contribution has no defined position. Declare an integer band."
        )
    return value


#: The recording seam a ``reminder_provider`` binds to when its manifest names none.
DEFAULT_REMINDER_SEAM = "turn_intake"


def _seams(c: ManifestContribution, plugin: str) -> tuple[str, ...]:  # noqa: ARG001
    """A ``reminder_provider``'s declared recording seams.

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
# ``requires-noeta`` — enforced as a warning, refused under ``strict``
# ---------------------------------------------------------------------------


#: The distribution whose installed version ``requires-noeta`` is compared to.
#: The manifest field names *noeta*, and the SDK is the package a plugin
#: actually contributes surfaces to.
_SDK_DISTRIBUTION = "noeta-sdk"

#: One clause of a ``requires-noeta`` specifier: an operator plus a dotted
#: release. Whitespace anywhere is tolerated; a clause this does not match makes
#: the WHOLE specifier unrecognised (a half-understood range is worse than an
#: unenforced one).
_CLAUSE = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*([0-9]+(?:\.[0-9]+)*)\s*$")


def _release(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _padded(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Both releases at equal length, so ``0.5`` and ``0.5.0`` compare equal."""
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def _clause_holds(op: str, installed: tuple[int, ...], bound: tuple[int, ...]) -> bool:
    left, right = _padded(installed, bound)
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">":
        return left > right
    return left < right


def _installed_release(installed: str) -> Optional[tuple[int, ...]]:
    """``installed`` as a release tuple, or ``None`` when it does not parse.

    A ``+local`` tail and one ``-suffix`` are stripped first; what remains must
    be dotted numerics, so a run-together pre-release (``1.0.0rc1``,
    ``0.6.1.dev3`` — the canonical forms ``importlib.metadata`` returns) reads
    as unparseable rather than guessed at.
    """
    try:
        return _release(installed.split("+", 1)[0].split("-", 1)[0])
    except ValueError:
        return None


def _specifier_satisfied(spec: str, installed: str) -> Optional[bool]:
    """Whether ``installed`` satisfies ``spec``; ``None`` when either side is
    unrecognised (a specifier this evaluator does not parse, or an installed
    version ``_installed_release`` cannot read).

    A deliberately minimal, dependency-free evaluator — ``>=X.Y[.Z]``, ``<``,
    ``>``, ``<=``, ``==``, ``!=``, AND-joined by commas. It exists so
    ``requires-noeta`` can mean something without dragging ``packaging`` into
    the SDK's install for one field; anything richer (extras, epochs,
    pre-release markers, ``~=``) reads as unrecognised and is reported as such
    rather than guessed at.
    """
    clauses = [part for part in spec.split(",") if part.strip()]
    if not clauses:
        return None
    current = _installed_release(installed)
    if current is None:
        return None
    satisfied = True
    for clause in clauses:
        match = _CLAUSE.match(clause)
        if match is None:
            return None
        op, bound = match.group(1), match.group(2)
        satisfied = satisfied and _clause_holds(op, current, _release(bound))
    return satisfied


def _installed_sdk_version() -> Optional[str]:
    """The installed ``noeta-sdk`` version, or ``None`` when it has no metadata.

    An in-repo checkout run straight off the source tree has no distribution
    metadata; there is no version to compare against, so every range counts as
    satisfied and the miss is logged at debug rather than warned — a developer
    running the tree is not the audience for a packaging warning.
    """
    try:
        return importlib.metadata.version(_SDK_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        _log.debug(
            "plugin: %s has no installed distribution metadata; "
            "requires-noeta ranges are treated as satisfied",
            _SDK_DISTRIBUTION,
        )
        return None


def _check_requires_noeta(
    manifest: PluginManifest, origin: str, *, strict: bool
) -> None:
    """Evaluate one manifest's ``requires-noeta``; warn, or raise under ``strict``."""
    declared = manifest.requires_noeta
    if not declared or not declared.strip():
        return
    installed = _installed_sdk_version()
    if installed is None:
        return
    verdict = _specifier_satisfied(declared, installed)
    if verdict is None:
        # Name the side that actually failed: an unparseable INSTALLED version
        # (a pre-release like ``1.0.0rc1``) is not the plugin author's doing,
        # and a warning blaming their specifier sends whoever debugs it to the
        # wrong file. Either way enforcement is skipped, never guessed at.
        if _installed_release(installed) is None:
            warnings.warn(
                f"plugin {manifest.name!r} ({origin}): requires-noeta "
                f"{declared!r} not enforced — installed {_SDK_DISTRIBUTION} "
                f"version {installed!r} is unparseable",
                PluginVersionWarning,
                stacklevel=2,
            )
        else:
            warnings.warn(
                f"plugin {manifest.name!r} ({origin}): unrecognized "
                f"requires-noeta specifier {declared!r} — not enforced",
                PluginVersionWarning,
                stacklevel=2,
            )
        return
    if verdict:
        return
    message = (
        f"plugin {manifest.name!r} ({origin}) requires noeta {declared}, but "
        f"{_SDK_DISTRIBUTION} {installed} is installed"
    )
    if strict:
        raise PluginError(message + " — refused (load_plugins(strict=True))")
    warnings.warn(message, PluginVersionWarning, stacklevel=2)


# ---------------------------------------------------------------------------
# The five-source loader
# ---------------------------------------------------------------------------


#: Built-ins whose capability the compiled agent depends on unconditionally, so
#: ``disabled_builtins`` cannot express a removal. Refusing loudly is the honest
#: answer: silently dropping the name from the catalogue while the capability
#: stays wired would make the disable read as effective.
_NON_REMOVABLE_BUILTINS: dict[str, str] = {
    "react": (
        "built-in 'react' cannot be disabled: it supplies the DEFAULT decision "
        "policy, and every compiled AgentSpec pins that identity as "
        "POLICY_REF ('react', '1') — an agent with no policy has no defined "
        "identity or parity. The default brain is REPLACEABLE, not removable: "
        "activate a plugin contributing the 'policy' surface and its ref "
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
    strict: bool = False,
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

    Every accepted candidate's ``requires-noeta`` range is evaluated against the
    installed ``noeta-sdk`` version. An unsatisfied range warns
    (:class:`~noeta.client.plugins.PluginVersionWarning`) and the plugin still
    loads; ``strict=True`` turns that warning into a :class:`PluginError`, which
    is the setting a deployment uses when "tested against this SDK" is a release
    gate rather than a hint. A specifier this loader cannot parse always warns
    and is never enforced, in either mode.

    A load fault (bad manifest, missing manifest, a broken file) raises
    :class:`PluginError` naming the plugin — never a silent skip, except the
    untrusted-workspace warning above and a single-file plugin whose name cannot
    be read statically while an ``enabled`` allow-list is in force.
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
        _check_requires_noeta(cand.manifest, cand.origin, strict=strict)
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
    """The built-in plugin catalogue, read via a **dynamic** import.

    ``noeta.builtins`` is the top-of-stack band and the loader sits below it, so
    the import must not be a static edge (import-linter rejects one). Importing
    the catalogue module runs no capability code — it only builds the inert
    static manifests — so the zero-execution guarantee holds.
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
    # A dotted module: naming it explicitly is what authorizes the import.
    module = _import_module(spec)
    builder = find_builder(module, spec)
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
    # Gate on a statically declared name BEFORE executing the file. When the file
    # declares no static name, executing a trusted file to read its manifest is
    # acceptable — but ONLY when there is no allow-list. An ``enabled`` list is
    # an authorization decision made per name, so a file whose name cannot be
    # read without running it has no name to authorize: executing it to find out
    # what it would have been called is exactly the "code from an unauthorized
    # source runs anyway" hole the static-name extraction exists to close. Skip
    # it, loudly enough that a legitimate plugin's author learns to declare a
    # ``noeta_plugin_name`` literal.
    declared = declared_plugin_name(path)
    if declared is None and enabled_set is not None:
        warnings.warn(
            f"skipping plugin file {path}: an 'enabled' allow-list is in force "
            f"and this file declares no statically readable plugin name, so it "
            f"cannot be authorized without executing it. Add a module-level "
            f"noeta_plugin_name = \"...\" literal (or a "
            f"PluginBuilder(\"...\") literal) to make it gateable.",
            UnnamedPluginFileWarning,
            stacklevel=2,
        )
        return None
    if declared is not None and not _enabled_pass(enabled_set, declared):
        return None
    module = exec_plugin_file(path, module_prefix="_noeta_plugin_")
    builder = find_builder(module, str(path))
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


