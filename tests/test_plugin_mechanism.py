"""Tests for the M1 manifest-plugin mechanism core (spec D1-D5).

The mechanism is built additively in three new ``noeta.client`` modules:

* :mod:`noeta.client.surfaces` — the ``SurfaceSpec`` registry (D2/D3).
* :mod:`noeta.client.plugin_manifest` — manifest schema + zero-execution reader
  and single-file decorator sugar (D1).
* :mod:`noeta.client.plugin_set` — the five-source loader, deterministic merge,
  and ``PluginSet`` (D4/D5).

Coverage maps to the spec's acceptance criteria and the task's required tests:
registry extension, zero-execution listing (import-sentinel), the enabled gate
applied before import, every collision class, determinism under shuffled
discovery order, and trust gating. The distribution reader is exercised for both
a regular and an editable install layout (spec Risk 2).
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path, PurePosixPath

import pytest

from noeta.client.plugin_manifest import (
    MANIFEST_BASENAME,
    ManifestContribution,
    PluginBuilder,
    PluginManifest,
    declared_plugin_name,
    parse_manifest_text,
    read_distribution_manifest,
    read_manifest_file,
)
from noeta.client.plugin_set import (
    LoadedPlugin,
    MergedContributions,
    PluginSet,
    load_plugins,
)
from noeta.client.plugins import (
    PluginError,
    UntrustedPluginDirWarning,
    grant_trust,
    is_trusted,
)
from noeta.client.surfaces import (
    STANDARD_SURFACES,
    SurfaceRegistry,
    SurfaceSpec,
    standard_registry,
)
from noeta.context.content_channel import ContentKindSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contrib(surface: str, name: str, *, ref: str | None = None, **params) -> ManifestContribution:
    return ManifestContribution(surface=surface, name=name, ref=ref, params=dict(params))


def _manifest(name: str, *contribs: ManifestContribution) -> PluginManifest:
    return PluginManifest(name=name, contributions=tuple(contribs))


def _loaded(manifest: PluginManifest, objects: dict | None = None) -> LoadedPlugin:
    return LoadedPlugin(
        name=manifest.name,
        manifest=manifest,
        origin=f"test {manifest.name!r}",
        source="test",
        resolved_objects=dict(objects or {}),
    )


def _set(*manifests: PluginManifest, registry: SurfaceRegistry | None = None) -> PluginSet:
    return PluginSet(
        plugins=tuple(_loaded(m) for m in manifests),
        registry=registry or standard_registry(),
    )


# ---------------------------------------------------------------------------
# 1. Surface registry (D2/D3) + host extension
# ---------------------------------------------------------------------------


def test_standard_registry_has_all_sixteen_surfaces():
    reg = standard_registry()
    assert len(reg.names()) == 16
    assert len(STANDARD_SURFACES) == 16
    # A spot-check of the D3 table cells that carry mechanism meaning.
    assert reg.get("policy").collision_key == "single-valued"
    assert reg.get("provider").merge_rule == "single"
    assert reg.get("reminder").ordering == "priority"
    assert reg.get("session_pack").ordering == "priority"
    assert reg.get("session_pack").activation_scope == "per-agent"
    # control_tool (control-tool-surface S1): identity plane, per-agent,
    # collision on name, priority-ordered (the dual-priority mount loop).
    assert reg.get("control_tool").plane == "identity"
    assert reg.get("control_tool").activation_scope == "per-agent"
    assert reg.get("control_tool").collision_key == "name"
    assert reg.get("control_tool").ordering == "priority"
    assert reg.get("guard").activation_scope == "process"
    assert reg.get("guard").collision_key == "none"
    assert reg.get("tool").plane == "identity"
    assert reg.get("mcp_server").collision_key == "alias"


def test_registry_is_fresh_each_call():
    assert standard_registry() is not standard_registry()


def test_registry_rejects_duplicate_surface_and_bad_type():
    reg = standard_registry()
    with pytest.raises(PluginError):
        reg.register(SurfaceSpec("tool", "identity", "per-agent", lambda v: None, "name", "append"))
    with pytest.raises(PluginError):
        reg.register(object())  # type: ignore[arg-type]


def test_registry_get_unknown_surface_names_it():
    with pytest.raises(PluginError) as exc:
        standard_registry().get("no_such_surface")
    assert "no_such_surface" in str(exc.value)


def test_host_extends_registry_before_load():
    """A host registers an app-plane surface on a copy; the loader honours it."""
    reg = standard_registry().copy()
    reg.register(
        SurfaceSpec("app_router", "host", "host-wired", lambda v: None, "name", "append")
    )
    assert "app_router" in reg
    assert "app_router" not in standard_registry()  # the copy is independent

    ps = _set(_manifest("routes", _contrib("app_router", "orders")), registry=reg)
    merged = ps.merged()
    assert [e.contribution.name for e in merged.for_surface("app_router")] == ["orders"]


def test_merge_over_unknown_surface_fails_naming_plugin():
    ps = _set(_manifest("p", _contrib("not_a_surface", "x")))
    with pytest.raises(PluginError) as exc:
        ps.merged()
    assert "not_a_surface" in str(exc.value)


# ---------------------------------------------------------------------------
# 2. Manifest schema + parsing (D1)
# ---------------------------------------------------------------------------


def test_parse_pyproject_tool_noeta_form():
    m = parse_manifest_text(
        textwrap.dedent(
            """
            [tool.noeta]
            name = "acme"
            requires-noeta = ">=0.4,<0.5"

            [[tool.noeta.contributions]]
            surface = "tool"
            name = "acme_lint"
            ref = "acme.tools:lint"

            [[tool.noeta.contributions]]
            surface = "reminder"
            name = "deadline"
            ref = "acme.rem:deadline"
            priority = 100
            """
        )
    )
    assert m.name == "acme"
    assert m.requires_noeta == ">=0.4,<0.5"
    assert [c.surface for c in m.contributions] == ["tool", "reminder"]
    assert m.contributions[1].params["priority"] == 100


def test_parse_bare_and_noeta_table_forms():
    bare = parse_manifest_text('name = "b"\n')
    assert bare.name == "b" and bare.contributions == ()
    nested = parse_manifest_text('[noeta]\nname = "n"\n')
    assert nested.name == "n"


def test_parse_derives_contribution_name_from_ref_when_omitted():
    m = parse_manifest_text(
        textwrap.dedent(
            """
            name = "d"
            [[contributions]]
            surface = "tool"
            ref = "pkg.mod:my_tool"
            """
        )
    )
    assert m.contributions[0].name == "my_tool"


def test_parse_rejects_missing_name_and_bad_shapes():
    with pytest.raises(PluginError):
        parse_manifest_text("requires-noeta = '>=0.4'\n")
    with pytest.raises(PluginError):
        parse_manifest_text('name = "x"\ncontributions = 3\n')
    with pytest.raises(PluginError):
        parse_manifest_text('name = "x"\n[[contributions]]\nref = "m:a"\n')  # no surface
    with pytest.raises(PluginError):
        # a contribution with neither a name nor a ref/path to derive one
        parse_manifest_text('name = "x"\n[[contributions]]\nsurface = "guard"\n')


def test_read_manifest_file_and_missing_file(tmp_path):
    p = tmp_path / "noeta-plugin.toml"
    p.write_text('name = "fromfile"\n', encoding="utf-8")
    assert read_manifest_file(p).name == "fromfile"
    with pytest.raises(PluginError):
        read_manifest_file(tmp_path / "nope.toml")


# ---------------------------------------------------------------------------
# 3. Single-file decorator sugar (D1)
# ---------------------------------------------------------------------------


def test_builder_decorators_produce_manifest_and_objects():
    plugin = PluginBuilder("sugar", requires_noeta=">=0.4")

    @plugin.reminder(priority=7)
    def nudge(state):  # pragma: no cover — identity only
        return None

    @plugin.tool(name="do_thing")
    def do_thing(arguments, ctx):  # pragma: no cover
        return None

    plugin.prompt_fragment("Be brief.", name="brief")

    m = plugin.manifest()
    assert m.name == "sugar" and m.requires_noeta == ">=0.4"
    surfaces = {c.surface: c for c in m.contributions}
    assert surfaces["reminder"].params["priority"] == 7
    assert surfaces["tool"].name == "do_thing"
    assert surfaces["prompt_fragment"].name == "brief"
    # The live objects are cached for single-file resolution.
    assert plugin.resolved_objects[("reminder", "nudge")] is nudge


def test_builder_rejects_within_plugin_duplicate():
    plugin = PluginBuilder("dup")
    plugin.contribute("tool", name="t", ref="m:a")
    with pytest.raises(PluginError):
        plugin.contribute("tool", name="t", ref="m:b")


def test_builder_requires_name_and_nonempty_builder_name():
    with pytest.raises(PluginError):
        PluginBuilder("")
    plugin = PluginBuilder("x")
    with pytest.raises(PluginError):
        plugin.contribute("guard", object())  # no name derivable


def test_declared_plugin_name_reads_both_literal_forms(tmp_path):
    a = tmp_path / "a.py"
    a.write_text('noeta_plugin_name = "aaa"\n', encoding="utf-8")
    assert declared_plugin_name(a) == "aaa"

    b = tmp_path / "b.py"
    b.write_text('from x import PluginBuilder\nplugin = PluginBuilder("bbb")\n', encoding="utf-8")
    assert declared_plugin_name(b) == "bbb"

    c = tmp_path / "c.py"
    c.write_text("x = 1\n", encoding="utf-8")
    assert declared_plugin_name(c) is None


# ---------------------------------------------------------------------------
# 4. Collisions — every class, naming both sides, no override (D4)
# ---------------------------------------------------------------------------


def test_tool_name_collision_names_both_plugins():
    ps = _set(
        _manifest("plugin_a", _contrib("tool", "grep_pro", ref="a:t")),
        _manifest("plugin_b", _contrib("tool", "grep_pro", ref="b:t")),
    )
    with pytest.raises(PluginError) as exc:
        ps.merged()
    m = str(exc.value)
    assert "plugin_a" in m and "plugin_b" in m and "grep_pro" in m


def test_content_kind_collision_uses_kind_label():
    ps = _set(
        _manifest("plugin_a", _contrib("content_kind", "notes", ref="a:k")),
        _manifest("plugin_b", _contrib("content_kind", "notes", ref="b:k")),
    )
    with pytest.raises(PluginError) as exc:
        ps.merged()
    m = str(exc.value)
    assert "content kind" in m and "notes" in m and "plugin_a" in m and "plugin_b" in m


def test_mcp_alias_collision_uses_alias_label():
    ps = _set(
        _manifest("plugin_a", _contrib("mcp_server", "srv", ref="a:s")),
        _manifest("plugin_b", _contrib("mcp_server", "srv", ref="b:s")),
    )
    with pytest.raises(PluginError) as exc:
        ps.merged()
    assert "mcp alias" in str(exc.value) and "srv" in str(exc.value)


def test_policy_is_single_valued_across_plugins():
    ps = _set(
        _manifest("plugin_a", _contrib("policy", "pol_a", ref="a:p")),
        _manifest("plugin_b", _contrib("policy", "pol_b", ref="b:p")),
    )
    with pytest.raises(PluginError) as exc:
        ps.merged()
    m = str(exc.value)
    assert "single-valued" in m and "plugin_a" in m and "plugin_b" in m


def test_provider_is_single_valued_across_plugins():
    ps = _set(
        _manifest("plugin_a", _contrib("provider", "prov_a", ref="a:p")),
        _manifest("plugin_b", _contrib("provider", "prov_b", ref="b:p")),
    )
    with pytest.raises(PluginError):
        ps.merged()


def test_guard_and_observer_never_collide():
    """collision_key == none: two guards of the same name coexist (governance)."""
    ps = _set(
        _manifest("plugin_a", _contrib("guard", "audit", ref="a:g")),
        _manifest("plugin_b", _contrib("guard", "audit", ref="b:g")),
    )
    merged = ps.merged()
    assert len(merged.for_surface("guard")) == 2


def test_cross_source_duplicate_plugin_name_raises():
    m1 = _manifest("dup", _contrib("tool", "x", ref="a:x"))
    m2 = _manifest("dup", _contrib("tool", "y", ref="b:y"))
    with pytest.raises(PluginError) as exc:
        load_plugins(builtins=[m1], entry_points=[_FakeEP("dup", _FakeDist.for_manifest(m2))])
    assert "dup" in str(exc.value)


# ---------------------------------------------------------------------------
# 5. Determinism under shuffled discovery order (D4)
# ---------------------------------------------------------------------------


def test_merge_is_invariant_under_discovery_order():
    manifests = [
        _manifest("zeta", _contrib("tool", "z_tool", ref="z:t"), _contrib("reminder", "z_rem", ref="z:r", priority=5)),
        _manifest("alpha", _contrib("tool", "a_tool", ref="a:t"), _contrib("reminder", "a_rem", ref="a:r", priority=5)),
        _manifest("mid", _contrib("reminder", "m_rem", ref="m:r", priority=1)),
    ]
    forward = _set(*manifests).merged()
    reverse = _set(*reversed(manifests)).merged()
    assert forward == reverse
    assert isinstance(forward, MergedContributions)
    # priority first, then (plugin, name): m_rem(1) < a_rem(5,alpha) < z_rem(5,zeta)
    assert [e.contribution.name for e in forward.for_surface("reminder")] == [
        "m_rem",
        "a_rem",
        "z_rem",
    ]
    # sorted surface: (plugin, name)
    assert [e.plugin for e in forward.for_surface("tool")] == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# 6. Trust gating of workspace directories (D4 source 4)
# ---------------------------------------------------------------------------


_SIMPLE_DECORATOR_PLUGIN = """
    from noeta.client.plugin_manifest import PluginBuilder

    plugin = PluginBuilder("ws-plugin")

    plugin.prompt_fragment("hello", name="frag")
"""


def _write_py(directory: Path, stem: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.py").write_text(textwrap.dedent(body), encoding="utf-8")


def test_untrusted_workspace_dir_skipped_with_warning(tmp_path):
    ws = tmp_path / "ws"
    _write_py(ws, "p", _SIMPLE_DECORATOR_PLUGIN)
    store = tmp_path / "trust.json"
    with pytest.warns(UntrustedPluginDirWarning):
        ps = load_plugins(builtins=False, workspace_dirs=[ws], trust_store=store)
    assert ps.names() == ()


def test_trusted_workspace_dir_loads(tmp_path):
    ws = tmp_path / "ws"
    _write_py(ws, "p", _SIMPLE_DECORATOR_PLUGIN)
    store = tmp_path / "trust.json"
    grant_trust(ws, store)
    ps = load_plugins(builtins=False, workspace_dirs=[ws], trust_store=store)
    assert ps.names() == ("ws-plugin",)


def test_user_dir_loads_unconditionally(tmp_path):
    d = tmp_path / "userplugins"
    _write_py(d, "p", _SIMPLE_DECORATOR_PLUGIN)
    ps = load_plugins(builtins=False, user_dirs=[d])
    assert ps.names() == ("ws-plugin",)


def test_grant_trust_is_idempotent(tmp_path):
    """Granting the same directory twice records it once (ported from M1)."""
    import json

    store = tmp_path / "trust.json"
    d = tmp_path / "x"
    grant_trust(d, store)
    grant_trust(d, store)
    data = json.loads(store.read_text())
    assert data["trusted"].count(str(Path(d).resolve())) == 1


def test_trust_survives_a_different_spelling_of_the_same_dir(tmp_path):
    """A grant and a lookup written differently still name one directory.

    Both sides canonicalise (``~`` expansion + absolute + resolve), so a
    workspace path carrying a ``..`` segment — the shape a host composes when it
    joins a workspace root with ``.noeta/plugins`` — is not a second, untrusted
    directory (ported from M1).
    """
    store = tmp_path / "trust.json"
    ws = tmp_path / "ws" / "plugins"
    ws.mkdir(parents=True)
    detour = tmp_path / "ws" / ".." / "ws" / "plugins"

    assert is_trusted(ws, store) is False
    grant_trust(detour, store)
    assert is_trusted(ws, store) is True

    # ...and the reverse direction: granted canonically, asked for by detour.
    other_store = tmp_path / "trust2.json"
    grant_trust(ws, other_store)
    assert is_trusted(detour, other_store) is True


# ---------------------------------------------------------------------------
# 7. The enabled allow-list gates BEFORE import (D4, acceptance 3)
# ---------------------------------------------------------------------------


#: A single-file plugin that writes a sentinel at module top-level, so a test can
#: prove the file was never executed when the allow-list excludes it.
_SENTINEL_PLUGIN = """
    import pathlib
    from noeta.client.plugin_manifest import PluginBuilder

    pathlib.Path(__file__).with_name("EXECUTED").write_text("yes")

    plugin = PluginBuilder("named-plugin")
    plugin.prompt_fragment("x", name="frag")
"""


def test_enabled_skips_single_file_plugin_before_executing_it(tmp_path):
    d = tmp_path / "plugins"
    _write_py(d, "plugin", _SENTINEL_PLUGIN)

    ps = load_plugins(user_dirs=[d], enabled=["something-else"])
    assert ps.names() == ()
    assert not (d / "EXECUTED").exists(), "excluded plugin was executed"

    ps = load_plugins(user_dirs=[d], enabled=["named-plugin"])
    assert ps.names() == ("named-plugin",)
    assert (d / "EXECUTED").exists()


def test_enabled_filters_entry_points_by_manifest_name():
    keep = _manifest("keep", _contrib("tool", "k", ref="k:k"))
    drop = _manifest("drop", _contrib("tool", "d", ref="d:d"))
    ps = load_plugins(
        entry_points=[
            _FakeEP("keep", _FakeDist.for_manifest(keep)),
            _FakeEP("drop", _FakeDist.for_manifest(drop)),
        ],
        enabled=["keep"],
    )
    assert ps.names() == ("keep",)


def test_builtins_default_on_and_disable_individually():
    a = _manifest("fs", _contrib("tool", "read", ref="fs:read"))
    b = _manifest("web", _contrib("tool", "fetch", ref="web:fetch"))
    assert set(load_plugins(builtins=[a, b]).names()) == {"fs", "web"}
    assert load_plugins(builtins=[a, b], disabled_builtins=["web"]).names() == ("fs",)
    assert load_plugins(builtins=False).names() == ()
    # Real discovery reads the noeta.builtins catalogue (M2; control-tool-surface
    # S2 adds the three control-tool built-ins → 17; the storage-backend
    # relocation adds the declaration-only `storage` built-in → 18).
    assert set(load_plugins(builtins=True).names()) == {
        "app", "ask_user_question", "browser", "delegation", "fs", "governance",
        "mcp", "memory", "presets", "providers", "react", "reminders", "sandbox",
        "skills", "storage", "todo_write", "web", "workspace",
    }


# ---------------------------------------------------------------------------
# 8. Zero-execution listing + the distribution reader (D1/D5, acceptance 2,
#    Risk 2: both regular and editable install layouts)
# ---------------------------------------------------------------------------


class _FakeEP:
    """A minimal ``importlib.metadata.EntryPoint`` stand-in exposing ``.dist``."""

    def __init__(self, name: str, dist: "_FakeDist") -> None:
        self.name = name
        self.dist = dist


class _FakeDist:
    """A fake distribution over an on-disk package directory.

    ``files``/``locate_file`` model a **regular** install (the manifest is listed
    in RECORD and read straight off disk). ``files=None`` + ``top_level`` models
    an **editable** install (RECORD omits package data; the reader resolves the
    package directory via ``find_spec``).
    """

    def __init__(self, base: Path, *, list_files: bool, top_level: str, name: str):
        self._base = Path(base)
        self._list_files = list_files
        self._top = top_level
        self._name = name

    @classmethod
    def for_manifest(cls, manifest: PluginManifest) -> "_FakeDist":
        """A throwaway on-disk regular-install dist carrying ``manifest`` as TOML."""
        import tempfile

        base = Path(tempfile.mkdtemp())
        pkg = base / manifest.name.replace("-", "_")
        pkg.mkdir()
        lines = [f'name = "{manifest.name}"']
        for c in manifest.contributions:
            lines += ["", "[[contributions]]", f'surface = "{c.surface}"', f'name = "{c.name}"']
            if c.ref:
                lines.append(f'ref = "{c.ref}"')
            for k, v in c.params.items():
                lines.append(f"{k} = {v!r}")
        (pkg / MANIFEST_BASENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return cls(base, list_files=True, top_level=pkg.name, name=manifest.name)

    @property
    def files(self):
        if not self._list_files:
            return None
        out = []
        for p in self._base.rglob(MANIFEST_BASENAME):
            out.append(PurePosixPath(p.relative_to(self._base).as_posix()))
        return out

    def locate_file(self, rel) -> Path:
        return self._base / str(rel)

    def read_text(self, name):
        if name == "top_level.txt":
            return self._top + "\n"
        return None

    @property
    def metadata(self):
        return {"Name": self._name}


def _make_sentinel_package(root: Path, pkg: str) -> Path:
    pkg_dir = root / pkg
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        "import pathlib\n"
        "pathlib.Path(__file__).with_name('EXECUTED').write_text('yes')\n"
        "FRAGMENT = 'resolved-fragment'\n",
        encoding="utf-8",
    )
    (pkg_dir / MANIFEST_BASENAME).write_text(
        textwrap.dedent(
            f"""
            name = "sentinel-plugin"
            [[contributions]]
            surface = "prompt_fragment"
            name = "frag"
            ref = "{pkg}:FRAGMENT"
            """
        ),
        encoding="utf-8",
    )
    return pkg_dir


def test_distribution_manifest_regular_install(tmp_path):
    pkg_dir = _make_sentinel_package(tmp_path / "site", "regpkg")
    dist = _FakeDist(tmp_path / "site", list_files=True, top_level="regpkg", name="regpkg")

    manifest = read_distribution_manifest(dist)
    assert manifest is not None and manifest.name == "sentinel-plugin"
    assert not (pkg_dir / "EXECUTED").exists(), "reading the manifest imported the package"


def test_distribution_manifest_editable_install(tmp_path, monkeypatch):
    src = tmp_path / "src"
    pkg_dir = _make_sentinel_package(src, "editpkg")
    # Editable installs leave the package importable via sys.path; RECORD lists
    # no package data, so the reader must fall back to find_spec.
    monkeypatch.syspath_prepend(str(src))
    sys.modules.pop("editpkg", None)
    importlib.invalidate_caches()

    dist = _FakeDist(src, list_files=False, top_level="editpkg", name="editpkg")
    manifest = read_distribution_manifest(dist)
    assert manifest is not None and manifest.name == "sentinel-plugin"
    assert not (pkg_dir / "EXECUTED").exists(), "editable manifest read imported the package"


def test_distribution_without_manifest_returns_none(tmp_path):
    empty = _FakeDist(tmp_path, list_files=True, top_level="nothing", name="nothing")
    assert read_distribution_manifest(empty) is None


def test_pluginset_lists_contributions_without_executing_then_resolve_imports(
    tmp_path, monkeypatch
):
    """Acceptance 2: list every contribution with zero execution; resolve imports."""
    src = tmp_path / "src"
    pkg_dir = _make_sentinel_package(src, "listpkg")
    monkeypatch.syspath_prepend(str(src))
    sys.modules.pop("listpkg", None)
    importlib.invalidate_caches()

    dist = _FakeDist(src, list_files=True, top_level="listpkg", name="listpkg")
    ps = load_plugins(builtins=False, entry_points=[_FakeEP("listpkg", dist)])

    # Listing reads only the static manifest — the package must NOT be imported.
    listed = ps.contributions()
    assert [(pn, c.surface, c.name) for pn, c in listed] == [
        ("sentinel-plugin", "prompt_fragment", "frag")
    ]
    assert not (pkg_dir / "EXECUTED").exists(), "listing executed plugin code"

    # merged() is likewise execution-free.
    assert ps.merged().for_surface("prompt_fragment")[0].contribution.name == "frag"
    assert not (pkg_dir / "EXECUTED").exists()

    # resolve() is the boundary that imports — now the package runs.
    resolved = ps.resolve()
    assert resolved[0].value == "resolved-fragment"
    assert (pkg_dir / "EXECUTED").exists()


def test_storage_builtin_is_declaration_only_and_lists_without_impl_import():
    """The ``storage`` built-in is the second declaration-only member after
    ``providers`` (storage-backend-relocation G3): its manifest carries zero
    contributions — the durable backends are host-wired via
    ``noeta.sdk.storage``, never merged — and discovering + listing it imports
    no backend impl module. The manifest layer stays inert; the psycopg half
    of the same gate is pinned on a clean interpreter by the install smoke.
    """
    impl_prefix = "noeta.builtins.storage.impl"
    # Other tests may legitimately have imported the backends; forget them so
    # the discovery/listing below would have to RE-import to be seen here.
    saved = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == impl_prefix or name.startswith(impl_prefix + ".")
    }
    try:
        ps = load_plugins(builtins=True)
        assert "storage" in ps.names()
        listed = [(pn, c) for pn, c in ps.contributions() if pn == "storage"]
        assert listed == [], "the storage manifest must declare zero contributions"
        imported = [
            name
            for name in sys.modules
            if name == impl_prefix or name.startswith(impl_prefix + ".")
        ]
        assert not imported, (
            f"listing the storage built-in imported backend impl modules: "
            f"{imported}"
        )
    finally:
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# 9. Resolution + per-surface validation (D2 validator, D4 pipeline)
# ---------------------------------------------------------------------------


def test_resolve_runs_the_surface_validator_and_rejects_bad_values():
    def only_ints(value):
        if not isinstance(value, int):
            raise PluginError("expected an int")

    reg = standard_registry().copy()
    reg.register(SurfaceSpec("counter", "host", "host-wired", only_ints, "name", "append"))

    good = _loaded(_manifest("g", _contrib("counter", "n")), {("counter", "n"): 7})
    ps_good = PluginSet(plugins=(good,), registry=reg)
    assert ps_good.resolve()[0].value == 7

    bad = _loaded(_manifest("b", _contrib("counter", "n")), {("counter", "n"): "not-int"})
    ps_bad = PluginSet(plugins=(bad,), registry=reg)
    with pytest.raises(PluginError) as exc:
        ps_bad.resolve()
    assert "validation" in str(exc.value) or "int" in str(exc.value)


def test_resolve_imports_ref_and_reports_missing_attribute():
    ck = ContentKindSpec(kind="notes", renderer=lambda names: None)
    # A ref resolved from a real module object via the resolved-objects cache.
    lp = _loaded(_manifest("p", _contrib("content_kind", "notes", ref="m:x")), {("content_kind", "notes"): ck})
    ps = PluginSet(plugins=(lp,), registry=standard_registry())
    assert ps.resolve()[0].value is ck

    missing = _loaded(_manifest("q", _contrib("tool", "t", ref="noeta.client.plugin_set:does_not_exist")))
    ps2 = PluginSet(plugins=(missing,), registry=standard_registry())
    with pytest.raises(PluginError) as exc:
        ps2.resolve()
    assert "does_not_exist" in str(exc.value)


# ---------------------------------------------------------------------------
# 10. PluginSet accessors + explicit module/manifest-file sources
# ---------------------------------------------------------------------------


def test_pluginset_accessors():
    ps = _set(_manifest("a", _contrib("tool", "t", ref="a:t")), _manifest("b"))
    assert len(ps) == 2
    assert "a" in ps and "z" not in ps
    assert ps.get("a").name == "a"
    with pytest.raises(PluginError):
        ps.get("missing")
    assert list(p.name for p in ps) == ["a", "b"]


def test_explicit_toml_file_source_is_zero_execution(tmp_path):
    p = tmp_path / "plug.toml"
    p.write_text(
        'name = "toml-plug"\n[[contributions]]\nsurface = "guard"\nname = "g"\nref = "m:g"\n',
        encoding="utf-8",
    )
    ps = load_plugins(builtins=False, modules=[str(p)])
    assert ps.names() == ("toml-plug",)


def test_explicit_py_file_source(tmp_path):
    _write_py(tmp_path, "single", _SIMPLE_DECORATOR_PLUGIN)
    ps = load_plugins(builtins=False, modules=[str(tmp_path / "single.py")])
    assert ps.names() == ("ws-plugin",)


@pytest.mark.parametrize(
    "surface, good, bad",
    [
        ("tool", "read", None),
        ("agent", None, None),  # placeholder, replaced below
        ("content_kind", None, "x"),
        ("prompt_fragment", "hello", ""),
        ("policy", object(), None),
        ("guard", object(), None),
        ("observer", lambda: None, 3),
        ("provider", object(), None),
        ("reminder_provider", lambda: None, 3),
        ("reminder", lambda: None, 3),
        ("tool_result_transform", lambda: None, 3),
        ("mcp_server", object(), None),
        ("skills", "/tmp/skills", ""),
        ("sandbox_provider", object(), None),
    ],
)
def test_standard_validators_accept_and_reject(surface, good, bad):
    reg = standard_registry()
    validator = reg.get(surface).validator
    if surface == "agent":
        from noeta.client.options import AgentDefinition

        validator(AgentDefinition(description="d", prompt="p"))
        with pytest.raises(PluginError):
            validator(object())
        return
    if surface == "content_kind":
        validator(ContentKindSpec(kind="k", renderer=lambda names: None))
        with pytest.raises(PluginError):
            validator("x")
        return
    if good is not None or surface in {"observer", "reminder", "reminder_provider", "tool_result_transform"}:
        validator(good)
    if bad is not None or surface in {"tool", "prompt_fragment", "skills"}:
        with pytest.raises(PluginError):
            validator(bad)


def test_v_tool_rejects_non_ref_non_callable():
    from noeta.client.surfaces import standard_registry as _sr

    with pytest.raises(PluginError):
        _sr().get("tool").validator(3)


def test_builder_all_decorator_forms():
    plugin = PluginBuilder("forms")

    @plugin.guard(name="audit")
    class _Audit:  # pragma: no cover — identity only
        pass

    @plugin.observer
    def obs(event):  # pragma: no cover
        return None

    @plugin.reminder_provider(seams=["turn_intake"])
    def recall(view):  # pragma: no cover
        return ()

    def a_tool(arguments, ctx):  # pragma: no cover
        return None

    plugin.tool(a_tool, name="direct_tool")
    plugin.guard(object(), name="direct_guard")
    plugin.observer(lambda e: None, name="direct_obs")
    plugin.reminder_provider(lambda v: (), name="direct_recall", seams=("task_seed",))

    surfaces = {c.name: c for c in plugin.manifest().contributions}
    assert surfaces["recall"].params["seams"] == ("turn_intake",)
    assert surfaces["direct_recall"].params["seams"] == ("task_seed",)
    assert "audit" in surfaces and "obs" in surfaces


def test_manifest_contribution_from_path_derives_name():
    m = parse_manifest_text(
        'name = "r"\n[[contributions]]\nsurface = "skills"\npath = "skills/pack"\n'
    )
    assert m.contributions[0].name == "pack"
    assert m.contributions[0].path == "skills/pack"


def test_declared_name_keyword_builder_form(tmp_path):
    p = tmp_path / "k.py"
    p.write_text('from x import PluginBuilder\nplugin = PluginBuilder(name="kw")\n', encoding="utf-8")
    assert declared_plugin_name(p) == "kw"


def test_distribution_top_level_fallback_to_dist_name(tmp_path):
    # No top_level.txt (read_text returns None for it); the reader derives the
    # package name from the distribution name.
    pkg_dir = _make_sentinel_package(tmp_path / "src", "fallbackpkg")

    class _NoTopLevel(_FakeDist):
        def read_text(self, name):
            return None

    import sys as _sys

    _sys.path.insert(0, str(tmp_path / "src"))
    try:
        _sys.modules.pop("fallbackpkg", None)
        importlib.invalidate_caches()
        dist = _NoTopLevel(tmp_path / "src", list_files=False, top_level="", name="fallbackpkg")
        manifest = read_distribution_manifest(dist)
        assert manifest is not None and manifest.name == "sentinel-plugin"
        assert not (pkg_dir / "EXECUTED").exists()
    finally:
        _sys.path.remove(str(tmp_path / "src"))


def test_explicit_dotted_module_source(tmp_path, monkeypatch):
    (tmp_path / "dotmod.py").write_text(
        "from noeta.client.plugin_manifest import PluginBuilder\n"
        'plugin = PluginBuilder("dotted")\n'
        'plugin.prompt_fragment("hi", name="frag")\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("dotmod", None)
    importlib.invalidate_caches()
    ps = load_plugins(builtins=False, modules=["dotmod"])
    assert ps.names() == ("dotted",)


def test_import_error_in_dotted_module_fails_loudly():
    with pytest.raises(PluginError):
        load_plugins(modules=["noeta_no_such_module_xyz"])


def test_resolve_resource_only_and_bare_module_ref():
    # path-only contribution resolves to its path string.
    reg = standard_registry()
    lp = _loaded(_manifest("r", ManifestContribution(surface="skills", name="pack", path="/skills/pack")))
    assert PluginSet(plugins=(lp,), registry=reg).resolve()[0].value == "/skills/pack"

    # a ref with no attribute resolves to the module object.
    lp2 = _loaded(_manifest("m", _contrib("provider", "p", ref="noeta.client.plugin_set")))
    resolved = PluginSet(plugins=(lp2,), registry=reg).resolve()[0]
    assert resolved.value.__name__ == "noeta.client.plugin_set"


def test_priority_non_int_param_treated_as_zero():
    ps = _set(
        _manifest("a", _contrib("reminder", "x", ref="a:x", priority="high")),
        _manifest("b", _contrib("reminder", "y", ref="b:y", priority=2)),
    )
    order = [e.contribution.name for e in ps.merged().for_surface("reminder")]
    # "x" has a non-int priority -> 0, so it sorts before y(2).
    assert order == ["x", "y"]


def test_multiple_builders_without_plugin_alias_fails(tmp_path):
    _write_py(
        tmp_path,
        "two",
        """
        from noeta.client.plugin_manifest import PluginBuilder
        one = PluginBuilder("one")
        two = PluginBuilder("two")
        """,
    )
    with pytest.raises(PluginError) as exc:
        load_plugins(modules=[str(tmp_path / "two.py")])
    assert "PluginBuilder" in str(exc.value)


def test_single_file_without_static_name_execs_then_gates(tmp_path):
    # No declared name literal -> loader must exec the trusted file to learn the
    # name, then apply the allow-list on the real name.
    _write_py(
        tmp_path,
        "anon",
        """
        import noeta.client.plugin_manifest as pm
        # The name is computed, so no static literal is extractable.
        plugin = pm.PluginBuilder('runtime' + '-named')
        plugin.prompt_fragment('x', name='f')
        """,
    )
    assert declared_plugin_name(tmp_path / "anon.py") is None
    assert load_plugins(user_dirs=[tmp_path], enabled=["runtime-named"]).names() == ("runtime-named",)
    assert load_plugins(user_dirs=[tmp_path], enabled=["nope"]).names() == ()


def test_dir_subpackage_manifest_and_missing_builder(tmp_path):
    # A sub-directory with a static manifest is read without execution...
    pkg = tmp_path / "plugins" / "sub"
    pkg.mkdir(parents=True)
    (pkg / MANIFEST_BASENAME).write_text('name = "subplug"\n', encoding="utf-8")
    ps = load_plugins(builtins=False, user_dirs=[tmp_path / "plugins"])
    assert ps.names() == ("subplug",)

    # ...while a .py file with no PluginBuilder fails loudly.
    bad_dir = tmp_path / "bad"
    _write_py(bad_dir, "nobuilder", "x = 1\n")
    with pytest.raises(PluginError) as exc:
        load_plugins(user_dirs=[bad_dir])
    assert "PluginBuilder" in str(exc.value)
