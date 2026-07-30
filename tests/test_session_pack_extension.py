"""The extension proof for the session-pack seam (microkernel phase 3, S5).

Phase 3 landed ``session_pack`` as the 15th standard surface — the single
generic seam through which a *capability* is constructed into a session (a
factory ``(SessionBuildContext) -> PackContribution`` the kernel builder runs in
one priority-ordered loop, enumerating no capability by name). The whole point
of that seam is that a **third party** can add a capability without editing the
kernel or the SDK host: author a single-file plugin, contribute a
``session_pack``, activate it, and its tools appear in a built session.

This file is that proof. It touches no production code; every test authors a
third-party-shaped plugin (or a bare :class:`SessionPackEntry`) and asserts the
seam carries it end to end:

* the loader **projection** surfaces an external plugin's pack as the
  ``(priority, name, factory)`` triple the Client folds (S5 unit);
* the kernel **builder** runs an injected pack in its generic loop — appending
  past every built-in tool at a high band, and *interleaving* between built-in
  bands at a low one — with the built-in golden order preserved (S5 builder);
* the full **Client** path assembles an activated plugin's pack tool into the
  session it builds, and scopes it to the agent that activated it (S5
  acceptance 5 — the zero-kernel-edit proof).

The factory a pack contributes is a pure function of its
:class:`~noeta.execution.session_pack.SessionBuildContext`: it reads a generic
context slot to prove it was handed the kernel-built context, and touches no
disk or network. The built-in tool order is imported from
``tests/test_session_pack_goldens.py`` rather than re-listed, so this file
pins only the *extension* invariant and never duplicates the golden's job.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from noeta.client.options import DEFAULT_PLUGINS, Options
from noeta.client.parts import default_session_packs
from noeta.client.plugin_set import load_plugins
from noeta.execution.session_pack import (
    PackContribution,
    SessionBuildContext,
    SessionPackEntry,
)
from noeta.presets import official_specs
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Client
from noeta.storage.memory import InMemoryContentStore
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool

from tests._session_inputs import build_code_replay_inputs
from tests.test_session_pack_goldens import GOLDEN_DEFAULT_TOOLS


# ---------------------------------------------------------------------------
# Pack factories — pure functions of the build context, no IO
# ---------------------------------------------------------------------------


def _echo_pack(ctx: SessionBuildContext) -> PackContribution:
    """Contribute one tool, reading a generic context slot to prove purity.

    A session-pack factory is handed the kernel-built
    :class:`SessionBuildContext`; asserting the type and reading
    ``workspace_dir`` (a slot, not IO) shows the seam passes the real context
    without the factory touching disk or network.
    """
    assert isinstance(ctx, SessionBuildContext)
    assert ctx.workspace_dir is not None  # a generic slot read — never IO
    return PackContribution(tools={"toy_echo": FakeTool(name="toy_echo")})


def _between_pack(ctx: SessionBuildContext) -> PackContribution:
    """Same shape as :func:`_echo_pack`, distinct tool name for the interleave."""
    assert isinstance(ctx, SessionBuildContext)
    assert ctx.workspace_dir is not None
    return PackContribution(tools={"toy_between": FakeTool(name="toy_between")})


# ---------------------------------------------------------------------------
# A single-file, third-party-shaped plugin contributing a session pack
# ---------------------------------------------------------------------------


#: The plugin an outside author would ship: a static name literal (so the loader
#: gates it without importing), a ``PluginBuilder``, a pure factory, and the
#: ``session_pack`` sugar picking a band (1100) above every built-in (100–1000).
_SESSION_PACK_PLUGIN = """
    noeta_plugin_name = "toybox"
    from noeta.sdk import PluginBuilder
    from noeta.execution.session_pack import PackContribution, SessionBuildContext
    from noeta.tools.fake import FakeTool

    plugin = PluginBuilder("toybox")

    def toy_echo_pack(ctx):
        # Pure: assert the kernel handed us a build context and read one generic
        # slot; never touch disk or network.
        assert isinstance(ctx, SessionBuildContext)
        assert ctx.workspace_dir is not None
        return PackContribution(tools={"toy_echo": FakeTool(name="toy_echo")})

    plugin.session_pack(toy_echo_pack, name="toy", priority=1100)
"""


def _write_plugin(root: Path, name: str, body: str) -> str:
    """Write a single-file plugin under ``root/name/plugin.py``; return its path."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plugin.py"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Client helpers (mirroring tests/test_plugin_wiring_contract.py)
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
    """The tool names on one composed request's wire schema."""
    return frozenset(
        spec["function"]["name"]
        for spec in (provider.received_requests[index].tools or ())
    )


def _build(tmp_path: Path, session_packs: tuple[SessionPackEntry, ...]) -> Any:
    """A default ``main`` build with an explicit ``session_packs`` injection.

    Mirrors ``tests/test_session_pack_goldens.py`` (same preset / store / model),
    so the built-in tool order equals ``GOLDEN_DEFAULT_TOOLS`` and any difference
    is attributable to the injected pack.
    """
    return build_code_replay_inputs(
        workspace_dir=tmp_path,
        agent=official_specs()["main"],
        content_store=InMemoryContentStore(),
        model="claude-sonnet-4-5",
        session_packs=session_packs,
    )


# ===========================================================================
# 1. Projection — the loader surfaces the external pack as (priority, name, factory)
# ===========================================================================


def test_projection_surfaces_the_external_session_pack(tmp_path: Path) -> None:
    """``activation_session_packs()`` projects a loaded plugin's pack triple.

    The wiring-plane, per-agent ``session_pack`` surface is projected exactly
    like ``tool_result_transform`` / ``reminder``: ``plugin name ->
    ((priority, contribution name, factory), …)``, external plugins only. The
    Client folds this into ``activated_session_packs`` for each activating agent,
    so this triple is the whole contract between the loader and the host — its
    shape and ordering values are what the builder loop later sorts on.
    """
    plugins = load_plugins(
        builtins=False,
        modules=[_write_plugin(tmp_path, "toybox", _SESSION_PACK_PLUGIN)],
    )
    projected = plugins.activation_session_packs()

    assert set(projected) == {"toybox"}
    entries = projected["toybox"]
    assert len(entries) == 1
    priority, name, factory = entries[0]
    assert (priority, name) == (1100, "toy")
    assert callable(factory)
    assert factory.__name__ == "toy_echo_pack"


# ===========================================================================
# 2. Builder — the kernel's generic loop runs an injected pack
# ===========================================================================


def test_builder_appends_external_pack_after_every_builtin_tool(
    tmp_path: Path, monkeypatch
) -> None:
    """A pack at a band above 1000 appends after every built-in tool.

    The builder runs ``default_session_packs()`` + the injected entry in ONE
    ``(priority, name)``-sorted loop; a pack at priority 1100 sorts past the last
    built-in band (app=1000). So its tool lands last, and the built-in golden
    order is preserved verbatim as the prefix — the extension is purely additive,
    the byte-order contract the goldens pin is untouched.
    """
    monkeypatch.delenv("NOETA_WEB_SEARCH_API_KEY", raising=False)
    packs = default_session_packs() + (SessionPackEntry("toy", 1100, _echo_pack),)
    inputs = _build(tmp_path, packs)

    order = list(inputs.tools)
    assert "toy_echo" in inputs.tools
    assert order == GOLDEN_DEFAULT_TOOLS + ["toy_echo"]
    assert order[-1] == "toy_echo"  # after every built-in
    assert order[: len(GOLDEN_DEFAULT_TOOLS)] == GOLDEN_DEFAULT_TOOLS  # prefix intact


def test_builder_interleaves_a_pack_between_priority_bands(
    tmp_path: Path, monkeypatch
) -> None:
    """A pack picks its neighbours by band — 250 lands between web and memory.

    Priority is not just append-order: a pack chooses where it sits relative to
    the built-in bands (web=200, memory=300). An entry at 250 must interleave —
    its tool sorts strictly after the web pack's ``webfetch`` and strictly before
    the memory pack's ``memory_write`` — proving the loop is a genuine
    priority merge, not a built-ins-then-extensions concatenation.
    """
    monkeypatch.delenv("NOETA_WEB_SEARCH_API_KEY", raising=False)
    packs = default_session_packs() + (
        SessionPackEntry("toy_mid", 250, _between_pack),
    )
    inputs = _build(tmp_path, packs)

    order = list(inputs.tools)
    idx = GOLDEN_DEFAULT_TOOLS.index("webfetch")
    expected = (
        GOLDEN_DEFAULT_TOOLS[: idx + 1]
        + ["toy_between"]
        + GOLDEN_DEFAULT_TOOLS[idx + 1 :]
    )
    assert order == expected
    assert order[order.index("webfetch") + 1] == "toy_between"  # after web (200)
    assert order[order.index("toy_between") + 1] == "memory_write"  # before memory (300)


# ===========================================================================
# 3. End-to-end — an activated plugin's pack tool reaches a built session
#    (S5 acceptance 5: zero kernel / SDK-host edits)
# ===========================================================================


def test_activated_pack_tool_reaches_the_built_session_and_scopes_to_the_agent(
    tmp_path: Path,
) -> None:
    """A third-party plugin's pack tool appears in a real ``Client`` session.

    The whole-path proof: a single-file plugin — nothing under ``packages/``
    changed — contributes a ``session_pack``; the agent that activates it gets
    ``toy_echo`` on its composed request's wire schema, and a sibling build that
    does NOT activate it does not. The seam is per-agent (wiring plane), so
    presence follows activation exactly, the same discipline every other
    per-agent surface honours.
    """
    plugin = _write_plugin(tmp_path, "toybox", _SESSION_PACK_PLUGIN)
    plugins = load_plugins(builtins=False, modules=[plugin])

    # Activated: main pulls in the pack, its tool composes onto the wire.
    active_provider = FakeLLMProvider(responses=[_end_turn()])
    active = _client(
        tmp_path,
        _bare(plugins=DEFAULT_PLUGINS + ("toybox",)),
        active_provider,
        plugins,
    )
    try:
        active.start(goal="hello")
    finally:
        active.shutdown()
    assert "toy_echo" in _request_tool_names(active_provider)

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
    assert "toy_echo" not in _request_tool_names(inactive_provider)
