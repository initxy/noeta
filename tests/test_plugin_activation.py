"""Activation + effect scoping for the manifest-plugin path.

These pin:

* built-in feature-bundle activation folds into the ``AgentSpec.plugins``
  identity tuple;
* an unknown activation name fails compilation loudly;
* per-agent activation makes an external plugin's tools follow the agent that
  activates it, and NOT a sibling that does not;
* a loaded plugin's guard / observer is process authority — resolved from the
  loaded set regardless of any agent's activation.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from noeta.agent.spec import agent_activates
from noeta.client.options import (
    DEFAULT_PLUGINS,
    AgentDefinition,
    Options,
    PluginActivation,
    compile_options,
)
from noeta.client.plugin_set import load_plugins


# ---------------------------------------------------------------------------
# built-in feature-bundle activation folds onto identity flags
# ---------------------------------------------------------------------------


def _bare(**kw: object) -> Options:
    return Options(system_prompt="You are a helpful assistant.", **kw)  # type: ignore[arg-type]


def test_default_plugins_is_identity_inert() -> None:
    """A bare ``Options()`` activates ``fs`` / ``web`` — inert, no feature flags."""
    assert Options(system_prompt="x").plugins == DEFAULT_PLUGINS
    main, _ = compile_options(_bare())
    # The identity tuple is exactly the inert default packs; no feature name.
    assert main.plugins == DEFAULT_PLUGINS
    assert main.spawnable == ()


def test_memory_activation_sets_the_flag() -> None:
    """``plugins=["memory"]`` is the successor to ``Capabilities(memory=True)``."""
    main, _ = compile_options(_bare(plugins=("memory",)))
    assert agent_activates(main, "memory") is True
    # And nothing else lands in the tuple.
    assert main.plugins == ("memory",)
    assert main.spawnable == ()


def test_control_and_feature_bundles_all_map() -> None:
    names = (
        "todo_write",
        "ask_user_question",
        "skill_invocation",
        "memory",
        "mcp",
        "browser",
    )
    main, _ = compile_options(_bare(plugins=names))
    assert all(agent_activates(main, n) for n in names)


def test_control_tool_plugin_names_coexist_with_activation_bundles() -> None:
    """``todo_write`` / ``ask_user_question`` /
    ``delegation`` are BOTH real built-in plugins (a ``control_tool``
    contribution each) AND activation bundle names. Naming the plugins the same
    must NOT change activation semantics: with the full built-in catalogue
    loaded, ``plugins=(those names)`` still folds to the matching identity flags
    and never errors as a duplicate/unknown activation — the capability-flag
    branch resolves first, and built-in plugins are excluded from the external
    activation map so they can never collide there.
    """
    loaded = load_plugins(builtins=True)
    activation = loaded.identity_activations()
    # The real plugins exist in the loaded set but are excluded from the
    # external activation vocabulary (built-ins never follow per-agent activation).
    assert {"todo_write", "ask_user_question", "delegation"} <= set(loaded.names())
    assert not ({"todo_write", "ask_user_question", "delegation"} & set(activation))
    main, _ = compile_options(
        _bare(plugins=("todo_write", "ask_user_question", "delegation")),
        plugins=activation,
    )
    assert all(
        agent_activates(main, n)
        for n in ("todo_write", "ask_user_question", "delegation")
    )


def test_delegation_and_spawnable_stay_structural() -> None:
    """delegation / spawnable derive from the child roster, not activation."""
    opts = _bare(
        agents={
            "child": AgentDefinition(description="a child", prompt="do work"),
        },
    )
    main, _ = compile_options(opts)
    assert agent_activates(main, "delegation") is True
    assert main.spawnable == ("child",)


def test_child_activation_flows_to_child_caps() -> None:
    opts = _bare(
        agents={
            "scout": AgentDefinition(
                description="scout", prompt="look", plugins=("skill_invocation",)
            )
        }
    )
    _, descendants = compile_options(opts)
    (child,) = descendants
    assert agent_activates(child, "skill_invocation") is True
    assert agent_activates(child, "delegation") is False  # flat child never derives it


def test_unknown_activation_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unknown plugin activation 'nope'"):
        compile_options(_bare(plugins=("nope",)))


def test_unknown_child_activation_names_the_child() -> None:
    opts = _bare(
        agents={
            "c": AgentDefinition(description="c", prompt="p", plugins=("bogus",))
        }
    )
    with pytest.raises(ValueError, match="AgentDefinition 'c'"):
        compile_options(opts)


# ---------------------------------------------------------------------------
# external plugin identity contributions follow per-agent activation
# ---------------------------------------------------------------------------


def test_external_tool_follows_activation() -> None:
    """An agent that activates a plugin gets its tool; a sibling does not."""
    activation = {
        "extra": PluginActivation(tools=("WebSearch",)),
    }
    # allowed_tools=() so the base is empty and the plugin tool is the only add.
    active = _bare(allowed_tools=(), plugins=DEFAULT_PLUGINS + ("extra",))
    main_active, _ = compile_options(active, plugins=activation)
    assert "WebSearch" in {t.name for t in main_active.tools}

    inactive = _bare(allowed_tools=())  # DEFAULT_PLUGINS only, no "extra"
    main_inactive, _ = compile_options(inactive, plugins=activation)
    assert "WebSearch" not in {t.name for t in main_inactive.tools}


def test_external_prompt_fragment_follows_activation() -> None:
    activation = {"pf": PluginActivation(prompt_fragments=(("z", "EXTRA POLICY"),))}
    opts = _bare(plugins=DEFAULT_PLUGINS + ("pf",))
    main, _ = compile_options(opts, plugins=activation)
    assert main.instructions.endswith("EXTRA POLICY")


# ---------------------------------------------------------------------------
# guard / observer are process authority (do NOT follow activation)
# ---------------------------------------------------------------------------


_GUARD_PLUGIN = textwrap.dedent(
    """
    noeta_plugin_name = "audit"
    from noeta.sdk import PluginBuilder

    plugin = PluginBuilder("audit")

    class _AuditGuard:
        name = "audit"

    @plugin.guard(name="audit")
    def audit_guard():
        return _AuditGuard()
    """
)


def test_loaded_guard_is_process_wide(tmp_path: Path) -> None:
    """A loaded guard is resolved regardless of any agent activating the plugin."""
    plugin_file = tmp_path / "audit_plugin.py"
    plugin_file.write_text(_GUARD_PLUGIN, encoding="utf-8")

    pset = load_plugins(builtins=False, modules=(str(plugin_file),))
    assert "audit" in pset.names()

    guards, observers = pset.process_hooks()
    assert len(guards) == 1  # in force process-wide, no activation needed
    assert observers == ()
    # And the plugin contributes no identity-plane tools, so activation of it
    # would add nothing — the guard is the whole effect, and it is not gated.
    assert pset.identity_activations()["audit"].tools == ()
