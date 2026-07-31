"""Plugin manifest schema + zero-execution reader (spec D1).

A **Plugin** is a package (or a single ``.py`` file for local/dev use) carrying
a **static manifest**. Two forms:

* **Distributed** — ``[tool.noeta]`` in ``pyproject.toml``, mirrored into the
  wheel as package data ``noeta-plugin.toml`` located via the distribution
  metadata. :func:`read_distribution_manifest` reads it **without importing any
  plugin code** — the D1 guarantee, exercised in both regular and editable
  installs (spec Risk 2): the RECORD-listed file is read directly, and the
  editable fallback resolves the package's on-disk location with
  :func:`importlib.util.find_spec`, which locates a top-level package **without
  executing its body**.

* **Single-file** (local dirs only) — a :class:`PluginBuilder` with decorator
  sugar (``@plugin.tool``, ``@plugin.reminder(...)``) *is* the manifest. Reading
  it executes the file; acceptable because local files pass an explicit trust
  gate anyway (D1). A publish-time packaging check (``plugin_check``, M5) derives
  and verifies the TOML from the decorators.

The manifest is intentionally *inert data*: a name, a compatibility range, an
optional config schema, and a list of contributions, each a ``surface`` + a
``ref`` (import string) or ``path`` (resource) + surface-specific params. The
``ref`` is a **string**; nothing here imports it. Resolution — importing the ref
and validating the value per its :class:`~noeta.client.surfaces.SurfaceSpec` — is
a separate, later step (:mod:`noeta.client.plugin_set`), so listing and
manifest-level collision detection run with zero plugin execution.
"""

from __future__ import annotations

import ast
import importlib.util
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from noeta.client.plugins import PluginError


__all__ = [
    "ManifestContribution",
    "PluginManifest",
    "PluginBuilder",
    "MANIFEST_BASENAME",
    "LITERAL_PARAM",
    "parse_manifest_text",
    "read_manifest_file",
    "read_distribution_manifest",
    "declared_plugin_name",
    "exec_plugin_file",
    "find_builder",
    "ref_attr_name",
    "split_ref",
]


#: The package-data filename the wheel mirror carries the manifest under (D1).
MANIFEST_BASENAME = "noeta-plugin.toml"

#: The param a **literal-valued** contribution carries its value under.
#: ``prompt_fragment`` is the only such surface today: its value is a string the
#: author wrote inline, not something a ``ref`` can import. Carrying it as an
#: ordinary param is what lets the static TOML form round-trip — without it the
#: emitted manifest would have neither a ``ref`` nor a ``path`` and would fail to
#: resolve once installed as a package.
LITERAL_PARAM = "text"


@dataclass(frozen=True)
class ManifestContribution:
    """One entry in a manifest's contribution list — pure static description.

    ``name`` is the collision / ordering key **and** the listing label. It is
    declared in the manifest (or derived from ``ref`` / ``path`` when omitted),
    so both listing and collision detection are decidable *without* importing
    the ``ref``. ``params`` holds surface-specific fields (``priority``,
    ``seams``, ``alias``, …) verbatim.
    """

    surface: str
    name: str
    ref: Optional[str] = None
    path: Optional[str] = None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginManifest:
    """A plugin's whole static manifest — its identity and its contributions."""

    name: str
    requires_noeta: Optional[str] = None
    config_schema: Optional[Mapping[str, Any]] = None
    contributions: tuple[ManifestContribution, ...] = ()


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------


def parse_manifest_text(text: str, *, origin: str = "<manifest>") -> PluginManifest:
    """Parse a manifest from TOML text.

    Accepts three shapes, in priority order: ``[tool.noeta]`` (a ``pyproject.toml``
    that also carries the plugin), ``[noeta]``, and bare top-level keys (the
    mirrored ``noeta-plugin.toml``). ``origin`` names the source in error
    messages.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PluginError(f"{origin}: not valid TOML: {exc}") from exc

    table: Mapping[str, Any] = data
    tool = data.get("tool")
    if isinstance(tool, Mapping) and isinstance(tool.get("noeta"), Mapping):
        table = tool["noeta"]
    elif isinstance(data.get("noeta"), Mapping):
        table = data["noeta"]

    return _manifest_from_table(table, origin)


def read_manifest_file(path: Any, *, origin: Optional[str] = None) -> PluginManifest:
    """Read + parse a manifest file (``noeta-plugin.toml`` or a ``pyproject.toml``)."""
    p = Path(path)
    origin = origin or f"manifest {str(p)!r}"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginError(f"{origin}: cannot read: {exc}") from exc
    return parse_manifest_text(text, origin=origin)


def _manifest_from_table(table: Mapping[str, Any], origin: str) -> PluginManifest:
    name = table.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PluginError(f"{origin}: manifest is missing a string 'name'")

    requires = table.get("requires-noeta", table.get("requires_noeta"))
    if requires is not None and not isinstance(requires, str):
        raise PluginError(f"{origin}: 'requires-noeta' must be a string")

    config_schema = table.get("config-schema", table.get("config_schema"))
    if config_schema is not None and not isinstance(config_schema, Mapping):
        raise PluginError(f"{origin}: 'config-schema' must be a table")

    raw_contribs = table.get("contributions", [])
    if not isinstance(raw_contribs, Sequence) or isinstance(raw_contribs, (str, bytes)):
        raise PluginError(f"{origin}: 'contributions' must be an array of tables")

    contributions = tuple(
        _contribution_from(raw, origin, i) for i, raw in enumerate(raw_contribs)
    )
    # ``(surface, name)`` is the collision / ordering key the whole mechanism
    # is built on, so a manifest declaring it twice is malformed — the second
    # entry would shadow the first in every by-key projection. ``PluginBuilder``
    # has always rejected this for the single-file form; the TOML form used to
    # accept it silently, which let a *distributed* plugin ship a duplicate the
    # same plugin's single-file form would have refused (a manifest/loader
    # drift the whole static-manifest guarantee depends on not having).
    seen: dict[tuple[str, str], int] = {}
    for index, c in enumerate(contributions):
        key = (c.surface, c.name)
        first = seen.get(key)
        if first is not None:
            raise PluginError(
                f"{origin}: contribution #{index} duplicates #{first} — "
                f"surface {c.surface!r} already declares {c.name!r}; "
                f"(surface, name) must be unique within a manifest"
            )
        seen[key] = index
    return PluginManifest(
        name=name,
        requires_noeta=requires,
        config_schema=config_schema,
        contributions=contributions,
    )


def _contribution_from(raw: Any, origin: str, index: int) -> ManifestContribution:
    where = f"{origin}: contribution #{index}"
    if not isinstance(raw, Mapping):
        raise PluginError(f"{where} must be a table")

    surface = raw.get("surface")
    if not isinstance(surface, str) or not surface.strip():
        raise PluginError(f"{where} is missing a string 'surface'")

    ref = raw.get("ref")
    path = raw.get("path")
    if ref is not None and not isinstance(ref, str):
        raise PluginError(f"{where}: 'ref' must be a string")
    if path is not None and not isinstance(path, str):
        raise PluginError(f"{where}: 'path' must be a string")

    declared = raw.get("name")
    name = declared if isinstance(declared, str) and declared.strip() else _derive_name(ref, path)
    if name is None:
        raise PluginError(
            f"{where} needs a 'name' (or a 'ref'/'path' to derive one from)"
        )

    params = {
        k: v for k, v in raw.items() if k not in {"surface", "ref", "path", "name"}
    }
    return ManifestContribution(surface=surface, name=name, ref=ref, path=path, params=params)


def split_ref(ref: str) -> tuple[Optional[str], tuple[str, ...]]:
    """Split a manifest ``ref`` into its module half and attribute path.

    The **one** place the two ref spellings are parsed (previously
    reimplemented in three modules, whose docstrings had to promise by hand
    that they stayed in agreement):

    * ``pkg.mod:attr.sub`` — the explicit form. Everything left of ``:`` is
      the module; returns ``("pkg.mod", ("attr", "sub"))``.
    * ``pkg.mod.attr`` — the dotted form. The module boundary is not knowable
      without trying imports, so the module half is ``None`` and every
      component is returned: ``(None, ("pkg", "mod", "attr"))``. The importer
      (:meth:`LoadedPlugin._import_ref`) resolves the boundary by backing off
      one component at a time.

    Empty components are dropped, so a stray ``"pkg.mod:"`` yields no
    attribute path rather than an empty-string component.
    """
    module, sep, attr = ref.partition(":")
    if sep:
        return module, tuple(p for p in attr.split(".") if p)
    return None, tuple(p for p in ref.split(".") if p)


def ref_attr_name(ref: Optional[str]) -> Optional[str]:
    """The final attribute component of a ``ref`` — its natural contribution name.

    ``"pkg.mod:attr.sub"`` and ``"pkg.mod.attr.sub"`` both yield ``"sub"``.
    ``None`` for a missing ref or one with no usable component.
    """
    if not ref:
        return None
    _module, parts = split_ref(ref)
    if not parts:
        return None
    return parts[-1].strip() or None


def _derive_name(ref: Optional[str], path: Optional[str]) -> Optional[str]:
    """Derive a contribution name from its ``ref`` attribute or ``path`` basename."""
    candidate = ref_attr_name(ref)
    if candidate:
        return candidate
    if path:
        stem = Path(path).name.strip()
        if stem:
            return stem
    return None


# ---------------------------------------------------------------------------
# Distribution reader — the zero-execution package form (D1, Risk 2)
# ---------------------------------------------------------------------------


def read_distribution_manifest(dist: Any) -> Optional[PluginManifest]:
    """Read a distribution's ``noeta-plugin.toml`` **without importing its code**.

    Returns ``None`` when the distribution ships no manifest (so a caller can
    attribute the miss). Works for both a regular install (the file is listed in
    the distribution's RECORD and read straight off disk) and an editable
    install (RECORD may omit package data, so the package's on-disk directory is
    resolved via :func:`importlib.util.find_spec`, which locates a top-level
    package without executing it).
    """
    text = _distribution_manifest_text(dist)
    if text is None:
        return None
    return parse_manifest_text(text, origin=f"distribution {_dist_name(dist)!r}")


def _distribution_manifest_text(dist: Any) -> Optional[str]:
    # 1. RECORD-listed package data (the regular-install path).
    try:
        files = dist.files
    except Exception:  # noqa: BLE001 — a malformed dist must not crash discovery
        files = None
    for entry in files or []:
        if getattr(entry, "name", "") == MANIFEST_BASENAME:
            try:
                return Path(dist.locate_file(entry)).read_text(encoding="utf-8")
            except OSError:
                continue

    # 2. Editable fallback: resolve each top-level package's directory and look
    #    for the manifest beside it. ``find_spec`` locates without executing.
    for pkg in _dist_top_level(dist):
        located = _package_data_path(pkg, MANIFEST_BASENAME)
        if located is not None:
            try:
                return located.read_text(encoding="utf-8")
            except OSError:
                continue
    return None


def _dist_name(dist: Any) -> str:
    try:
        meta = dist.metadata
        name = meta["Name"] if meta is not None else None
    except Exception:  # noqa: BLE001
        name = None
    return name or getattr(dist, "name", None) or "<unknown>"


def _dist_top_level(dist: Any) -> list[str]:
    try:
        top = dist.read_text("top_level.txt")
    except Exception:  # noqa: BLE001
        top = None
    names = [line.strip() for line in top.splitlines()] if top else []
    if not names:
        raw = _dist_name(dist)
        if raw and raw != "<unknown>":
            names = [raw.replace("-", "_")]
    return [n for n in names if n]


def _package_data_path(pkg: str, basename: str) -> Optional[Path]:
    """The path to ``pkg``'s ``basename`` package-data file, or ``None``.

    Uses :func:`importlib.util.find_spec`, which locates a top-level package
    without executing its ``__init__`` — the editable-install path that keeps the
    zero-execution guarantee.
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec(pkg)
    except (ImportError, ValueError, AttributeError):
        return None
    if spec is None:
        return None
    for location in spec.submodule_search_locations or []:
        candidate = Path(location) / basename
        if candidate.is_file():
            return candidate
    if spec.origin and spec.origin not in ("namespace", "built-in", "frozen"):
        candidate = Path(spec.origin).parent / basename
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Static name extraction — enabled-gate-before-execution for single-file plugins
# ---------------------------------------------------------------------------


def declared_plugin_name(path: Any) -> Optional[str]:
    """The plugin name a single-file plugin declares, read **without executing it**.

    Parsed statically with :mod:`ast`, so the ``enabled`` allow-list can gate a
    single-file plugin *before* its body runs. Recognises two module-level
    literal forms::

        noeta_plugin_name = "the-name"
        plugin = PluginBuilder("the-name")

    Returns ``None`` when neither literal is present (or the file cannot be
    parsed) — the loader then falls back to executing the trusted file.
    """
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None

    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value: Optional[ast.expr] = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue

        names_here = {t.id for t in targets if isinstance(t, ast.Name)}
        if (
            "noeta_plugin_name" in names_here
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value

        builder_name = _builder_literal_name(value)
        if builder_name is not None:
            return builder_name
    return None


def _builder_literal_name(value: Optional[ast.expr]) -> Optional[str]:
    """The first string-literal argument of a ``PluginBuilder(...)`` call, or ``None``."""
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    callee = (
        func.id if isinstance(func, ast.Name)
        else func.attr if isinstance(func, ast.Attribute)
        else None
    )
    if callee != "PluginBuilder":
        return None
    for arg in value.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    for kw in value.keywords:
        if (
            kw.arg == "name"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return None


# ---------------------------------------------------------------------------
# Single-file decorator sugar (D1)
# ---------------------------------------------------------------------------


class PluginBuilder:
    """Decorator sugar that *is* a single-file plugin's manifest.

    A local ``.py`` plugin declares one module-level builder and decorates its
    contributions::

        plugin = PluginBuilder("deadline-nudge")

        @plugin.reminder(priority=100)
        def deadline(state):
            ...

    :meth:`manifest` returns the equivalent static :class:`PluginManifest`; the
    decorated objects are also kept (:attr:`resolved_objects`) so the loader can
    resolve a single-file plugin's contributions without a second import. Every
    decorator maps to the generic :meth:`contribute`, so the same builder covers
    surfaces without a dedicated method.
    """

    def __init__(
        self,
        name: str,
        *,
        requires_noeta: Optional[str] = None,
        config_schema: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise PluginError("PluginBuilder requires a non-empty name")
        self._name = name
        self._requires_noeta = requires_noeta
        self._config_schema = config_schema
        self._contributions: list[ManifestContribution] = []
        #: ``(surface, name) -> resolved object`` for single-file resolution.
        self.resolved_objects: dict[tuple[str, str], Any] = {}
        self._keys: set[tuple[str, str]] = set()

    @property
    def name(self) -> str:
        return self._name

    def contribute(
        self,
        surface: str,
        value: Any = None,
        *,
        name: Optional[str] = None,
        ref: Optional[str] = None,
        path: Optional[str] = None,
        **params: Any,
    ) -> Any:
        """Record one contribution; return ``value`` so decorator forms compose.

        When ``value`` is a live object and no ``ref`` is given, its import ref
        (``module:qualname``) is derived and the object is cached for resolution.
        """
        if value is not None and ref is None and not isinstance(value, (str, Path)):
            module = getattr(value, "__module__", None)
            qualname = getattr(value, "__qualname__", getattr(value, "__name__", None))
            if module and qualname:
                ref = f"{module}:{qualname}"
        resolved_name = (
            name
            or getattr(value, "name", None)
            or _derive_name(ref, path)
            or (str(value) if isinstance(value, (str, Path)) else None)
        )
        if not isinstance(resolved_name, str) or not resolved_name.strip():
            raise PluginError(
                f"plugin {self._name!r}: contribution to surface {surface!r} "
                f"needs a name (none given and none derivable)"
            )
        key = (surface, resolved_name)
        if key in self._keys:
            raise PluginError(
                f"plugin {self._name!r} contributes {resolved_name!r} to surface "
                f"{surface!r} twice"
            )
        self._keys.add(key)
        self._contributions.append(
            ManifestContribution(
                surface=surface, name=resolved_name, ref=ref, path=path, params=dict(params)
            )
        )
        if value is not None:
            self.resolved_objects[key] = value
        return value

    def _decorator(self, surface: str, **params: Any) -> Callable[[Any], Any]:
        declared = params.pop("name", None)

        def deco(fn: Any) -> Any:
            self.contribute(surface, fn, name=declared, **params)
            return fn

        return deco

    def tool(self, fn: Any = None, *, name: Optional[str] = None) -> Any:
        """Contribute a ``tool`` (decorator or direct call)."""
        if fn is None:
            return self._decorator("tool", name=name)
        return self.contribute("tool", fn, name=name)

    def reminder(self, fn: Any = None, *, name: Optional[str] = None, priority: int = 0) -> Any:
        """Contribute a compose-time ``reminder`` (track B; carries ``priority``)."""
        if fn is None:
            return self._decorator("reminder", name=name, priority=priority)
        return self.contribute("reminder", fn, name=name, priority=priority)

    def reminder_provider(
        self, fn: Any = None, *, name: Optional[str] = None, seams: Sequence[str] = ()
    ) -> Any:
        """Contribute a recorded ``reminder_provider`` (track A; carries ``seams``)."""
        if fn is None:
            return self._decorator("reminder_provider", name=name, seams=tuple(seams))
        return self.contribute("reminder_provider", fn, name=name, seams=tuple(seams))

    def guard(self, obj: Any = None, *, name: Optional[str] = None) -> Any:
        """Contribute a ``guard`` (governance; process-scoped)."""
        if obj is None:
            return self._decorator("guard", name=name)
        return self.contribute("guard", obj, name=name)

    def observer(self, fn: Any = None, *, name: Optional[str] = None) -> Any:
        """Contribute an ``observer`` (governance; process-scoped)."""
        if fn is None:
            return self._decorator("observer", name=name)
        return self.contribute("observer", fn, name=name)

    def prompt_fragment(self, text: str, *, name: str) -> Any:
        """Contribute a ``prompt_fragment`` (a literal string, keyed by ``name``).

        The text is recorded as the :data:`LITERAL_PARAM` param as well as being
        cached for single-file resolution, so ``plugin_check --emit`` produces a
        manifest that still carries the fragment — a distributed install has no
        ``resolved_objects`` cache and no ``ref`` to import for a literal.
        """
        return self.contribute(
            "prompt_fragment", text, name=name, **{LITERAL_PARAM: text}
        )

    def tool_result_transform(
        self, fn: Any = None, *, name: Optional[str] = None, priority: int = 0
    ) -> Any:
        """Contribute a ``tool_result_transform`` (D9; carries ``priority``).

        A pure ``ToolResult -> ToolResult`` stage applied inside the ToolRuntime
        boundary before recording. Ordered like a ``reminder``: integer
        ``priority`` first, ties broken by ``(plugin, name)``.
        """
        if fn is None:
            return self._decorator("tool_result_transform", name=name, priority=priority)
        return self.contribute("tool_result_transform", fn, name=name, priority=priority)

    def policy(self, factory: Any = None, *, name: Optional[str] = None) -> Any:
        """Contribute the agent's decision ``policy`` (D10; single-valued).

        ``factory`` is a ``(llm) -> Policy`` callable carrying a ``.ref``
        property (its identity). A single agent may activate at most one policy;
        a base ``Options.policy`` plus an active plugin policy, or two active
        plugin policies, is a loud collision.
        """
        if factory is None:
            return self._decorator("policy", name=name)
        return self.contribute("policy", factory, name=name)

    def sandbox_provider(self, obj: Any = None, *, name: Optional[str] = None) -> Any:
        """Contribute a ``sandbox_provider`` (D10; host-plane, host selects one)."""
        if obj is None:
            return self._decorator("sandbox_provider", name=name)
        return self.contribute("sandbox_provider", obj, name=name)

    def session_pack(
        self, factory: Any = None, *, name: Optional[str] = None, priority: int = 0
    ) -> Any:
        """Contribute a ``session_pack`` (microkernel phase 3; carries ``priority``).

        ``factory`` is a ``(SessionBuildContext) -> PackContribution`` callable
        the kernel builder's generic loop runs when an agent activates this
        plugin — the session-construction half of a capability: tools, content
        kinds, named exports. Ordered like a ``reminder``: integer ``priority``
        first, ties broken by ``(plugin, name)``. The built-in bands run
        100–1000; an external pack picks its own band relative to them (e.g.
        ``priority=1100`` appends after everything).
        """
        if factory is None:
            return self._decorator("session_pack", name=name, priority=priority)
        return self.contribute("session_pack", factory, name=name, priority=priority)

    def control_tool(
        self, factory: Any = None, *, name: Optional[str] = None, priority: int = 0
    ) -> Any:
        """Contribute a ``control_tool`` (control-tool-surface S2; carries ``priority``).

        ``factory`` is a ``(ControlToolBuildContext) -> ControlToolMount | None``
        callable the kernel builder's dual-priority mount loop runs when an agent
        activates this plugin — it self-gates on the build context and returns a
        materialized ``ControlToolMount`` (a provider-facing schema + a
        response→neutral-Decision translate closure + its two priority bands) or
        ``None``. ``priority`` orders factory execution (ties broken by
        ``(plugin, name)``); the OUTPUT byte orders come from each mount's own
        ``schema_priority`` / ``routing_priority``. The built-in bands run
        100–600; an external control tool picks its own bands relative to them.
        """
        if factory is None:
            return self._decorator("control_tool", name=name, priority=priority)
        return self.contribute("control_tool", factory, name=name, priority=priority)

    def manifest(self) -> PluginManifest:
        """The static :class:`PluginManifest` this builder describes."""
        return PluginManifest(
            name=self._name,
            requires_noeta=self._requires_noeta,
            config_schema=self._config_schema,
            contributions=tuple(self._contributions),
        )


# ---------------------------------------------------------------------------
# Single-file plugin loading — shared by the loader and the verifier
# ---------------------------------------------------------------------------


def exec_plugin_file(path: Path, *, module_prefix: str) -> Any:
    """Execute a single-file plugin and return its module object.

    The one implementation of "run a ``plugin.py`` and hand back the module",
    shared by the loader (:mod:`noeta.client.plugin_set`) and the manifest
    verifier (:mod:`noeta.sdk.plugin_check`). They differ only in the synthetic
    module name they register under, which is what ``module_prefix`` carries —
    the verifier must not collide with a module the loader already executed in
    the same process.

    Any import fault inside the file surfaces as a named :class:`PluginError`
    rather than an opaque traceback: a plugin that cannot be imported is a
    build-time configuration error, not an engine crash.
    """
    modspec = importlib.util.spec_from_file_location(
        f"{module_prefix}{path.stem}", path
    )
    if modspec is None or modspec.loader is None:
        raise PluginError(f"plugin file {path} could not be loaded")
    module = importlib.util.module_from_spec(modspec)
    try:
        modspec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — surface any import fault, named
        raise PluginError(f"plugin file {path} failed to import: {exc}") from exc
    return module


def find_builder(module: Any, origin: Any) -> PluginBuilder:
    """The module-level :class:`PluginBuilder` a single-file plugin exposes.

    Prefers the conventional ``plugin`` name; falls back to a unique
    module-level builder. Zero builders and two-or-more builders are both
    errors naming ``origin`` (a path or a module label) — guessing which of two
    builders the author meant would silently ship half a plugin.

    Shared by the loader and the verifier so the discovery rule cannot drift
    between "what gets loaded" and "what gets verified".
    """
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
