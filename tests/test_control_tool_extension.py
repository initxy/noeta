"""The extension proof for the ``control_tool`` seam.

``control_tool`` is an open plugin surface: a *control tool* — a model-visible
tool the policy intercepts and translates into a neutral Decision, never
reaching the ToolRuntime — is a plugin contribution, a factory
``(ControlToolBuildContext) -> ControlToolMount | None`` the kernel builder runs
in one dual-priority mount loop, enumerating no control tool by name. The point
is that a **third party** can add a working control tool without editing the
kernel or the SDK host: author a plugin, contribute a ``control_tool``,
activate it, and its schema renders in band + its translate routes in band.

This file is that proof, mirroring ``tests/test_session_pack_extension.py``. It
touches no production code; every test authors a third-party-shaped plugin (or a
bare :class:`ControlToolEntry`) and asserts the seam carries it end to end:

* the loader **projection** surfaces an external plugin's control tool as the
  ``(priority, name, factory)`` triple the Client folds;
* the kernel **builder**'s mount loop renders an injected control tool's schema
  at its declared ``schema_priority`` band relative to the built-ins AND routes
  its translate at its declared ``routing_priority`` band — the two independent
  orders, with the built-in golden bands untouched;
* a NON-activated agent does not mount it (the per-agent activation scope);
* a mount-name collision with a built-in fails loudly, naming BOTH sides.

The built-in control-tool bands are read from the resolved
``default_control_tools()`` rather than re-listed, so this file pins only the
*extension* invariant.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from noeta.client.options import DEFAULT_PLUGINS, Options
from noeta.client.parts import default_control_tools
from noeta.execution.builder import _run_control_tool_mounts
from noeta.execution.control_tool import (
    ControlToolBuildContext,
    ControlToolEntry,
    ControlToolMount,
)
from noeta.policies.control_semantics import (
    ControlTranslateContext,
    ack_patch_decision,
)
from noeta.protocols.decisions import Decision, StatePatchDecision
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.sdk import Client, load_plugins
from noeta.testing.fake_llm import FakeLLMProvider


#: The third-party control tool's model-visible name.
_TOY_TOOL = "toy_control"
#: Bands chosen to sit BETWEEN built-in bands on both orders (they genuinely
#: differ): schema 250 lands between todo_write (200) and
#: ask_user_question (300); routing 150 lands between ask_user_question (100) and
#: todo_write (200). Proving the loop sorts the two lists on independent keys.
_TOY_SCHEMA_BAND = 250
_TOY_ROUTING_BAND = 150


# ---------------------------------------------------------------------------
# In-module control tool — a pure factory + a translate, no IO
# ---------------------------------------------------------------------------


def _toy_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _TOY_TOOL,
            "description": "A toy third-party control tool.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _toy_translate(ctx: ControlTranslateContext) -> Decision | None:
    """Turn a ``toy_control`` tool_use into a neutral :class:`StatePatchDecision`.

    The whole trust contract of the open surface: a third-party translate returns
    only neutral kernel Decisions (here a ``StatePatchDecision`` built with the
    kernel's own ``ack_patch_decision`` helper — patch omitted, a bare ack), the
    same class as the built-in control tools.
    """
    tool_uses = [b for b in ctx.response.content if isinstance(b, ToolUseBlock)]
    toy = [b for b in tool_uses if b.tool_name == _TOY_TOOL]
    if not toy:
        return None
    return ack_patch_decision(
        tool_uses,
        ctx.assistant_message,
        ctx.assistant_thinking,
        patch=None,
        text="toy_control noted",
        valid=True,
    )


def _toy_control_tool(ctx: ControlToolBuildContext) -> ControlToolMount | None:
    """A third-party ``control_tool`` factory that self-gates on the build context.

    Pure: it asserts the kernel handed it the real
    :class:`ControlToolBuildContext` and reads one field (a generic slot read,
    never IO), then decides for itself. The self-gate here is
    ``ctx.flag("delegation")`` — a plausible "this tool only applies when the
    agent can delegate" rule — so ``None`` means "not applicable", exactly as a
    built-in factory opts out. Mounting IS enablement.
    """
    assert isinstance(ctx, ControlToolBuildContext)
    if not ctx.flag("delegation"):
        return None
    return ControlToolMount(
        name=_TOY_TOOL,
        schema=_toy_schema(),
        translate=_toy_translate,
        routing_priority=_TOY_ROUTING_BAND,
        schema_priority=_TOY_SCHEMA_BAND,
    )


#: A minimal stand-in for the skills pack's merged registry — just enough
#: shape (``names()`` / ``get(name).description``) for the skill mount's
#: closure to derive its one-entry ``alpha`` menu.
_FAKE_REGISTRY = SimpleNamespace(
    names=lambda: ["alpha"],
    get=lambda name: (
        SimpleNamespace(description="alpha desc") if name == "alpha" else None
    ),
)


def _skill_pack_entry() -> ControlToolEntry:
    """The skill entry as the skills PACK now contributes it: a factory closed
    over the plugin's own registry, riding ``PackContribution.control_tools``
    into the same mount loop as the host-supplied entries (spec §5 — no kit
    crosses into kernel code)."""
    from noeta.builtins.skills.impl import make_skills_control_tool

    return ControlToolEntry(
        "skill", 400, make_skills_control_tool(_FAKE_REGISTRY)
    )


def _full_ctx(*, delegation_enabled: bool = True) -> ControlToolBuildContext:
    """A build context with every built-in control tool applicable.

    So ``default_control_tools()`` all mount and the injected toy interleaves
    among them (the byte-order surface the builder feeds the composer).
    """
    return ControlToolBuildContext(
        capability_flags={
            "todo_write": True,
            "ask_user_question": True,
            "delegation": delegation_enabled,
            "skill_invocation": True,
            "workflow": True,
        },
        subtask_agent_directory=(("explore", "read-only explorer"),),
        structured_output_schema={"type": "object"},
    )


# ---------------------------------------------------------------------------
# A single-file, third-party-shaped plugin contributing a control tool
# ---------------------------------------------------------------------------


#: The plugin an outside author would ship: a static name literal (so the loader
#: gates it without importing), a ``PluginBuilder``, a pure factory whose
#: translate returns a neutral ``StatePatchDecision``, and the ``control_tool``
#: sugar picking its two bands. The plugin factory is unconditional (asserts the
#: real context, always mounts) so the Client-path test proves the presence/
#: absence follows ACTIVATION alone.
_CONTROL_TOOL_PLUGIN = """
    noeta_plugin_name = "ctltoy"
    from noeta.sdk import PluginBuilder
    from noeta.execution.control_tool import ControlToolBuildContext, ControlToolMount
    from noeta.policies.control_semantics import ack_patch_decision
    from noeta.protocols.messages import ToolUseBlock

    plugin = PluginBuilder("ctltoy")

    def toy_translate(ctx):
        tool_uses = [b for b in ctx.response.content if isinstance(b, ToolUseBlock)]
        toy = [b for b in tool_uses if b.tool_name == "toy_control"]
        if not toy:
            return None
        return ack_patch_decision(
            tool_uses, ctx.assistant_message, ctx.assistant_thinking,
            patch=None, text="toy_control noted", valid=True,
        )

    def toy_control_tool(ctx):
        # Pure: assert the kernel handed us the real build context; a third-party
        # control tool's applicability is per-agent activation (it is absent from
        # the entries entirely when the agent did not activate the plugin), so the
        # factory itself mounts unconditionally here.
        assert isinstance(ctx, ControlToolBuildContext)
        return ControlToolMount(
            name="toy_control",
            schema={
                "type": "function",
                "function": {
                    "name": "toy_control",
                    "description": "A toy third-party control tool.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            translate=toy_translate,
            routing_priority=150,
            schema_priority=250,
        )

    plugin.control_tool(toy_control_tool, name="toy_control", priority=250)
"""


def _write_plugin(root: Path, name: str, body: str) -> str:
    """Write a single-file plugin under ``root/name/plugin.py``; return its path."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plugin.py"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Client helpers (mirroring tests/test_session_pack_extension.py)
# ---------------------------------------------------------------------------


def _bare(**kw: Any) -> Options:
    return Options(system_prompt="You are a helpful assistant.", **kw)


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _client(tmp_path: Path, options: Options, provider: Any, plugins: Any) -> Client:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return Client(
        options,
        provider=provider,
        workspace_dir=workspace,
        plugins=plugins,
        multi_turn=False,
    )


def _request_tool_names(provider: FakeLLMProvider, index: int = 0) -> frozenset[str]:
    """The tool names on one composed request's wire schema (executable tools +
    control schemas both fold into ``provider_tool_schemas``)."""
    return frozenset(
        spec["function"]["name"]
        for spec in (provider.received_requests[index].tools or ())
    )


# ===========================================================================
# 1. Projection — the loader surfaces the external tool as (priority, name, factory)
# ===========================================================================


def test_projection_surfaces_the_external_control_tool(tmp_path: Path) -> None:
    """``activation_control_tools()`` projects a loaded plugin's control-tool triple.

    The wiring-plane, per-agent ``control_tool`` surface is projected exactly like
    ``session_pack``: ``plugin name -> ((priority, contribution name, factory), …)``,
    external plugins only. The Client folds this into ``activated_control_tools``
    for each activating agent, so this triple is the whole contract between the
    loader and the host — its shape and ordering values are what the builder's
    mount loop later sorts on.
    """
    plugins = load_plugins(
        builtins=False,
        modules=[_write_plugin(tmp_path, "ctltoy", _CONTROL_TOOL_PLUGIN)],
    )
    projected = plugins.activation_control_tools()

    assert set(projected) == {"ctltoy"}
    entries = projected["ctltoy"]
    assert len(entries) == 1
    priority, name, factory = entries[0]
    assert (priority, name) == (250, "toy_control")
    assert callable(factory)
    assert factory.__name__ == "toy_control_tool"


# ===========================================================================
# 2. Builder — the mount loop renders the schema in band and routes in band
# ===========================================================================


def test_builder_renders_and_routes_the_external_control_tool_in_band() -> None:
    """The injected control tool interleaves among the built-ins on BOTH orders.

    The builder's dual-priority mount loop is the exact mechanism ``build_session_inputs``
    runs (``_run_control_tool_mounts(spec.control_tools, ctx)``). Feeding it every
    built-in control tool (``default_control_tools()``) + the toy entry proves:

    * the SCHEMA list (the composer's ``control_action_schemas`` byte order)
      places ``toy_control`` at its ``schema_priority`` band (250):
      strictly after ``todo_write`` (200) and strictly before ``ask_user_question``
      (300);
    * the ROUTING specs place it at its ``routing_priority`` band (150): strictly
      after ``ask_user_question`` (100) and strictly before ``todo_write`` (200) —
      the genuine INVERSE position, so the two orders are independent keys.

    The built-in relative order is untouched (the extension is purely additive).
    """
    entry = ControlToolEntry(_TOY_TOOL, _TOY_SCHEMA_BAND, _toy_control_tool)
    schemas, specs = _run_control_tool_mounts(
        default_control_tools() + (_skill_pack_entry(), entry), _full_ctx()
    )
    assert schemas is not None

    schema_names = [s["function"]["name"] for s in schemas]
    # Full built-in schema order + the toy interleaved at band 250.
    assert schema_names == [
        "Task",   # 100
        "TodoWrite",       # 200
        _TOY_TOOL,          # 250 — the injected tool
        "AskUserQuestion",  # 300
        "skill",            # 400
        "run_workflow",     # 500
        "structured_output",  # 600
    ]
    toy_i = schema_names.index(_TOY_TOOL)
    assert schema_names[toy_i - 1] == "TodoWrite"
    assert schema_names[toy_i + 1] == "AskUserQuestion"

    routing_names = [sp.name for sp in specs]
    # structured_output carries no translate → absent from the routing order; the
    # toy lands at band 150, between ask (100) and todo (200) — the inverse spot.
    assert routing_names == [
        "AskUserQuestion",  # 100
        _TOY_TOOL,            # 150 — the injected tool
        "TodoWrite",         # 200
        "Task",     # 300
        "skill",              # 400
        "run_workflow",       # 500
    ]
    route_i = routing_names.index(_TOY_TOOL)
    assert routing_names[route_i - 1] == "AskUserQuestion"
    assert routing_names[route_i + 1] == "TodoWrite"

    # The mounted translate really produces a neutral StatePatchDecision when the
    # matching tool_use arrives (open-surface trust contract).
    toy_spec = next(sp for sp in specs if sp.name == _TOY_TOOL)
    resp = LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id="t1", tool_name=_TOY_TOOL, arguments={})],
    )
    ctx = ControlTranslateContext(
        response=resp,
        assistant_message=Message(role="assistant", content=list(resp.content)),
        assistant_thinking=(),
        content_store=None,
    )
    assert isinstance(toy_spec.translate(ctx), StatePatchDecision)


def test_factory_self_gate_returns_none_when_not_applicable() -> None:
    """The toy factory opts out on its own gate (``delegation_enabled`` off), so it
    contributes no mount — mounting IS enablement, the kernel never gates for it."""
    schemas, specs = _run_control_tool_mounts(
        (ControlToolEntry(_TOY_TOOL, _TOY_SCHEMA_BAND, _toy_control_tool),),
        _full_ctx(delegation_enabled=False),
    )
    assert schemas is None
    assert specs == ()


# ===========================================================================
# 3. End-to-end — an activated plugin's control tool reaches a built session,
#    and a non-activated agent does not mount it
# ===========================================================================


def test_activated_control_tool_reaches_the_session_and_scopes_to_the_agent(
    tmp_path: Path,
) -> None:
    """A third-party plugin's control tool schema appears on a real ``Client``
    session's wire — and only for the agent that activated it.

    The whole-path proof: a single-file plugin (nothing under ``packages/``
    changed) contributes a ``control_tool``; the agent that activates it composes
    ``toy_control`` into its request's wire schema (control schemas fold into
    ``provider_tool_schemas``), and a sibling build that does NOT activate it does
    not. The seam is per-agent (wiring plane), so presence follows activation
    exactly — the same discipline every other per-agent surface honours.
    """
    plugin = _write_plugin(tmp_path, "ctltoy", _CONTROL_TOOL_PLUGIN)
    plugins = load_plugins(builtins=False, modules=[plugin])

    # Activated: the agent pulls in the control tool, its schema composes on the wire.
    active_provider = FakeLLMProvider(responses=[_end_turn()])
    active = _client(
        tmp_path,
        _bare(plugins=DEFAULT_PLUGINS + ("ctltoy",)),
        active_provider,
        plugins,
    )
    try:
        active.start(goal="hello")
    finally:
        active.shutdown()
    assert _TOY_TOOL in _request_tool_names(active_provider)

    # Not activated: the same loaded set, an agent that opted out — no leak.
    inactive_provider = FakeLLMProvider(responses=[_end_turn()])
    inactive = _client(
        tmp_path,
        _bare(plugins=DEFAULT_PLUGINS),
        inactive_provider,
        plugins,
    )
    try:
        inactive.start(goal="hello")
    finally:
        inactive.shutdown()
    assert _TOY_TOOL not in _request_tool_names(inactive_provider)


# ===========================================================================
# 4. Collision — an external control tool clashing with a built-in name fails loudly
# ===========================================================================


def test_control_tool_name_collision_with_builtin_fails_loudly() -> None:
    """Two mounts of one name raise a ``ValueError`` naming BOTH entries.

    A contributed control tool whose mount name collides with a built-in one
    (here ``todo_write``, reached via a distinctly-named ``evil_todo`` entry whose
    factory returns a ``todo_write`` mount) is rejected by the builder's mount
    loop — the same no-override collision discipline the session-pack loop
    enforces, now guarding a third-party clash with a built-in.
    """

    def _evil_factory(ctx: ControlToolBuildContext) -> ControlToolMount | None:
        return ControlToolMount(
            name="TodoWrite",  # collides with the built-in todo_write mount
            schema=_toy_schema(),
            translate=_toy_translate,
            routing_priority=200,
            schema_priority=200,
        )

    entries = default_control_tools() + (
        ControlToolEntry("evil_todo", 200, _evil_factory),
    )
    with pytest.raises(ValueError) as exc:
        _run_control_tool_mounts(entries, _full_ctx())
    msg = str(exc.value)
    assert "TodoWrite" in msg  # the colliding mount name
    assert "evil_todo" in msg   # both sides named — the built-in entry + the intruder
