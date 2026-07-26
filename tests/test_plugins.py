"""Tests for ``noeta.client.plugins`` — the plugin mechanism (M1).

Covers the spec's acceptance criteria:

1. Identity property: an ``Options`` assembled via :func:`merge_plugins`
   compiles to an ``AgentSpec`` byte-identical to the equivalent hand-written
   ``Options``.
2. Order invariance: loading ``[A, B]`` vs ``[B, A]`` yields identical specs.
3. Collision errors name **both** contributors (tool / agent / content kind /
   mcp alias), a second provider conflict, and collisions with the base
   ``Options``.
4. Trust gating of workspace directories against the trust store.
5. Entry-point loading via an injected iterable, config passing, and loud
   broken-plugin failure.
"""

from __future__ import annotations

import textwrap

import pytest

from noeta.client.plugins import (
    LoadedPlugin,
    PluginAPI,
    PluginError,
    UntrustedPluginDirWarning,
    grant_trust,
    is_trusted,
    load_plugins,
    merge_plugins,
    merged_mcp_servers,
    merged_skill_dirs,
)
from noeta.context.content_channel import ContentKindSpec
from noeta.sdk import AgentDefinition, Options, compile_options
from noeta.tools.decorator import tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "string"}},
    "required": ["x"],
    "additionalProperties": False,
}


@tool(name="plugin_tool", version="2", risk_level="medium", input_schema=_SCHEMA)
def plugin_tool(arguments, ctx):  # pragma: no cover — identity-only
    raise NotImplementedError


def _loaded(name: str, factory) -> LoadedPlugin:
    """Run a plugin factory into a :class:`LoadedPlugin` (bypass discovery)."""
    api = PluginAPI(name)
    factory(api)
    return LoadedPlugin(name=name, contributions=api._contributions())


def _worker(prompt: str = "do the work") -> AgentDefinition:
    return AgentDefinition(description="a worker", prompt=prompt, tools=("read",))


# ---------------------------------------------------------------------------
# 1. Identity property
# ---------------------------------------------------------------------------


def test_merge_is_byte_identical_to_hand_written():
    base = Options(system_prompt="root", allowed_tools=("read", "grep"))

    def factory(api: PluginAPI) -> None:
        api.add_tool(plugin_tool)
        api.add_agent("worker", _worker())

    merged = merge_plugins(base, [_loaded("alpha", factory)])

    hand = Options(
        system_prompt="root",
        allowed_tools=("read", "grep", plugin_tool),
        agents={"worker": _worker()},
    )

    m_main, m_desc = compile_options(merged)
    h_main, h_desc = compile_options(hand)
    assert m_main == h_main
    assert m_desc == h_desc


# ---------------------------------------------------------------------------
# 2. Order invariance
# ---------------------------------------------------------------------------


def test_merge_order_invariance():
    base = Options(system_prompt="root", allowed_tools=("read",))

    @tool(name="tool_a", version="1", risk_level="low", input_schema=_SCHEMA)
    def tool_a(arguments, ctx):  # pragma: no cover
        raise NotImplementedError

    @tool(name="tool_b", version="1", risk_level="low", input_schema=_SCHEMA)
    def tool_b(arguments, ctx):  # pragma: no cover
        raise NotImplementedError

    a = _loaded("plugin_a", lambda api: (api.add_tool(tool_a), api.add_agent("agent_a", _worker())))
    b = _loaded("plugin_b", lambda api: (api.add_tool(tool_b), api.add_agent("agent_b", _worker())))

    forward = compile_options(merge_plugins(base, [a, b]))
    reverse = compile_options(merge_plugins(base, [b, a]))
    assert forward == reverse


# ---------------------------------------------------------------------------
# 3. Collisions
# ---------------------------------------------------------------------------


def test_tool_collision_names_both_plugins():
    base = Options(system_prompt="root", allowed_tools=())
    a = _loaded("plugin_a", lambda api: api.add_tool(plugin_tool))
    b = _loaded("plugin_b", lambda api: api.add_tool(plugin_tool))
    with pytest.raises(PluginError) as exc:
        merge_plugins(base, [a, b])
    msg = str(exc.value)
    assert "plugin_a" in msg and "plugin_b" in msg
    assert "plugin_tool" in msg


def test_agent_collision_names_both_plugins():
    base = Options(system_prompt="root", allowed_tools=())
    a = _loaded("plugin_a", lambda api: api.add_agent("worker", _worker()))
    b = _loaded("plugin_b", lambda api: api.add_agent("worker", _worker()))
    with pytest.raises(PluginError) as exc:
        merge_plugins(base, [a, b])
    assert "plugin_a" in str(exc.value) and "plugin_b" in str(exc.value)


def test_content_kind_collision_names_both_plugins():
    base = Options(system_prompt="root", allowed_tools=())
    spec = ContentKindSpec(kind="notes", renderer=lambda names: None)
    a = _loaded("plugin_a", lambda api: api.add_content_kind(spec))
    b = _loaded("plugin_b", lambda api: api.add_content_kind(spec))
    with pytest.raises(PluginError) as exc:
        merge_plugins(base, [a, b])
    m = str(exc.value)
    assert "plugin_a" in m and "plugin_b" in m and "notes" in m


def test_mcp_alias_collision_names_both_plugins():
    base = Options(system_prompt="root", allowed_tools=())
    a = _loaded("plugin_a", lambda api: api.add_mcp_server("srv", object()))
    b = _loaded("plugin_b", lambda api: api.add_mcp_server("srv", object()))
    with pytest.raises(PluginError) as exc:
        merge_plugins(base, [a, b])
    m = str(exc.value)
    assert "plugin_a" in m and "plugin_b" in m and "srv" in m


def test_second_provider_conflict():
    base = Options(system_prompt="root", allowed_tools=())
    a = _loaded("plugin_a", lambda api: api.set_provider(object()))
    b = _loaded("plugin_b", lambda api: api.set_provider(object()))
    with pytest.raises(PluginError) as exc:
        merge_plugins(base, [a, b])
    m = str(exc.value)
    assert "plugin_a" in m and "plugin_b" in m


def test_provider_conflict_with_base():
    base = Options(system_prompt="root", allowed_tools=(), provider=object())
    a = _loaded("plugin_a", lambda api: api.set_provider(object()))
    with pytest.raises(PluginError) as exc:
        merge_plugins(base, [a])
    m = str(exc.value)
    assert "base options" in m and "plugin_a" in m


def test_tool_collision_with_base_options():
    base = Options(system_prompt="root", allowed_tools=("read",))
    a = _loaded("plugin_a", lambda api: api.add_tool("read"))
    with pytest.raises(PluginError) as exc:
        merge_plugins(base, [a])
    m = str(exc.value)
    assert "base options" in m and "plugin_a" in m and "read" in m


def test_tool_collision_with_base_default_builtins():
    # allowed_tools=None ⇒ the full built-in set seeds the registry, so a
    # plugin adding a built-in name collides with the base.
    base = Options(system_prompt="root")
    a = _loaded("plugin_a", lambda api: api.add_tool("grep"))
    with pytest.raises(PluginError) as exc:
        merge_plugins(base, [a])
    assert "base options" in str(exc.value) and "grep" in str(exc.value)


def test_within_plugin_duplicate_tool_raises_at_factory_time():
    api = PluginAPI("dup")
    api.add_tool(plugin_tool)
    with pytest.raises(PluginError):
        api.add_tool(plugin_tool)


# ---------------------------------------------------------------------------
# 4. Trust gating
# ---------------------------------------------------------------------------


def _write_plugin(directory, stem: str, body: str):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.py").write_text(textwrap.dedent(body), encoding="utf-8")


_SIMPLE_PLUGIN = """
    def noeta_plugin(api):
        api.add_tool("grep")
"""


def test_trusted_dir_loads_unconditionally(tmp_path):
    home = tmp_path / "home_plugins"
    _write_plugin(home, "hp", _SIMPLE_PLUGIN)
    plugins = load_plugins(trusted_dirs=[home])
    assert [p.name for p in plugins] == ["hp"]
    assert plugins[0].contributions.tools[0][0] == "grep"


def test_untrusted_workspace_dir_skipped_with_warning(tmp_path):
    ws = tmp_path / "ws_plugins"
    _write_plugin(ws, "wp", _SIMPLE_PLUGIN)
    store = tmp_path / "trust.json"
    with pytest.warns(UntrustedPluginDirWarning):
        plugins = load_plugins(workspace_dirs=[ws], trust_store=store)
    assert plugins == []


def test_trusted_workspace_dir_loads(tmp_path):
    ws = tmp_path / "ws_plugins"
    _write_plugin(ws, "wp", _SIMPLE_PLUGIN)
    store = tmp_path / "trust.json"
    assert is_trusted(ws, store) is False
    grant_trust(ws, store)
    assert is_trusted(ws, store) is True
    plugins = load_plugins(workspace_dirs=[ws], trust_store=store)
    assert [p.name for p in plugins] == ["wp"]


def test_grant_trust_idempotent(tmp_path):
    store = tmp_path / "trust.json"
    d = tmp_path / "x"
    grant_trust(d, store)
    grant_trust(d, store)
    import json

    data = json.loads(store.read_text())
    assert data["trusted"].count(str(d.absolute())) == 1


# ---------------------------------------------------------------------------
# 5. Entry points, config, broken plugins
# ---------------------------------------------------------------------------


class _FakeEntryPoint:
    """A minimal ``importlib.metadata.EntryPoint`` stand-in for injection."""

    def __init__(self, name, factory):
        self.name = name
        self._factory = factory

    def load(self):
        return self._factory


def test_entry_point_loading_via_injection():
    def factory(api):
        api.add_tool("grep")

    plugins = load_plugins(entry_points=[_FakeEntryPoint("ep_plugin", factory)])
    assert [p.name for p in plugins] == ["ep_plugin"]
    assert plugins[0].contributions.tools[0][0] == "grep"


def test_entry_points_off_by_default():
    assert load_plugins() == []


def test_config_passed_only_to_factories_that_accept_it():
    captured = {}

    def with_config(api, config):
        captured["cfg"] = config
        api.add_tool("grep")

    def without_config(api):
        api.add_tool("read")

    plugins = load_plugins(
        entry_points=[
            _FakeEntryPoint("cfg_plugin", with_config),
            _FakeEntryPoint("nocfg_plugin", without_config),
        ],
        config={"cfg_plugin": {"threshold": 5}},
    )
    assert captured["cfg"] == {"threshold": 5}
    assert {p.name for p in plugins} == {"cfg_plugin", "nocfg_plugin"}


def test_config_defaults_to_empty_dict_for_config_factory():
    captured = {}

    def with_config(api, config):
        captured["cfg"] = config

    load_plugins(entry_points=[_FakeEntryPoint("cfg_plugin", with_config)])
    assert captured["cfg"] == {}


def test_enabled_allowlist_filters():
    def factory(api):
        api.add_tool("grep")

    plugins = load_plugins(
        entry_points=[
            _FakeEntryPoint("keep", factory),
            _FakeEntryPoint("drop", factory),
        ],
        enabled=["keep"],
    )
    assert [p.name for p in plugins] == ["keep"]


def test_broken_factory_fails_loudly_with_name():
    def factory(api):
        raise ValueError("boom")

    with pytest.raises(PluginError) as exc:
        load_plugins(entry_points=[_FakeEntryPoint("broken", factory)])
    assert "broken" in str(exc.value)


def test_missing_factory_in_file_fails_loudly(tmp_path):
    d = tmp_path / "plugins"
    _write_plugin(d, "empty", "x = 1\n")
    with pytest.raises(PluginError) as exc:
        load_plugins(trusted_dirs=[d])
    assert "empty" in str(exc.value)


def test_import_error_in_file_fails_loudly(tmp_path):
    d = tmp_path / "plugins"
    _write_plugin(d, "bad", "import a_module_that_does_not_exist_xyz\n")
    with pytest.raises(PluginError) as exc:
        load_plugins(trusted_dirs=[d])
    assert "bad" in str(exc.value)


def test_duplicate_plugin_name_across_sources_raises():
    def factory(api):
        api.add_tool("grep")

    with pytest.raises(PluginError) as exc:
        load_plugins(
            entry_points=[
                _FakeEntryPoint("dup", factory),
                _FakeEntryPoint("dup", factory),
            ]
        )
    assert "dup" in str(exc.value)


# ---------------------------------------------------------------------------
# Host-plane contribution accessors
# ---------------------------------------------------------------------------


def test_merged_mcp_servers_and_skill_dirs(tmp_path):
    spec_a = object()
    d1 = tmp_path / "skills_one"

    def factory(api):
        api.add_mcp_server("srv_a", spec_a)
        api.add_skill_dir(d1)

    lp = _loaded("host_plugin", factory)
    assert merged_mcp_servers([lp]) == {"srv_a": spec_a}
    assert merged_skill_dirs([lp]) == (d1.absolute(),)


def test_module_loading_via_explicit_path(tmp_path):
    d = tmp_path / "mods"
    _write_plugin(d, "mymod", _SIMPLE_PLUGIN)
    plugins = load_plugins(modules=[str(d / "mymod.py")])
    assert [p.name for p in plugins] == ["mymod"]
