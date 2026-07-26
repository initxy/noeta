"""Plugin mechanism — discoverable bundles of typed ``Options`` contributions.

A **Plugin** is a Python module exporting a ``noeta_plugin(api)`` factory (see
``docs/adr/plugin-contribution-bundles.md``). The factory receives a
:class:`PluginAPI` accumulator and populates the *open extension surfaces* the
:class:`~noeta.client.options.Options` recipe already exposes — tools, guards,
observers, a provider, content kinds, child agents — plus two host-plane
surfaces a plugin may ship (MCP server specs and skill directories). Loading
collects those contributions and :func:`merge_plugins` folds them
**deterministically** into a base ``Options`` before ``compile_options``; the
merge is order-independent and any name collision fails loudly, naming both
contributors. There is no override flag (ADR decision D4).

This module is deliberately *deep*: a small surface (``PluginAPI`` +
``load_plugins`` + ``merge_plugins``) hiding discovery, trust gating,
factory-signature dispatch, deterministic sorting, and collision detection. It
adds **zero** new power to the engine — plugins substitute nothing, they only
populate the seams that were already open (ADR rationale). No engine or
composer code is touched.

Two planes, mirroring the ``Options`` / ``HostConfig`` split
(``docs/adr/plugin-contribution-bundles.md``):

* **Identity plane** — tools, agents, content kinds, provider, guards,
  observers — folds into the returned ``Options`` and therefore into
  ``AgentSpec`` identity (tools + agents) or its wiring (the rest).
* **Host plane** — MCP server specs (alias → connectable spec) and skill
  directories — are validated and collision-checked by :func:`merge_plugins`
  but do **not** enter ``Options`` (they have no ``Options`` surface; a host
  wires them into its ``HostConfig`` via :func:`merged_mcp_servers` /
  :func:`merged_skill_dirs`). Keeping them off ``Options`` preserves the
  identity-plane/host-plane split.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import os
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    Optional,
    Sequence,
)

from noeta.client.options import Options, AgentDefinition, _compile_tool
from noeta.client.parts import BUILTIN_TOOL_CLASSES
from noeta.context.content_channel import ContentKindSpec


__all__ = [
    "PluginAPI",
    "PluginContributions",
    "LoadedPlugin",
    "PluginError",
    "UntrustedPluginDirWarning",
    "load_plugins",
    "merge_plugins",
    "merged_mcp_servers",
    "merged_skill_dirs",
    "grant_trust",
    "is_trusted",
    "PLUGIN_ENTRY_POINT_GROUP",
    "DEFAULT_TRUST_STORE",
]


#: The entry-point group the SDK owns for runtime-plane plugins (ADR D2).
PLUGIN_ENTRY_POINT_GROUP = "noeta.plugins"

#: The default trust store recording operator-approved workspace plugin
#: directories (JSON ``{"trusted": [abs path, ...]}``).
DEFAULT_TRUST_STORE = Path.home() / ".noeta" / "trust.json"

#: The collision source label for names already present on the base ``Options``.
_BASE_SOURCE = "<base options>"

#: Factory attribute a plugin module may set to override its derived name.
_NAME_ATTR = "noeta_plugin_name"

#: The factory a plugin module must export.
_FACTORY_ATTR = "noeta_plugin"


class PluginError(RuntimeError):
    """A plugin failed to load, or its contributions collided at merge.

    Raised loudly — never swallowed — so a bad plugin fails the client build
    at startup rather than a mid-session turn. The single documented exception
    is an *untrusted* workspace directory, which is skipped with an
    :class:`UntrustedPluginDirWarning` instead of raising.
    """


class UntrustedPluginDirWarning(UserWarning):
    """A workspace plugin directory was skipped because it is not trusted."""


# ---------------------------------------------------------------------------
# Contributions value object + the accumulator API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginContributions:
    """The immutable set of contributions one plugin factory produced.

    Every collection preserves the plugin's contribution order; global
    ordering (across plugins) is imposed by :func:`merge_plugins`, so a
    plugin author never needs to sort. ``tools`` / ``agents`` / ``mcp_servers``
    carry the resolved collision key alongside the value.
    """

    #: ``(tool_ref_name, entry)`` — ``entry`` is the original ``add_tool``
    #: argument (a built-in name string or a ``.ref``-bearing tool object).
    tools: tuple[tuple[str, Any], ...] = ()
    guards: tuple[Any, ...] = ()
    observers: tuple[Any, ...] = ()
    provider: Optional[Any] = None
    content_kinds: tuple[ContentKindSpec, ...] = ()
    #: ``(agent_name, AgentDefinition)``.
    agents: tuple[tuple[str, AgentDefinition], ...] = ()
    #: ``(alias, spec)`` — a host-plane connectable MCP server spec.
    mcp_servers: tuple[tuple[str, Any], ...] = ()
    skill_dirs: tuple[Path, ...] = ()


class PluginAPI:
    """The accumulator a plugin factory receives — a *pure* recorder.

    Holds **no live engine handles**: methods only append typed contributions,
    validated eagerly where the check is cheap (a bad tool entry, a non-spec
    content kind, a second provider, a duplicate name *within one plugin* all
    raise here, at factory time, naming the plugin). Cross-plugin and
    base-``Options`` collisions are the job of :func:`merge_plugins`.
    """

    def __init__(self, plugin_name: str) -> None:
        self._plugin_name = plugin_name
        self._tools: list[tuple[str, Any]] = []
        self._guards: list[Any] = []
        self._observers: list[Any] = []
        self._provider: Optional[Any] = None
        self._provider_set = False
        self._content_kinds: list[ContentKindSpec] = []
        self._agents: list[tuple[str, AgentDefinition]] = []
        self._mcp_servers: list[tuple[str, Any]] = []
        self._skill_dirs: list[Path] = []
        # within-plugin name guards
        self._tool_names: set[str] = set()
        self._agent_names: set[str] = set()
        self._kinds: set[str] = set()
        self._aliases: set[str] = set()

    # -- contributions ---------------------------------------------------

    def add_tool(self, tool: Any) -> None:
        """Contribute one tool: a built-in name string or a ``.ref``-bearing tool.

        The entry is resolved to its :class:`~noeta.agent.spec.ToolRef` eagerly
        (an unknown built-in name or a bad ``.ref`` raises here), and its name is
        the collision key.
        """
        ref = _compile_tool(tool)
        if ref.name in self._tool_names:
            raise self._dup("tool", ref.name)
        self._tool_names.add(ref.name)
        self._tools.append((ref.name, tool))

    def add_guard(self, guard: Any) -> None:
        """Contribute a :class:`~noeta.protocols.hooks.Guard` (no collision key)."""
        if guard is None:
            raise PluginError(
                f"plugin {self._plugin_name!r}: add_guard received None"
            )
        self._guards.append(guard)

    def add_observer(self, observer: Any) -> None:
        """Contribute a post-commit event ``Observer`` (must be callable)."""
        if not callable(observer):
            raise PluginError(
                f"plugin {self._plugin_name!r}: add_observer requires a "
                f"callable; got {type(observer).__name__}"
            )
        self._observers.append(observer)

    def set_provider(self, provider: Any) -> None:
        """Set the single LLM provider. A second call in the same plugin raises."""
        if provider is None:
            raise PluginError(
                f"plugin {self._plugin_name!r}: set_provider received None"
            )
        if self._provider_set:
            raise PluginError(
                f"plugin {self._plugin_name!r}: set_provider called twice — "
                f"the provider is single-valued"
            )
        self._provider = provider
        self._provider_set = True

    def add_content_kind(self, spec: ContentKindSpec) -> None:
        """Contribute a :class:`ContentKindSpec` (keyed by ``spec.kind``)."""
        if not isinstance(spec, ContentKindSpec):
            raise PluginError(
                f"plugin {self._plugin_name!r}: add_content_kind requires a "
                f"ContentKindSpec; got {type(spec).__name__}"
            )
        if spec.kind in self._kinds:
            raise self._dup("content kind", spec.kind)
        self._kinds.add(spec.kind)
        self._content_kinds.append(spec)

    def add_agent(self, name: str, definition: AgentDefinition) -> None:
        """Contribute a child agent (keyed by ``name``)."""
        if not isinstance(name, str) or not name.strip():
            raise PluginError(
                f"plugin {self._plugin_name!r}: add_agent requires a "
                f"non-empty name"
            )
        if not isinstance(definition, AgentDefinition):
            raise PluginError(
                f"plugin {self._plugin_name!r}: add_agent requires an "
                f"AgentDefinition; got {type(definition).__name__}"
            )
        if name in self._agent_names:
            raise self._dup("agent", name)
        self._agent_names.add(name)
        self._agents.append((name, definition))

    def add_mcp_server(self, alias: str, spec: Any) -> None:
        """Contribute a host-plane MCP server spec (keyed by ``alias``).

        The ``spec`` is an opaque connectable server spec (e.g.
        ``McpServerSpec`` / ``McpHttpServerSpec``); a host wires it into its
        ``HostConfig.mcp_server_resolver``. It does not enter ``Options``.
        """
        if not isinstance(alias, str) or not alias.strip():
            raise PluginError(
                f"plugin {self._plugin_name!r}: add_mcp_server requires a "
                f"non-empty alias"
            )
        if spec is None:
            raise PluginError(
                f"plugin {self._plugin_name!r}: add_mcp_server received None"
            )
        if alias in self._aliases:
            raise self._dup("mcp alias", alias)
        self._aliases.add(alias)
        self._mcp_servers.append((alias, spec))

    def add_skill_dir(self, path: Any) -> None:
        """Contribute a skill directory (host-plane; no collision key).

        Coerced to an absolute :class:`~pathlib.Path`. Existence is a host
        concern (the directory may be provisioned later), so it is not required
        here — only that the argument is a non-empty path.
        """
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise PluginError(
                f"plugin {self._plugin_name!r}: add_skill_dir requires a "
                f"non-empty path"
            )
        self._skill_dirs.append(Path(path).absolute())

    # -- internal --------------------------------------------------------

    def _dup(self, kind: str, name: str) -> PluginError:
        return PluginError(
            f"plugin {self._plugin_name!r} contributes {kind} {name!r} twice"
        )

    def _contributions(self) -> PluginContributions:
        return PluginContributions(
            tools=tuple(self._tools),
            guards=tuple(self._guards),
            observers=tuple(self._observers),
            provider=self._provider,
            content_kinds=tuple(self._content_kinds),
            agents=tuple(self._agents),
            mcp_servers=tuple(self._mcp_servers),
            skill_dirs=tuple(self._skill_dirs),
        )


@dataclass(frozen=True)
class LoadedPlugin:
    """One discovered + invoked plugin: its name and its contributions."""

    name: str
    contributions: PluginContributions = field(default_factory=PluginContributions)


# ---------------------------------------------------------------------------
# Trust store
# ---------------------------------------------------------------------------


def _read_trusted(store: Path) -> list[str]:
    try:
        raw = store.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginError(
            f"trust store {store} is not valid JSON: {exc}"
        ) from exc
    trusted = data.get("trusted", []) if isinstance(data, Mapping) else []
    return [str(p) for p in trusted]


def is_trusted(path: Any, store: Optional[Path] = None) -> bool:
    """Return whether ``path``'s absolute form is recorded in the trust store.

    ``store`` defaults to :data:`DEFAULT_TRUST_STORE`. A missing store is
    treated as "nothing trusted" (returns ``False``), never an error.
    """
    store = Path(store) if store is not None else DEFAULT_TRUST_STORE
    target = str(Path(path).absolute())
    return target in _read_trusted(store)


def grant_trust(path: Any, store: Optional[Path] = None) -> None:
    """Record ``path``'s absolute form as trusted (idempotent).

    Creates the store (and its parent directory) if absent. Safe to call
    repeatedly — an already-trusted path is left unchanged.
    """
    store = Path(store) if store is not None else DEFAULT_TRUST_STORE
    target = str(Path(path).absolute())
    trusted = _read_trusted(store)
    if target not in trusted:
        trusted.append(target)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"trusted": trusted}, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Discovery + loading
# ---------------------------------------------------------------------------


def load_plugins(
    *,
    entry_points: "bool | Iterable[Any]" = False,
    modules: Sequence[str] = (),
    trusted_dirs: Sequence[Any] = (),
    workspace_dirs: Sequence[Any] = (),
    enabled: Optional[Iterable[str]] = None,
    config: Optional[Mapping[str, dict]] = None,
    trust_store: Optional[Path] = None,
    entry_point_group: str = PLUGIN_ENTRY_POINT_GROUP,
) -> list[LoadedPlugin]:
    """Discover and invoke plugins from up to three opt-in sources.

    Every source is off unless its argument is supplied:

    * **entry points** — pass ``entry_points=True`` to discover the
      ``noeta.plugins`` group via ``importlib.metadata``, or pass an iterable
      of entry-point-like objects (``.name`` + ``.load()``) to inject them (the
      testing seam). Each loaded object is the plugin's ``noeta_plugin`` factory.
    * **explicit modules / files** — ``modules`` holds dotted module paths
      (``importlib.import_module``) or ``.py`` file paths (loaded by location).
      Each must export ``noeta_plugin``.
    * **directories** — ``trusted_dirs`` are scanned unconditionally (e.g.
      ``~/.noeta/plugins``); ``workspace_dirs`` are scanned only when the
      directory's absolute path is recorded in the trust store, otherwise the
      directory is skipped with a loud :class:`UntrustedPluginDirWarning` (never
      silently). Both scan top-level ``*.py`` (files starting with ``_`` are
      skipped); each file must export ``noeta_plugin``.

    ``enabled`` (when not ``None``) is an allow-list of plugin names: only those
    load; every other candidate is skipped before it is imported. ``config``
    maps plugin name → a ``dict`` passed as the factory's second argument, but
    **only** to factories whose signature declares a second parameter.

    A broken plugin (import error, missing factory, factory raise) fails loudly
    with :class:`PluginError` naming the plugin — never a silent skip (the one
    exception is the untrusted-directory warning above).
    """
    enabled_set = set(enabled) if enabled is not None else None
    config_map = dict(config) if config is not None else {}
    seen_names: dict[str, str] = {}
    loaded: list[LoadedPlugin] = []

    def _accept(name: str, factory: Any, origin: str) -> None:
        if enabled_set is not None and name not in enabled_set:
            return
        if name in seen_names:
            raise PluginError(
                f"duplicate plugin name {name!r}: found in both "
                f"{seen_names[name]} and {origin}"
            )
        seen_names[name] = origin
        loaded.append(
            LoadedPlugin(
                name=name,
                contributions=_invoke_factory(
                    name, factory, config_map.get(name)
                ),
            )
        )

    # -- 1. entry points --------------------------------------------------
    for ep in _entry_point_iter(entry_points, entry_point_group):
        name = ep.name
        if enabled_set is not None and name not in enabled_set:
            continue
        factory = _load_entry_point(name, ep)
        _accept(name, factory, f"entry point {name!r}")

    # -- 2. explicit modules / files -------------------------------------
    for spec in modules:
        candidate = _candidate_module_name(spec)
        if enabled_set is not None and candidate not in enabled_set:
            # Cannot honour a module-level name override without importing,
            # but the derived candidate is the documented allow-list key.
            continue
        module = _import_target(spec)
        name = getattr(module, _NAME_ATTR, candidate)
        factory = _module_factory(name, module, spec)
        _accept(name, factory, f"module {spec!r}")

    # -- 3. directories ---------------------------------------------------
    for d in _dir_files(trusted_dirs):
        _accept_dir_file(d, enabled_set, seen_names, _accept)
    for wdir in workspace_dirs:
        wpath = Path(wdir)
        if not is_trusted(wpath, trust_store):
            warnings.warn(
                f"skipping untrusted workspace plugin directory {wpath} — "
                f"grant_trust({str(wpath.absolute())!r}) to load it",
                UntrustedPluginDirWarning,
                stacklevel=2,
            )
            continue
        for d in _scan_dir(wpath):
            _accept_dir_file(d, enabled_set, seen_names, _accept)

    return loaded


def _accept_dir_file(
    path: Path,
    enabled_set: Optional[set],
    seen_names: dict,
    accept: Callable[[str, Any, str], None],
) -> None:
    candidate = path.stem
    if enabled_set is not None and candidate not in enabled_set:
        return
    module = _load_module_from_file(candidate, path)
    name = getattr(module, _NAME_ATTR, candidate)
    factory = _module_factory(name, module, str(path))
    accept(name, factory, f"directory file {str(path)!r}")


def _entry_point_iter(
    entry_points: "bool | Iterable[Any]", group: str
) -> Iterable[Any]:
    if entry_points is False:
        return ()
    if entry_points is True:
        eps = importlib.metadata.entry_points()
        # Python 3.10+ ``select``; the group kwarg on 3.12 is equivalent.
        try:
            return list(eps.select(group=group))
        except AttributeError:  # pragma: no cover — legacy mapping API
            return list(eps.get(group, []))
    return list(entry_points)


def _load_entry_point(name: str, ep: Any) -> Any:
    try:
        return ep.load()
    except Exception as exc:  # noqa: BLE001 — surface any load fault loudly
        raise PluginError(
            f"plugin {name!r}: entry point failed to load: {exc}"
        ) from exc


def _candidate_module_name(spec: str) -> str:
    if _looks_like_path(spec):
        return Path(spec).stem
    return spec.rsplit(".", 1)[-1]


def _looks_like_path(spec: str) -> bool:
    return spec.endswith(".py") or os.sep in spec or (os.altsep or "") in spec


def _import_target(spec: str) -> ModuleType:
    if _looks_like_path(spec):
        return _load_module_from_file(Path(spec).stem, Path(spec))
    try:
        return importlib.import_module(spec)
    except Exception as exc:  # noqa: BLE001
        raise PluginError(
            f"plugin module {spec!r} failed to import: {exc}"
        ) from exc


def _load_module_from_file(name: str, path: Path) -> ModuleType:
    modspec = importlib.util.spec_from_file_location(
        f"_noeta_plugin_{name}", path
    )
    if modspec is None or modspec.loader is None:
        raise PluginError(f"plugin file {path} could not be loaded")
    module = importlib.util.module_from_spec(modspec)
    try:
        modspec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        raise PluginError(
            f"plugin file {path} failed to import: {exc}"
        ) from exc
    return module


def _module_factory(name: str, module: ModuleType, origin: str) -> Any:
    factory = getattr(module, _FACTORY_ATTR, None)
    if factory is None:
        raise PluginError(
            f"plugin {name!r} ({origin}) does not export {_FACTORY_ATTR}()"
        )
    return factory


def _dir_files(dirs: Sequence[Any]) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        out.extend(_scan_dir(Path(d)))
    return out


def _scan_dir(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p
        for p in directory.glob("*.py")
        if p.is_file() and not p.name.startswith("_")
    )


def _factory_takes_config(factory: Any) -> bool:
    """Whether the factory declares a second positional parameter (for config)."""
    try:
        sig = inspect.signature(factory)
    except (TypeError, ValueError):  # pragma: no cover — builtins etc.
        return False
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    if len(positional) >= 2:
        return True
    return any(
        p.kind is inspect.Parameter.VAR_POSITIONAL
        for p in sig.parameters.values()
    )


def _invoke_factory(
    name: str, factory: Any, config: Optional[dict]
) -> PluginContributions:
    if not callable(factory):
        raise PluginError(
            f"plugin {name!r}: {_FACTORY_ATTR} is not callable "
            f"(got {type(factory).__name__})"
        )
    api = PluginAPI(name)
    try:
        if _factory_takes_config(factory):
            factory(api, {} if config is None else dict(config))
        else:
            factory(api)
    except PluginError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PluginError(
            f"plugin {name!r}: {_FACTORY_ATTR}() raised: {exc}"
        ) from exc
    return api._contributions()


# ---------------------------------------------------------------------------
# Deterministic merge into Options
# ---------------------------------------------------------------------------


def _base_tool_names(options: Options) -> tuple[str, ...]:
    if options.allowed_tools is None:
        return tuple(sorted(BUILTIN_TOOL_CLASSES))
    return tuple(_compile_tool(e).name for e in options.allowed_tools)


def _register(
    registry: dict[str, str], kind: str, name: str, source: str
) -> None:
    """Record ``name`` under ``source``, raising if it is already claimed."""
    prior = registry.get(name)
    if prior is not None:
        raise PluginError(
            f"{kind} {name!r} is contributed by both {prior} and {source} — "
            f"plugin contributions must not collide (no override in v1)"
        )
    registry[name] = source


def merge_plugins(
    options: Options, plugins: Sequence[LoadedPlugin]
) -> Options:
    """Fold ``plugins`` into ``options`` deterministically, returning a new ``Options``.

    Contributions are ordered by ``(plugin name, contribution name)`` before
    they are merged, so the compiled ``AgentSpec`` is invariant under plugin
    load order. Any name collision — a tool, agent, content kind, or MCP alias
    claimed by two plugins or already present on the base ``options``, or a
    second provider — raises :class:`PluginError` naming **both** sources. There
    is no override mechanism (ADR D4).

    Only the *identity-plane* contributions (tools, agents, content kinds,
    provider, guards, observers) land on the returned ``Options``. Host-plane
    contributions (MCP server specs, skill directories) are still validated and
    collision-checked here but are read separately via :func:`merged_mcp_servers`
    / :func:`merged_skill_dirs` — they have no ``Options`` surface.
    """
    ordered = sorted(plugins, key=lambda p: p.name)

    tool_reg: dict[str, str] = {}
    agent_reg: dict[str, str] = {}
    kind_reg: dict[str, str] = {}
    alias_reg: dict[str, str] = {}
    provider_source: Optional[str] = None
    provider_value: Optional[Any] = None

    # Seed collision registries from the base Options.
    for tname in _base_tool_names(options):
        _register(tool_reg, "tool", tname, _BASE_SOURCE)
    for aname in options.agents:
        _register(agent_reg, "agent", aname, _BASE_SOURCE)
    for ck in options.content_channels:
        _register(kind_reg, "content kind", ck.kind, _BASE_SOURCE)
    if options.provider is not None:
        provider_source = _BASE_SOURCE
        provider_value = options.provider

    tool_entries: list[tuple[str, str, Any]] = []  # (plugin, name, entry)
    kind_specs: list[tuple[str, str, ContentKindSpec]] = []
    guards: list[Any] = []
    observers: list[Any] = []
    agents_add: dict[str, AgentDefinition] = {}

    for plugin in ordered:
        c = plugin.contributions
        src = f"plugin {plugin.name!r}"
        for tname, entry in c.tools:
            _register(tool_reg, "tool", tname, src)
            tool_entries.append((plugin.name, tname, entry))
        for aname, defn in c.agents:
            _register(agent_reg, "agent", aname, src)
            agents_add[aname] = defn
        for ck in c.content_kinds:
            _register(kind_reg, "content kind", ck.kind, src)
            kind_specs.append((plugin.name, ck.kind, ck))
        for alias, _spec in c.mcp_servers:
            _register(alias_reg, "mcp alias", alias, src)
        if c.provider is not None:
            if provider_source is not None:
                raise PluginError(
                    f"provider is set by both {provider_source} and {src} — "
                    f"the provider is single-valued (no override in v1)"
                )
            provider_source = src
            provider_value = c.provider
        guards.extend(c.guards)
        observers.extend(c.observers)

    # Deterministic ordering of the identity-bearing tuples.
    tool_entries.sort(key=lambda t: (t[0], t[1]))
    kind_specs.sort(key=lambda t: (t[0], t[1]))

    # Tools: keep the base entries first (in their given order), then the
    # sorted plugin entries. A ``None`` base with no plugin tools stays ``None``
    # (byte-identical default); a ``None`` base with plugin tools expands to the
    # full built-in set so plugins *add* rather than silently replace.
    new_allowed = options.allowed_tools
    if tool_entries:
        if options.allowed_tools is None:
            base_entries: tuple[Any, ...] = tuple(sorted(BUILTIN_TOOL_CLASSES))
        else:
            base_entries = tuple(options.allowed_tools)
        new_allowed = base_entries + tuple(e for _p, _n, e in tool_entries)

    new_agents = {**options.agents, **agents_add}
    new_channels = tuple(options.content_channels) + tuple(
        ck for _p, _k, ck in kind_specs
    )
    new_guards = tuple(options.guards) + tuple(guards)
    new_observers = tuple(options.observers) + tuple(observers)

    return replace(
        options,
        allowed_tools=new_allowed,
        agents=new_agents,
        content_channels=new_channels,
        guards=new_guards,
        observers=new_observers,
        provider=provider_value,
    )


def merged_mcp_servers(
    plugins: Sequence[LoadedPlugin],
) -> dict[str, Any]:
    """Collect the host-plane MCP server specs from ``plugins`` as ``alias → spec``.

    Raises :class:`PluginError` on an alias collision across plugins (the same
    check :func:`merge_plugins` performs). A host wires the result into its
    ``HostConfig.mcp_server_resolver``.
    """
    out: dict[str, Any] = {}
    source: dict[str, str] = {}
    for plugin in sorted(plugins, key=lambda p: p.name):
        for alias, spec in plugin.contributions.mcp_servers:
            if alias in out:
                raise PluginError(
                    f"mcp alias {alias!r} is contributed by both "
                    f"{source[alias]} and plugin {plugin.name!r}"
                )
            out[alias] = spec
            source[alias] = f"plugin {plugin.name!r}"
    return out


def merged_skill_dirs(plugins: Sequence[LoadedPlugin]) -> tuple[Path, ...]:
    """Collect the host-plane skill directories from ``plugins``, de-duplicated.

    Ordered by ``(plugin name, path)``; a directory contributed by more than
    one plugin appears once. A host wires the result into its ``HostConfig``
    skills directories.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for plugin in sorted(plugins, key=lambda p: p.name):
        for d in sorted(plugin.contributions.skill_dirs, key=str):
            if d not in seen:
                seen.add(d)
                out.append(d)
    return tuple(out)
