"""The plugin surfaces reach the runtime — the wiring half of the D3 catalogue.

``tests/test_plugin_mechanism.py`` proves a manifest **loads, lists and merges**;
``tests/test_example_new_surfaces.py`` proves each example's resolved callable
satisfies its surface's runtime contract in isolation. Neither proves the middle:
that :class:`~noeta.sdk.Client` actually carries a declared contribution from the
loaded set into the engine it builds. A surface can be registered, validated,
merged and documented and still be a total no-op — which is exactly what the
``reminder`` / ``reminder_provider`` / ``content_kind`` surfaces were.

So each test here starts from a real ``Client`` (or the compile / loader boundary
it delegates to) and asserts the *effect*: the reminder renders into the composed
request, the provider's output lands in the ledger, the collision is refused, the
unactivated plugin never runs. Companion to the surface-local tests, not a
replacement for them.
"""

from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path
from typing import Any, Sequence

import pytest

from noeta.builtins import BUILTIN_PLUGIN_NAMES, assert_activation_vocabulary
from noeta.client.options import (
    BUILTIN_ACTIVATIONS,
    AgentDefinition,
    Options,
    PluginActivation,
    compile_options,
)
from noeta.client.plugin_set import load_plugins
from noeta.client.plugins import PluginError
from noeta.core.engine import Engine
from noeta.core.composer import PassthroughComposer
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.runtime.tool import ToolRuntime
from noeta.sdk import Client
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plugin(root: Path, name: str, body: str) -> str:
    """Write a single-file plugin under ``root/name/plugin.py``; return its path."""
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


def _client(
    tmp_path: Path, options: Options, provider: Any, plugins: Any = None
) -> Client:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return Client(
        options,
        provider=provider,
        workspace_dir=workspace,
        plugins=plugins,
        multi_turn=False,
    )


def _request_text(provider: FakeLLMProvider, index: int = 0) -> str:
    """Every text block of one composed request, flattened."""
    request = provider.received_requests[index]
    return "\n".join(
        block.text
        for message in request.messages
        for block in message.content
        if isinstance(block, TextBlock)
    )


def _request_tool_names(provider: FakeLLMProvider, index: int = 0) -> frozenset[str]:
    """The tool names on one composed request's wire schema."""
    return frozenset(
        spec["function"]["name"]
        for spec in (provider.received_requests[index].tools or ())
    )


def _bare(**kw: Any) -> Options:
    return Options(system_prompt="You are a helpful assistant.", **kw)


# ===========================================================================
# Track B — a compose-time ``reminder`` reaches the composer (D8)
# ===========================================================================


_REMINDER_PLUGIN = """
    noeta_plugin_name = "nudger"
    from noeta.sdk import PluginBuilder

    plugin = PluginBuilder("nudger")

    @plugin.reminder(name="house-style", priority=400)
    def house_style(view):
        return "HOUSE STYLE: prefer pure functions."
"""


def test_activated_reminder_renders_into_the_composed_request(tmp_path: Path) -> None:
    """A plugin ``reminder`` is registered on the activating agent's composer.

    The whole point of track B: the render runs at the tail of the dynamic suffix
    and its text ships in the request. Before the wiring existed the plugin loaded
    clean, listed its contribution, and rendered nothing — ever.
    """
    plugins = load_plugins(
        builtins=False, modules=[_write_plugin(tmp_path, "nudger", _REMINDER_PLUGIN)]
    )
    provider = FakeLLMProvider(responses=[_end_turn()])
    client = _client(tmp_path, _bare(plugins=("nudger",)), provider, plugins)
    try:
        client.start(goal="hello")
    finally:
        client.shutdown()

    assert "HOUSE STYLE: prefer pure functions." in _request_text(provider)


def test_a_reminder_does_not_leak_to_an_agent_that_did_not_activate_it(
    tmp_path: Path,
) -> None:
    """Per-agent activation scoping (D6) — the same set, an agent that opted out."""
    plugins = load_plugins(
        builtins=False, modules=[_write_plugin(tmp_path, "nudger", _REMINDER_PLUGIN)]
    )
    provider = FakeLLMProvider(responses=[_end_turn()])
    client = _client(tmp_path, _bare(plugins=()), provider, plugins)
    try:
        client.start(goal="hello")
    finally:
        client.shutdown()

    assert "HOUSE STYLE" not in _request_text(provider)


# ===========================================================================
# Track A — a recorded ``reminder_provider`` reaches the intake seam (D7)
# ===========================================================================


_PROVIDER_PLUGIN = """
    noeta_plugin_name = "recaller"
    from collections import namedtuple

    from noeta.sdk import PluginBuilder

    Recalled = namedtuple("Recalled", ["text", "origin"])

    #: Every RecallView text this provider was invoked with (assertion hook: the
    #: provider must run exactly once per live intake and never on replay).
    CALLS = []

    plugin = PluginBuilder("recaller")

    @plugin.reminder_provider(name="rag", seams=["turn_intake"])
    def rag(view):
        CALLS.append(view.text)
        return [Recalled(text="RECALLED: the deploy gate is staging-first.",
                         origin="system")]
"""


def test_activated_reminder_provider_records_at_turn_intake(tmp_path: Path) -> None:
    """The provider runs once at the live intake and its reminder is recorded.

    "Recorded" is the load-bearing half (D7): the text has to be in the ledger, so
    it composes back on every later turn without the provider being consulted
    again. Asserting it only in the request would not tell the two apart.
    """
    plugins = load_plugins(
        builtins=False, modules=[_write_plugin(tmp_path, "recaller", _PROVIDER_PLUGIN)]
    )
    # The resolved function object is memoised on the PluginSet, so this is the
    # very object the Client wires — its module globals are the call counter.
    resolved = plugins.activation_reminder_providers()["recaller"][0][2]
    calls = resolved.__globals__["CALLS"]

    provider = FakeLLMProvider(responses=[_end_turn()])
    client = _client(tmp_path, _bare(plugins=("recaller",)), provider, plugins)
    try:
        outcome = client.start(goal="how do deploys work")
        recorded = "\n".join(
            str(getattr(item, "text", "")) for item in client.messages(outcome.task_id)
        )
    finally:
        client.shutdown()

    assert calls == ["how do deploys work"], calls
    assert "RECALLED: the deploy gate is staging-first." in recorded
    assert "RECALLED: the deploy gate is staging-first." in _request_text(provider)


def test_reminder_provider_is_scoped_to_the_activating_agent(tmp_path: Path) -> None:
    plugins = load_plugins(
        builtins=False, modules=[_write_plugin(tmp_path, "recaller", _PROVIDER_PLUGIN)]
    )
    resolved = plugins.activation_reminder_providers()["recaller"][0][2]
    calls = resolved.__globals__["CALLS"]

    provider = FakeLLMProvider(responses=[_end_turn()])
    client = _client(tmp_path, _bare(plugins=()), provider, plugins)
    try:
        client.start(goal="how do deploys work")
    finally:
        client.shutdown()

    assert calls == []


# ===========================================================================
# content_kind — an identity-plane surface that had no activation binding
# ===========================================================================


_CONTENT_KIND_PLUGIN = """
    noeta_plugin_name = "resident"
    from noeta.sdk import PluginBuilder
    from noeta.context.content_channel import ContentKindSpec

    plugin = PluginBuilder("resident")

    house_rules = ContentKindSpec(
        kind="house_rules",
        renderer=lambda names: [],
    )
    plugin.contribute("content_kind", house_rules, name="house_rules")
"""


def test_activated_content_kind_reaches_the_composer(tmp_path: Path) -> None:
    """``content_kind`` is an identity-plane surface with a wiring effect.

    It passed validation and merge but ``identity_activations()`` had no branch
    for it, so the value was dropped on the floor between resolve and compile.
    """
    plugins = load_plugins(
        builtins=False,
        modules=[_write_plugin(tmp_path, "resident", _CONTENT_KIND_PLUGIN)],
    )
    activation = plugins.identity_activations()["resident"]
    assert [k.kind for k in activation.content_kinds] == ["house_rules"]


def test_an_unhandled_identity_surface_fails_loudly(tmp_path: Path) -> None:
    """The fallthrough that hid ``content_kind`` is now an error, not a shrug."""
    from noeta.client.surfaces import SurfaceSpec, standard_registry

    registry = standard_registry()
    registry.register(
        SurfaceSpec("gizmo", "identity", "per-agent", lambda v: None, "name", "append")
    )
    body = """
        noeta_plugin_name = "gizmos"
        from noeta.sdk import PluginBuilder

        plugin = PluginBuilder("gizmos")
        plugin.contribute("gizmo", "anything", name="g")
    """
    plugins = load_plugins(
        builtins=False,
        modules=[_write_plugin(tmp_path, "gizmos", body)],
        registry=registry,
    )
    with pytest.raises(PluginError, match="has no activation binding"):
        plugins.identity_activations()


# ===========================================================================
# Collisions — the loader's "no override" rule reaches the real wiring path
# ===========================================================================


_TOOL_A = """
    noeta_plugin_name = "pack-a"
    from noeta.sdk import PluginBuilder, ToolContext, ToolResult, tool

    plugin = PluginBuilder("pack-a")

    @tool(name="deploy", version="1", risk_level="low",
          input_schema={"type": "object", "additionalProperties": True})
    def deploy(arguments, ctx):
        return ToolResult(success=True, summary="a")

    plugin.tool(deploy, name="deploy")
"""

_TOOL_B = _TOOL_A.replace("pack-a", "pack-b").replace('summary="a"', 'summary="b"')


def test_two_plugins_contributing_one_tool_name_fail_at_client_build(
    tmp_path: Path,
) -> None:
    """``PluginSet.merged()`` runs on the real path, naming both plugins.

    The loader always implemented this; nothing on the ``Client`` path called it,
    so the second contribution was silently dropped by the tool de-dup instead.
    """
    plugins = load_plugins(
        builtins=False,
        modules=[
            _write_plugin(tmp_path, "pack_a", _TOOL_A),
            _write_plugin(tmp_path, "pack_b", _TOOL_B),
        ],
    )
    with pytest.raises(PluginError, match="pack-a.*pack-b|pack-b.*pack-a"):
        _client(tmp_path, _bare(plugins=("pack-a", "pack-b")), FakeLLMProvider(), plugins)


def test_a_plugin_tool_shadowing_a_builtin_is_refused() -> None:
    """A plugin tool named like a built-in would silently vanish in the de-dup."""
    activation = {"shady": PluginActivation(tools=("read",))}
    with pytest.raises(ValueError, match="no override"):
        compile_options(_bare(plugins=("shady",)), plugins=activation)


def test_a_plugin_tool_in_disallowed_tools_is_refused() -> None:
    """Activation and subtraction contradict — pick one, do not guess."""
    activation = {"shady": PluginActivation(tools=("read",))}
    with pytest.raises(ValueError, match="disallowed_tools"):
        compile_options(
            _bare(plugins=("shady",), allowed_tools=(), disallowed_tools=("read",)),
            plugins=activation,
        )


def test_a_plugin_child_agent_cannot_shadow_a_declared_one() -> None:
    """Silent override here re-points every ``spawn_subagent`` at the plugin."""
    mine = AgentDefinition(description="mine", prompt="p")
    theirs = AgentDefinition(description="theirs", prompt="q")
    activation = {"shady": PluginActivation(agents=(("explore", theirs),))}
    with pytest.raises(ValueError, match="no override"):
        compile_options(
            _bare(plugins=("shady",), agents={"explore": mine}), plugins=activation
        )


def test_a_plugin_child_agent_without_a_clash_is_compiled() -> None:
    theirs = AgentDefinition(description="theirs", prompt="q")
    activation = {"helpful": PluginActivation(agents=(("scout", theirs),))}
    main, descendants = compile_options(
        _bare(plugins=("helpful",)), plugins=activation
    )
    assert [d.name for d in descendants] == ["scout"]
    assert main.capabilities.spawnable == ("scout",)


# ===========================================================================
# Activation scoping of *execution* — loading is not running (D5)
# ===========================================================================


def _package_plugin(root: Path, name: str, *, sentinel: Path) -> str:
    """A **package-form** plugin: static manifest + a module resolution imports.

    The single-file form caches its decorated objects while reading the manifest,
    so its refs are never imported a second time — it cannot show whether
    resolution ran. The package form can: the module body only executes when a
    ``ref`` is resolved, and it drops ``sentinel`` when it does.
    """
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        f"from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('imported', encoding='utf-8')\n"
        f"def noop(view):\n    return None\n",
        encoding="utf-8",
    )
    (package / "noeta-plugin.toml").write_text(
        textwrap.dedent(
            f"""
            name = "{name}"

            [[contributions]]
            surface = "reminder"
            name = "noop"
            ref = "{name}:noop"
            priority = 900
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return str(package / "noeta-plugin.toml")


def test_an_unactivated_plugins_refs_are_never_resolved(
    tmp_path: Path, monkeypatch
) -> None:
    """Activation gates the code, not just the effect (D5).

    A ``Client`` build used to resolve every loaded external plugin, so ten
    workspace plugins meant ten module bodies running even when the agent
    activated one — and a module body is where a real plugin opens its network
    client or reads its credentials.
    """
    sentinel = tmp_path / "IMPORTED"
    manifest = _package_plugin(tmp_path, "unactivated_pkg", sentinel=sentinel)
    monkeypatch.syspath_prepend(str(tmp_path))

    plugins = load_plugins(builtins=False, modules=[manifest])
    assert not sentinel.exists(), "loading must not import (D1)"

    provider = FakeLLMProvider(responses=[_end_turn()])
    client = _client(tmp_path, _bare(plugins=()), provider, plugins)
    try:
        client.start(goal="hello")
    finally:
        client.shutdown()

    assert not sentinel.exists(), "an unactivated plugin was resolved anyway"


def test_an_activated_plugin_is_resolved(tmp_path: Path, monkeypatch) -> None:
    """The other half of the gate: activation still gets you the contribution."""
    sentinel = tmp_path / "IMPORTED"
    manifest = _package_plugin(tmp_path, "activated_pkg", sentinel=sentinel)
    monkeypatch.syspath_prepend(str(tmp_path))

    plugins = load_plugins(builtins=False, modules=[manifest])
    provider = FakeLLMProvider(responses=[_end_turn()])
    client = _client(tmp_path, _bare(plugins=("activated_pkg",)), provider, plugins)
    try:
        client.start(goal="hello")
    finally:
        client.shutdown()

    assert sentinel.exists()


def test_resolution_is_memoised_per_plugin(tmp_path: Path) -> None:
    """One resolve per plugin per set, however many projections ask for it.

    Measured by validator invocations rather than imports: Python caches modules,
    so re-resolution shows up as repeated validation + attribute walking, which is
    precisely what ``Client``'s three-projections-per-build was paying for.
    """
    from noeta.client.surfaces import SurfaceSpec, standard_registry

    validated: list[Any] = []
    registry = standard_registry()
    registry.register(
        SurfaceSpec(
            "counted", "wiring", "process", validated.append, "none", "append"
        )
    )
    # A guard (governance pass) + a reminder (activation pass) + the counted
    # surface, so more than one projection has a reason to resolve this plugin —
    # the situation the memo exists for.
    body = """
        noeta_plugin_name = "counter"
        from noeta.sdk import PluginBuilder

        plugin = PluginBuilder("counter")
        plugin.contribute("counted", "value", name="c")
        plugin.guard(object(), name="g")

        @plugin.reminder(name="r", priority=1)
        def r(view):
            return None
    """
    plugins = load_plugins(
        builtins=False,
        modules=[_write_plugin(tmp_path, "counter", body)],
        registry=registry,
    )
    for _ in range(3):
        plugins.identity_activations()
        plugins.activation_transforms()
        plugins.activation_reminders()
        plugins.process_hooks()
    assert validated == ["value"]


# ===========================================================================
# Ref spelling — the dotted form the name-derivation already accepts
# ===========================================================================


def test_a_dotted_ref_resolves_like_the_colon_form(tmp_path: Path, monkeypatch) -> None:
    """``pkg.mod.attr`` is a documented ref spelling; only ``mod:attr`` worked.

    ``_derive_name`` and ``plugin_check`` both normalise the dotted form, so a
    manifest using it passed the packaging check and then failed at resolve.
    """
    package = tmp_path / "dotted_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "stages.py").write_text(
        "def scrub(result):\n    return result\n", encoding="utf-8"
    )
    (package / "noeta-plugin.toml").write_text(
        textwrap.dedent(
            """
            name = "dotted"

            [[contributions]]
            surface = "tool_result_transform"
            name = "scrub"
            ref = "dotted_pkg.stages.scrub"
            priority = 10
            """
        ).lstrip(),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    plugins = load_plugins(
        builtins=False, modules=[str(package / "noeta-plugin.toml")]
    )
    (priority, name, fn) = plugins.activation_transforms()["dotted"][0]
    assert (priority, name) == (10, "scrub")
    assert fn.__name__ == "scrub"


def test_an_import_fault_inside_a_real_module_is_not_reinterpreted(
    tmp_path: Path, monkeypatch
) -> None:
    """Backing off must not swallow a genuine import error.

    The dotted form tries shorter prefixes when one is not a module. A module
    that *is* importable but raises ``ModuleNotFoundError`` for its own missing
    dependency must surface that, not be silently retried as an attribute.
    """
    package = tmp_path / "broken_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "stages.py").write_text(
        "import definitely_not_installed_xyz\n", encoding="utf-8"
    )
    (package / "noeta-plugin.toml").write_text(
        textwrap.dedent(
            """
            name = "broken"

            [[contributions]]
            surface = "tool_result_transform"
            name = "scrub"
            ref = "broken_pkg.stages.scrub"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    plugins = load_plugins(builtins=False, modules=[str(package / "noeta-plugin.toml")])
    with pytest.raises(PluginError, match="definitely_not_installed_xyz"):
        plugins.activation_transforms()


# ===========================================================================
# tool_result_transform — the runtime must not drop the stages
# ===========================================================================


def _echo_tool() -> Any:
    class _Echo:
        name = "echo"
        risk_level = "low"
        input_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}

        def invoke(self, arguments: dict[str, Any], ctx: Any) -> Any:
            from noeta.protocols.tool import ToolResult

            return ToolResult(success=True, summary="plain")

    return _Echo()


def test_transforms_are_honoured_even_with_no_compiled_tools() -> None:
    """``tools`` being empty is not a reason to drop a redaction stage."""
    log = InMemoryEventLog()
    engine = Engine(
        event_log=log,
        content_store=InMemoryContentStore(),
        composer=PassthroughComposer(),
        tools={},
        tool_result_transforms=(lambda r: dataclasses.replace(r, summary="X"),),
    )
    assert engine._tool_runtime is not None


def test_transforms_with_an_injected_runtime_are_refused() -> None:
    """Silently ignoring them turns an activated redaction plugin into a no-op."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    with pytest.raises(ValueError, match="injected tool_runtime"):
        Engine(
            event_log=log,
            content_store=store,
            composer=PassthroughComposer(),
            tools={"echo": _echo_tool()},
            tool_runtime=ToolRuntime(event_log=log, content_store=store),
            tool_result_transforms=(lambda r: r,),
        )


# ===========================================================================
# Capability vocabulary + delegation authoring
# ===========================================================================


def test_every_builtin_plugin_is_an_activatable_name() -> None:
    """The two hardcoded lists cannot drift apart unnoticed."""
    assert_activation_vocabulary()
    assert BUILTIN_PLUGIN_NAMES <= BUILTIN_ACTIVATIONS


def test_a_child_agent_can_be_granted_delegation() -> None:
    """The successor to ``AgentDefinition(capabilities=Capabilities(delegation=True))``.

    Removing ``Capabilities`` left delegation derivable only from an inline child
    roster, which a flat child never has — so no child could delegate at all.
    """
    child = AgentDefinition(
        description="a lead", prompt="p", plugins=("delegation",)
    )
    _main, descendants = compile_options(_bare(agents={"lead": child}))
    assert descendants[0].capabilities.delegation is True


def test_activating_delegation_on_a_childless_root_is_additive() -> None:
    main, _d = compile_options(_bare(plugins=("delegation",)))
    assert main.capabilities.delegation is True
    assert main.capabilities.spawnable == ()


# ===========================================================================
# Composer — the already_spawned scan is gated on delegation
# ===========================================================================


def test_already_spawned_scan_is_skipped_for_a_non_delegating_agent() -> None:
    """The scan feeds one reminder that renders nothing without delegation.

    Walking the whole rolling history to compute a value the reminder discards is
    per-compose waste on every leaf sub-agent.
    """
    from noeta.context.composer import ThreeSegmentComposer

    class _ExplodingMessages(list):
        def __iter__(self):  # noqa: D105
            raise AssertionError("history was scanned despite delegation being off")

    composer = ThreeSegmentComposer(
        system_prompt="p", tools={}, content_store=InMemoryContentStore()
    )

    class _Runtime:
        messages = _ExplodingMessages()

    class _Task:
        runtime = _Runtime()
        state = type("S", (), {"todos": ()})()
        context = type("C", (), {"compaction_thrashing": False})()

    assert composer._append_reminders(
        _Task(), [], delegation_enabled=False
    ) == []


# ===========================================================================
# Loader knobs — builtins=False scopes the loaded set, never the SDK defaults
# ===========================================================================


def test_builtinless_set_keeps_the_default_capability_surface(tmp_path: Path) -> None:
    """``builtins=False`` scopes the *loaded set*, not the SDK's own defaults.

    Microkernel acceptance 9 as amended in M4: the loader knob governs which
    plugins the session can list / resolve (the external audit surface); a bare
    ``Options()`` still obtains its default fs/web roster through the
    ``noeta.client.parts`` doorway, which reads the built-in catalogue directly.
    The reference host (``examples/reference-host``) documents and relies on
    exactly this — its example plugins load with ``builtins=False`` while the
    preset recipe keeps noeta's own capabilities.
    """
    baseline = _client(tmp_path, _bare(), FakeLLMProvider(responses=[_end_turn()]))
    try:
        golden = dataclasses.asdict(baseline.registry.resolve("main"))
    finally:
        baseline.shutdown()

    scoped = _client(
        tmp_path,
        _bare(),
        FakeLLMProvider(responses=[_end_turn()]),
        load_plugins(builtins=False),
    )
    try:
        assert dataclasses.asdict(scoped.registry.resolve("main")) == golden
    finally:
        scoped.shutdown()


def _skill_roster(tmp_path: Path, disabled: Sequence[str]) -> frozenset[str]:
    """The composed tool roster of a one-skill workspace under ``disabled``."""
    skill_dir = tmp_path / "ws" / ".noeta" / "skills" / "greet"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: greet\ndescription: say hi\n---\n\nSay hi.\n", encoding="utf-8"
    )
    provider = FakeLLMProvider(responses=[_end_turn()])
    client = _client(
        tmp_path,
        _bare(plugins=("skill_invocation",)),
        provider,
        load_plugins(builtins=True, disabled_builtins=disabled),
    )
    try:
        client.start(goal="hello")
    finally:
        client.shutdown()
    return _request_tool_names(provider)


def test_disabling_the_skills_builtin_actually_removes_the_capability(
    tmp_path: Path,
) -> None:
    """``disabled_builtins=["skills"]`` reaches the engine, not just the listing.

    The ``skills`` built-in contributes nothing per-agent (the manifest is
    contribution-free), so before this the name was dropped from the catalogue
    while the host injected ``default_skills_kit_factory()`` regardless — skills
    stayed indexed and the ``skill`` control tool stayed on the wire. The
    disable now travels: ``PluginSet`` records it, the host withholds the
    factory, and the kernel's skills stage no-ops.

    Asserted as a roster DIFFERENCE so the test pins the one intended effect —
    everything else about the session is untouched.
    """
    on = _skill_roster(tmp_path, ())
    off = _skill_roster(tmp_path, ("skills",))
    assert "skill" in on
    assert on - off == {"skill"}
    assert off - on == frozenset()


def test_a_builtinless_set_does_not_disable_skills(tmp_path: Path) -> None:
    """Absence is not a disable — only an explicit ``disabled_builtins`` is.

    ``builtins=False`` is the ordinary way to load just one's own plugin (every
    ``examples/`` test does it), and it drops every built-in NAME. Reading the
    host switch off membership would silently strip skills from those sessions;
    the switch reads the recorded disable instead. Companion to
    :func:`test_builtinless_set_keeps_the_default_capability_surface`, one layer
    down: that one pins the compiled spec, this one pins the built engine.
    """
    (tmp_path / "ws").mkdir(exist_ok=True)
    scoped = _client(
        tmp_path, _bare(), FakeLLMProvider(responses=[_end_turn()]),
        load_plugins(builtins=False),
    )
    try:
        assert scoped._host.skills_enabled is True
    finally:
        scoped.shutdown()


def test_the_react_builtin_refuses_to_be_disabled() -> None:
    """The default policy is replaceable (D10 ``policy`` surface), not removable.

    Every compiled ``AgentSpec`` pins ``POLICY_REF ("react", "1")``, so there is
    no identity for a policy-less agent and no parity to resume. Silently
    accepting the disable — dropping the catalogue entry while the host wired
    ``ReActPolicy`` anyway — was the dishonest half; refusing is the honest one.
    """
    with pytest.raises(PluginError, match="'react' cannot be disabled"):
        load_plugins(builtins=True, disabled_builtins=["react"])


# ===========================================================================
# Reference host — the plugin env must not outlive the build
# ===========================================================================


def test_plugin_env_scope_restores_the_previous_environment(
    tmp_path: Path, monkeypatch
) -> None:
    """A per-build workspace path left in ``os.environ`` outlives its directory.

    The next build in the same process then points ``protected-paths`` at a
    deleted root — an order-dependent failure with no connection to its cause.
    """
    import importlib.util
    import os

    host_path = (
        Path(__file__).resolve().parents[1] / "examples" / "reference-host" / "host.py"
    )
    import sys

    spec = importlib.util.spec_from_file_location("_ref_host_env", host_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the host defines a ``@dataclass`` under
    # ``from __future__ import annotations``, whose ClassVar resolution looks the
    # module up in ``sys.modules`` at class-creation time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    monkeypatch.setenv("NOETA_PROTECTED_PATHS_ROOTS", "/original")
    monkeypatch.delenv("NOETA_GIT_CHECKPOINT_REPO", raising=False)

    with module.plugin_env_scope(tmp_path) as env:
        assert os.environ["NOETA_PROTECTED_PATHS_ROOTS"] == str(tmp_path)
        assert env["NOETA_GIT_CHECKPOINT_REPO"] == str(tmp_path)

    assert os.environ["NOETA_PROTECTED_PATHS_ROOTS"] == "/original"
    assert "NOETA_GIT_CHECKPOINT_REPO" not in os.environ
