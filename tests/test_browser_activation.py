"""Sandbox browser activation — the product-layer opt-in that registers the
``web`` subagent into main's delegation roster (spec D3 / B6).

``web`` is the sole identity that opens ``browser``; ``main`` stays browser-free
and delegates every page interaction to ``web``.

The browser subsystem is inert by default: ``main_options()`` carries no ``web``
agent and ``browser=False``, so every non-sandbox deployment's roster + stable
prefix are byte-identical to pre-browser-subsystem. ``sandbox_browser_options()``
is the explicit opt-in a product uses when it provisions per-session AIO
containers (``NOETA_AGENT_SANDBOX`` on) — only then can the browser tool pack
actually work. These tests pin that activation shape and the non-activation
invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.agent.spec import AgentSpec
from noeta.client.options import compile_options
from noeta.presets import (
    WEB_SUBAGENT,
    main_options,
    official_specs,
    sandbox_browser_options,
)


# -- Options-level activation --------------------------------------------- #


class TestSandboxBrowserOptions:
    """``sandbox_browser_options()`` — the activated recipe."""

    def test_adds_web_agent(self) -> None:
        opts = sandbox_browser_options()
        assert "web" in opts.agents
        assert opts.agents["web"] is WEB_SUBAGENT

    def test_main_browser_stays_off(self) -> None:
        # Direction A: main never opens ``browser`` — it has no browser tools
        # and must delegate to ``web``. Only ``web`` opens the capability.
        opts = sandbox_browser_options()
        assert opts.capabilities.browser is False

    def test_compiles_web_into_registry(self) -> None:
        main, descendants = compile_options(sandbox_browser_options())
        names = {main.name} | {d.name for d in descendants}
        assert "web" in names
        web = next(d for d in descendants if d.name == "web")
        assert web.capabilities.browser is True

    def test_main_spawnable_includes_web(self) -> None:
        main, _descendants = compile_options(sandbox_browser_options())
        assert "web" in main.capabilities.spawnable

    def test_main_identity_unchanged_from_main(self) -> None:
        """Activation only adds ``web`` to the roster; main's full capability
        identity is byte-identical to :func:`main_options` — including
        ``browser`` (stays ``False``; direction A). No drift of the
        conversational agent's capabilities."""
        base_main, _ = compile_options(main_options())
        sb_main, _ = compile_options(sandbox_browser_options())
        for field in ("todo_write", "ask_user_question", "delegation",
                       "skill_invocation", "memory", "mcp", "browser"):
            assert getattr(sb_main.capabilities, field) == getattr(
                base_main.capabilities, field
            ), f"capabilities.{field} drifted during browser activation"


# -- Non-activation invariant (stable prefix) ----------------------------- #


class TestDefaultInvariant:
    """``main_options()`` / ``official_specs()`` stay browser-free so the
    non-sandbox stable prefix is byte-identical."""

    def test_default_has_no_web(self) -> None:
        opts = main_options()
        assert "web" not in opts.agents

    def test_default_browser_off(self) -> None:
        opts = main_options()
        assert opts.capabilities.browser is False

    def test_official_specs_has_no_web(self) -> None:
        specs = official_specs()
        assert "web" not in specs

    def test_official_main_browser_off(self) -> None:
        specs = official_specs()
        assert specs["main"].capabilities.browser is False

    def test_official_main_spawnable_has_no_web(self) -> None:
        specs = official_specs()
        assert "web" not in specs["main"].capabilities.spawnable


# -- EngineRoom full-chain smoke ------------------------------------------ #


class TestEngineRoomActivation:
    """The served product threads ``sandbox_browser`` through ``EngineRoom``
    so the live registry gains ``web`` (main stays browser-free; only ``web``
    opens ``browser``)."""

    @pytest.fixture
    def stub_provider(self) -> object:
        from noeta.agent.observe._stub_provider import CodeStubProvider

        return CodeStubProvider()

    def _build_room(self, provider: object, *, sandbox_browser: bool):
        from noeta.agent.backend.engine_room import EngineRoom

        return EngineRoom.official(
            provider=provider,
            workspace_dir=Path("/tmp/noeta-browser-activation-test"),
            sandbox_browser=sandbox_browser,
        )

    def test_default_room_has_no_web(self, stub_provider: object) -> None:
        room = self._build_room(stub_provider, sandbox_browser=False)
        try:
            assert "web" not in room.agent_names()
            main = room._client.registry.resolve(room._client.main_agent_name)
            assert main.capabilities.browser is False
        finally:
            room._client.shutdown()

    def test_activated_room_has_web(self, stub_provider: object) -> None:
        room = self._build_room(stub_provider, sandbox_browser=True)
        try:
            assert "web" in room.agent_names()
            main = room._client.registry.resolve(room._client.main_agent_name)
            assert main.capabilities.browser is False
            assert "web" in main.capabilities.spawnable
            web = room._client.registry.resolve("web")
            assert web.capabilities.browser is True
        finally:
            room._client.shutdown()
