"""The host plane closes: `skills`, `mcp_server`, operator config, `requires-noeta`.

Four of the sixteen surfaces used to be declarable and silently ignored. This
file is the proof that the two with a real consumption story now have one, that
the two without say so out loud, and that the three defects around them are
fixed:

* a plugin's ``skills`` path becomes a real skill tier — lowest, so the user's
  own workspace pack still shadows it — and a missing directory contributes
  nothing rather than failing a session;
* a plugin's ``mcp_server`` joins the effective ``Options.mcp_servers``, with
  one alias namespace and no override;
* ``HostConfig.plugin_config`` is the operator-config channel
  ``docs/how-to/write-a-plugin.md`` documents, so a third-party pack's
  ``ctx.config("<name>")`` is no longer always ``{}``;
* ``requires-noeta`` is evaluated — a warning by default, a refusal under
  ``strict=True``, never a crash on a spelling the loader cannot parse.

The plugins here are third-party-shaped single files: nothing under
``packages/`` is edited to make any of them work, which is the whole point of a
surface.
"""

from __future__ import annotations

import importlib.metadata
import textwrap
import warnings
from pathlib import Path
from typing import Any

import pytest

from noeta.client.host import SdkHost
from noeta.client.mcp_server import SdkMcpServer
from noeta.client.plugin_manifest import ManifestContribution, PluginManifest
from noeta.client.plugin_set import (
    LoadedPlugin,
    PluginSet,
    _specifier_satisfied,
    load_plugins,
)
from noeta.client.plugins import PluginError, PluginVersionWarning
from noeta.client.surfaces import standard_registry
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Client, HostConfig, Options
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider

from tests._skill_fixtures import write_skill_raw


# ---------------------------------------------------------------------------
# Harness (mirrors tests/test_session_pack_extension.py)
# ---------------------------------------------------------------------------


def _write_plugin(root: Path, name: str, body: str) -> str:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plugin.py"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return str(path)


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _bare(**kw: Any) -> Options:
    return Options(system_prompt="You are a helpful assistant.", **kw)


def _drive(
    tmp_path: Path,
    options: Options,
    plugins: Any,
    *,
    host_config: HostConfig | None = None,
) -> FakeLLMProvider:
    """Run one turn through a real ``Client``; return the provider it spoke to."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    provider = FakeLLMProvider(responses=[_end_turn()])
    client = Client(
        options,
        provider=provider,
        workspace_dir=workspace,
        plugins=plugins,
        multi_turn=False,
        host_config=host_config,
    )
    try:
        client.start(goal="hello")
    finally:
        client.shutdown()
    return provider


def _tool_names(provider: FakeLLMProvider, index: int = 0) -> frozenset[str]:
    return frozenset(
        spec["function"]["name"]
        for spec in (provider.received_requests[index].tools or ())
    )


def _skill_menu(provider: FakeLLMProvider, index: int = 0) -> dict[str, Any]:
    """The ``skill`` control tool's schema off a composed request, or ``{}``."""
    for spec in provider.received_requests[index].tools or ():
        if spec.get("function", {}).get("name") == "skill":
            return spec["function"]["parameters"]["properties"]["skill"]
    return {}


def _host(workspace: Path, *, overrides: dict[str, dict[str, Any]]) -> SdkHost:
    """A bare ``SdkHost`` for the config-bag unit tests (no engine is built)."""
    dispatcher = InMemoryDispatcher()
    return SdkHost(
        event_log=InMemoryEventLog(lease_validator=dispatcher),
        content_store=InMemoryContentStore(),
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=[]),
        workspace_dir=workspace,
        plugin_config_overrides=overrides,
    )


def _skill_body(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\nBody of {name}.\n"


def _loaded(manifest: PluginManifest) -> LoadedPlugin:
    return LoadedPlugin(
        name=manifest.name,
        manifest=manifest,
        origin=f"test {manifest.name!r}",
        source="test",
    )


def _set(*manifests: PluginManifest) -> PluginSet:
    return PluginSet(
        plugins=tuple(_loaded(m) for m in manifests), registry=standard_registry()
    )


# ===========================================================================
# 1. `skills` — a path contribution is a real, lowest-precedence tier
# ===========================================================================


_SKILLS_PLUGIN = """
    noeta_plugin_name = "packrat"
    from noeta.sdk import PluginBuilder

    plugin = PluginBuilder("packrat")
    plugin.contribute("skills", name="pack", path={path!r})
"""


def test_plugin_skills_path_reaches_the_session_menu(tmp_path: Path) -> None:
    """A plugin's skill pack shows up in the model's menu, with no host wiring.

    The end-to-end claim ``plugin-surfaces.md`` used to make and no code kept:
    contribute a directory, and the skills inside it are indexed into the same
    catalogue the ``skill`` control tool renders.
    """
    pack = tmp_path / "shipped-skills"
    write_skill_raw(pack, "runbook", _skill_body("runbook", "operate the thing"))
    plugin = _write_plugin(tmp_path, "packrat", _SKILLS_PLUGIN.format(path=str(pack)))
    plugins = load_plugins(builtins=False, modules=[plugin])

    provider = _drive(
        tmp_path, _bare(plugins=("fs", "web", "skill_invocation")), plugins
    )
    menu = _skill_menu(provider)
    assert menu.get("enum") == ["runbook"]
    assert "operate the thing" in menu["description"]


def test_workspace_skill_shadows_a_plugin_skill(tmp_path: Path) -> None:
    """The plugin tier is the LOWEST one: a workspace skill of the same name wins.

    Precedence matters more than presence here — an operator installing a plugin
    must not be able to silently replace a skill the user wrote in their own
    workspace.
    """
    pack = tmp_path / "shipped-skills"
    write_skill_raw(pack, "runbook", _skill_body("runbook", "PLUGIN version"))
    workspace_pack = tmp_path / "ws" / ".noeta" / "skills"
    write_skill_raw(workspace_pack, "runbook", _skill_body("runbook", "MY version"))
    plugin = _write_plugin(tmp_path, "packrat", _SKILLS_PLUGIN.format(path=str(pack)))
    plugins = load_plugins(builtins=False, modules=[plugin])

    provider = _drive(
        tmp_path, _bare(plugins=("fs", "web", "skill_invocation")), plugins
    )
    menu = _skill_menu(provider)
    assert menu.get("enum") == ["runbook"]
    assert "MY version" in menu["description"]
    assert "PLUGIN version" not in menu["description"]


def test_nonexistent_plugin_skills_path_contributes_nothing(tmp_path: Path) -> None:
    """A path that is not on disk is an empty tier, not a broken session.

    A plugin may ship its packs conditionally (an extra, a platform build); the
    indexer already treats a missing root as empty, so the surface leans on that
    instead of inventing a failure mode.
    """
    missing = tmp_path / "not-shipped"
    write_skill_raw(tmp_path / "ws" / ".noeta" / "skills", "tidy", _skill_body("tidy", "t"))
    plugin = _write_plugin(
        tmp_path, "packrat", _SKILLS_PLUGIN.format(path=str(missing))
    )
    plugins = load_plugins(builtins=False, modules=[plugin])

    provider = _drive(
        tmp_path, _bare(plugins=("fs", "web", "skill_invocation")), plugins
    )
    assert _skill_menu(provider).get("enum") == ["tidy"]


def test_relative_skills_path_is_refused_naming_the_plugin(tmp_path: Path) -> None:
    """A relative path has no unambiguous root, so it is refused, not guessed.

    The same manifest is read from a wheel's package data, a bare ``.toml`` and
    a single ``.py``; resolving ``"skills"`` against each would point at three
    different directories depending only on how the plugin was installed.
    """
    plugin = _write_plugin(
        tmp_path, "packrat", _SKILLS_PLUGIN.format(path="skills/pack")
    )
    plugins = load_plugins(builtins=False, modules=[plugin])
    with pytest.raises(PluginError) as exc:
        plugins.host_skills_dirs()
    assert "packrat" in str(exc.value) and "ABSOLUTE" in str(exc.value)


def test_host_skills_dirs_orders_by_plugin_then_name() -> None:
    """The projection is deterministic under any discovery order."""
    pset = _set(
        PluginManifest(
            name="zeta",
            contributions=(
                ManifestContribution(surface="skills", name="a", path="/z/a"),
            ),
        ),
        PluginManifest(
            name="alpha",
            contributions=(
                ManifestContribution(surface="skills", name="b", path="/a/b"),
                ManifestContribution(surface="skills", name="a", path="/a/a"),
            ),
        ),
    )
    assert pset.host_skills_dirs() == (
        Path("/a/a"),
        Path("/a/b"),
        Path("/z/a"),
    )


# ===========================================================================
# 2. `mcp_server` — one alias namespace with the recipe, no override
# ===========================================================================


_MCP_PLUGIN = """
    noeta_plugin_name = "{plugin}"
    from noeta.protocols.tool import ToolResult
    from noeta.sdk import PluginBuilder, create_sdk_mcp_server, tool

    plugin = PluginBuilder("{plugin}")

    _SCHEMA = {{
        "type": "object",
        "properties": {{"text": {{"type": "string"}}}},
        "additionalProperties": False,
    }}


    @tool(name="{toolname}", version="1", risk_level="low", input_schema=_SCHEMA)
    def _bundled(arguments, ctx):
        return ToolResult(success=True, output=str(arguments.get("text", "")))


    SERVER = create_sdk_mcp_server("{alias}", tools=[_bundled])
    plugin.contribute("mcp_server", SERVER, name="{alias}")
"""


def _mcp_plugin(root: Path, *, plugin: str, alias: str, toolname: str) -> str:
    return _write_plugin(
        root,
        plugin,
        _MCP_PLUGIN.format(plugin=plugin, alias=alias, toolname=toolname),
    )


def test_plugin_mcp_server_tool_reaches_the_built_session(tmp_path: Path) -> None:
    """A contributed in-process server's tools mount, with no ``Options`` edit.

    The surface is host-wired, so this needs no activation: loading the plugin
    is what puts the server in the process.
    """
    plugin = _mcp_plugin(tmp_path, plugin="ticketry", alias="tickets", toolname="ticket_open")
    plugins = load_plugins(builtins=False, modules=[plugin])

    provider = _drive(tmp_path, _bare(), plugins)
    assert "ticket_open" in _tool_names(provider)


def test_plugin_alias_colliding_with_options_names_both_sides(tmp_path: Path) -> None:
    """An alias the recipe already claims is refused — the merge's rule, extended."""
    from noeta.sdk import create_sdk_mcp_server

    plugin = _mcp_plugin(tmp_path, plugin="ticketry", alias="tickets", toolname="ticket_open")
    plugins = load_plugins(builtins=False, modules=[plugin])
    mine = create_sdk_mcp_server("tickets")

    with pytest.raises(PluginError) as exc:
        _drive(tmp_path, _bare(mcp_servers=(mine,)), plugins)
    message = str(exc.value)
    assert "tickets" in message
    assert "ticketry" in message and "Options.mcp_servers" in message


def test_two_plugins_claiming_one_alias_collide(tmp_path: Path) -> None:
    """Plugin ↔ plugin is the manifest merge's existing rule, reached at build."""
    a = _mcp_plugin(tmp_path, plugin="left", alias="tickets", toolname="left_open")
    b = _mcp_plugin(tmp_path, plugin="right", alias="tickets", toolname="right_open")
    plugins = load_plugins(builtins=False, modules=[a, b])

    with pytest.raises(PluginError) as exc:
        _drive(tmp_path, _bare(), plugins)
    assert "'left'" in str(exc.value) and "'right'" in str(exc.value)


def test_mcp_contribution_of_the_wrong_shape_names_the_plugin(tmp_path: Path) -> None:
    """A value that is not an ``SdkMcpServer`` fails loudly, attributed.

    Notably this is where an author reaching for a *remote* server spec lands:
    the message points them at ``HostConfig.mcp_server_resolver``, because a
    static manifest is the wrong place for a url and a token.
    """
    plugin = _write_plugin(
        tmp_path,
        "bogus",
        """
        noeta_plugin_name = "bogus"
        from noeta.sdk import PluginBuilder

        plugin = PluginBuilder("bogus")
        NOT_A_SERVER = {"url": "https://example.invalid", "token": "t"}
        plugin.contribute("mcp_server", NOT_A_SERVER, name="tickets")
        """,
    )
    plugins = load_plugins(builtins=False, modules=[plugin])
    with pytest.raises(PluginError) as exc:
        plugins.host_mcp_servers()
    assert "bogus" in str(exc.value) and "SdkMcpServer" in str(exc.value)
    assert "mcp_server_resolver" in str(exc.value)


def test_host_mcp_servers_projects_alias_plugin_and_value(tmp_path: Path) -> None:
    plugin = _mcp_plugin(tmp_path, plugin="ticketry", alias="tickets", toolname="ticket_open")
    plugins = load_plugins(builtins=False, modules=[plugin])
    projected = plugins.host_mcp_servers()
    assert len(projected) == 1
    alias, owner, server = projected[0]
    assert (alias, owner) == ("tickets", "ticketry")
    assert isinstance(server, SdkMcpServer)


# ===========================================================================
# 3. `HostConfig.plugin_config` — the operator-config channel actually works
# ===========================================================================


_CONFIG_PLUGIN = """
    noeta_plugin_name = "runbooks"
    from noeta.execution.session_pack import PackContribution
    from noeta.sdk import PluginBuilder
    from noeta.tools.fake import FakeTool

    plugin = PluginBuilder("runbooks", config_schema={"tool_name": "str"})

    def runbook_pack(ctx):
        # The documented route: a pack reads ONLY its own entry, by plugin name.
        name = ctx.config("runbooks").get("tool_name")
        if not name:
            return PackContribution()
        return PackContribution(tools={name: FakeTool(name=name)})

    plugin.session_pack(runbook_pack, name="runbook", priority=1100)
"""


def test_third_party_pack_reads_its_own_host_config(tmp_path: Path) -> None:
    """``ctx.config("<plugin>")`` returns what the host put under that name.

    Before this, the config bag held exactly four hardcoded built-in keys, so a
    third-party pack's own entry was always ``{}`` while the how-to told authors
    to read it. The pack self-gates on an empty entry, so the same plugin with
    no config contributes nothing — which is the second half of the proof.
    """
    plugin = _write_plugin(tmp_path, "runbooks", _CONFIG_PLUGIN)
    plugins = load_plugins(builtins=False, modules=[plugin])
    options = _bare(plugins=("fs", "web", "runbooks"))

    configured = _drive(
        tmp_path,
        options,
        plugins,
        host_config=HostConfig(plugin_config={"runbooks": {"tool_name": "deploy_book"}}),
    )
    assert "deploy_book" in _tool_names(configured)

    unconfigured = _drive(tmp_path, options, plugins)
    assert "deploy_book" not in _tool_names(unconfigured)


def test_host_override_wins_per_key_and_leaves_derived_keys(tmp_path: Path) -> None:
    """Overriding one ``fs`` key must not delete the ones it did not mention.

    A shallow per-key overlay: replacing the whole entry would make supplying
    ``shell_allowlist`` silently drop ``write_mode``, which is the kind of
    override that reads as working right up until a write happens.
    """
    host = _host(
        tmp_path,
        overrides={
            "fs": {"shell_allowlist": ("git status",)},
            "runbooks": {"tool_name": "deploy_book"},
        },
    )
    config = host._plugin_config(shell_mode="deny")
    assert config["fs"]["shell_allowlist"] == ("git status",)
    # The keys the host did NOT name survive the overlay.
    assert "write_mode" in config["fs"] and "shell_mode" in config["fs"]
    # A name the SDK derives nothing for passes through verbatim.
    assert config["runbooks"] == {"tool_name": "deploy_book"}


def test_overrides_do_not_reinstate_the_reduced_environment(tmp_path: Path) -> None:
    """The orchestration build's deliberate omissions stay omitted.

    ``_plugin_config(spec=None)`` leaves out the lower skill tiers and the whole
    ``memory`` entry on purpose. An override may add them back — that is an
    explicit act — but merely configuring something else must not.
    """
    host = _host(
        tmp_path,
        overrides={"runbooks": {"tool_name": "x"}},
    )
    reduced = host._plugin_config(shell_mode="deny")
    assert "memory" not in reduced
    assert "builtin_skills_dirs" not in reduced["skills"]
    assert reduced["runbooks"] == {"tool_name": "x"}


# ===========================================================================
# 4. `requires-noeta` — enforced as a warning, refused under `strict`
# ===========================================================================


_VERSIONED_PLUGIN = """
    noeta_plugin_name = "vplug"
    from noeta.sdk import PluginBuilder

    plugin = PluginBuilder("vplug", requires_noeta={spec!r})
    plugin.prompt_fragment("hi", name="frag")
"""


def _versioned(root: Path, spec: str) -> str:
    return _write_plugin(root, "vplug", _VERSIONED_PLUGIN.format(spec=spec))


def _installed(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    """Pin the version ``requires-noeta`` is compared against."""

    def fake_version(name: str) -> str:
        assert name == "noeta-sdk"
        if version is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return version

    monkeypatch.setattr(importlib.metadata, "version", fake_version)


@pytest.mark.parametrize(
    "spec, installed, expected",
    [
        (">=0.4", "0.6.0", True),
        (">=0.7", "0.6.0", False),
        (">0.6", "0.6.0", False),
        (">0.5", "0.6.0", True),
        ("<=0.6", "0.6.0", True),
        ("<0.6", "0.6.0", False),
        ("==0.6", "0.6.0", True),          # 0.6 and 0.6.0 pad to equal
        ("!=0.6.0", "0.6.0", False),
        (" >= 0.4 , < 1.0 ", "0.6.0", True),
        (">=0.4,<0.5", "0.6.0", False),
        ("~=0.6", "0.6.0", None),          # legal PEP 440, not implemented here
        ("0.6", "0.6.0", None),            # no operator
        ("", "0.6.0", None),
        (">=0.4", "not-a-version", None),
    ],
)
def test_specifier_evaluator(spec: str, installed: str, expected: bool | None) -> None:
    """The hand-rolled range check, operator by operator.

    ``None`` means "unrecognised" — reported to the caller rather than guessed
    at, which is what keeps a plugin from being refused over a spelling this
    loader never promised to parse.
    """
    assert _specifier_satisfied(spec, installed) is expected


def test_satisfied_requires_noeta_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch, "0.6.0")
    plugin = _versioned(tmp_path, ">=0.4,<1.0")
    with warnings.catch_warnings():
        warnings.simplefilter("error", PluginVersionWarning)
        assert load_plugins(builtins=False, modules=[plugin]).names() == ("vplug",)


def test_unsatisfied_requires_noeta_warns_and_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default is a warning: a range is the author's claim, not a lock.

    Refusing on it would break a working deployment on a patch bump the plugin
    simply had not been re-tested against, so the loader reports and continues.
    """
    _installed(monkeypatch, "0.6.0")
    plugin = _versioned(tmp_path, ">=9.0")
    with pytest.warns(PluginVersionWarning) as record:
        loaded = load_plugins(builtins=False, modules=[plugin])
    assert loaded.names() == ("vplug",)
    message = str(record[0].message)
    assert "'vplug'" in message and ">=9.0" in message and "0.6.0" in message


def test_strict_refuses_an_unsatisfied_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch, "0.6.0")
    plugin = _versioned(tmp_path, ">=9.0")
    with pytest.raises(PluginError) as exc:
        load_plugins(builtins=False, modules=[plugin], strict=True)
    assert "'vplug'" in str(exc.value) and ">=9.0" in str(exc.value)


@pytest.mark.parametrize("strict", [False, True])
def test_unrecognized_specifier_warns_and_is_never_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    """A spelling this loader never promised must not refuse a working plugin.

    ``~=`` is legal PEP 440; the hand-rolled evaluator does not implement it,
    and reporting that honestly beats either guessing at the bound or importing
    a dependency for one advisory field.
    """
    _installed(monkeypatch, "0.6.0")
    plugin = _versioned(tmp_path, "~=0.6")
    with pytest.warns(PluginVersionWarning, match="unrecognized requires-noeta"):
        loaded = load_plugins(builtins=False, modules=[plugin], strict=strict)
    assert loaded.names() == ("vplug",)


@pytest.mark.parametrize("strict", [False, True])
def test_unparseable_installed_version_warning_names_the_installed_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    """A pre-release install (``1.0.0rc1``) fails the evaluator on the
    INSTALLED side; the warning must say so instead of blaming the plugin's
    perfectly ordinary specifier — and enforcement is skipped, never guessed.
    """
    _installed(monkeypatch, "1.0.0rc1")
    plugin = _versioned(tmp_path, ">=0.4")
    with pytest.warns(PluginVersionWarning, match="1.0.0rc1.*unparseable") as record:
        loaded = load_plugins(builtins=False, modules=[plugin], strict=strict)
    assert loaded.names() == ("vplug",)
    assert "unrecognized" not in str(record[0].message)


def test_absent_distribution_metadata_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-repo checkout has no version to compare against, so nothing fires."""
    _installed(monkeypatch, None)
    plugin = _versioned(tmp_path, ">=9.0")
    with warnings.catch_warnings():
        warnings.simplefilter("error", PluginVersionWarning)
        assert load_plugins(
            builtins=False, modules=[plugin], strict=True
        ).names() == ("vplug",)


# ===========================================================================
# 5. No declared-but-dead surface — the docs claim matches the code
# ===========================================================================


_SURFACES_DOC = (
    Path(__file__).resolve().parents[1] / "docs" / "reference" / "plugin-surfaces.md"
)

#: Surfaces with no automatic consumer, by design. Each must say so in the
#: reference, in these words, so an author reading the catalogue can tell
#: "declared and wired for you" from "declared for the host to pick up".
_HOST_RESOLVED = frozenset({"provider", "sandbox_provider"})


def test_every_registered_surface_is_documented() -> None:
    """A surface with no section is a surface nobody can discover how to use."""
    doc = _SURFACES_DOC.read_text(encoding="utf-8")
    documented = {
        line.removeprefix("### `").removesuffix("`")
        for line in doc.splitlines()
        if line.startswith("### `") and line.endswith("`")
    }
    assert set(standard_registry().names()) <= documented


@pytest.mark.parametrize("surface", sorted(_HOST_RESOLVED))
def test_unconsumed_surfaces_say_host_resolved_listing(surface: str) -> None:
    """The acceptance rule: consumed by code, or documented as listing-only.

    ``provider`` and ``sandbox_provider`` are deliberately never auto-bound — a
    process has one of each and the deployment chooses it. That is a legitimate
    design, but only if the catalogue says so; "declared and silently ignored"
    is what this whole slice exists to end.
    """
    doc = _SURFACES_DOC.read_text(encoding="utf-8")
    section = doc.split(f"### `{surface}`", 1)[1].split("\n### ", 1)[0].lower()
    assert "host-resolved listing" in section
    assert "never auto-consumed" in section
