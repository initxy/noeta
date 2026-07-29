"""Tests for the first-party ``approval-modes`` manifest plugin (M5).

Covers the spec's acceptance criterion for ``approval-modes``: the four
goose-style modes produce the expected verdicts (``chat`` denies tools;
``approve`` requires approval on all; ``smart_approve`` allows low-risk and
requires approval otherwise; ``auto`` allows), with per-tool overrides winning;
plus config validation and loading through ``load_plugin_set`` + ``process_hooks``
end-to-end via the loader's explicit-path mechanism (no install).

The plugin is loaded twice under two module objects: once here by explicit
``importlib`` to reach its classes (``ApprovalModesGuard`` / ``build_policy``),
and once inside ``load_plugin_set`` for the end-to-end path. Those two module
loads carry *distinct* class objects, so the end-to-end assertions identify the
resolved guard by its ``name`` attribute and exercise ``check`` directly rather
than by ``isinstance`` (``Verdict`` / ``VerdictResult`` come from the shared
``noeta.protocols.hooks`` singleton, so verdict comparisons are stable across
both loads).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from noeta.protocols.decisions import SpawnSubtaskDecision, ToolCall
from noeta.protocols.hooks import (
    GuardContext,
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    Verdict,
)
from noeta.sdk import PluginBuilder, load_plugin_set


_PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "plugins"
    / "approval-modes"
    / "plugin.py"
)


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "approval_modes_plugin_under_test", _PLUGIN_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_with_env(**env: str):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return _load_plugin_module()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_mod = _load_plugin_module()
ApprovalModesGuard = _mod.ApprovalModesGuard
build_policy = _mod.build_policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> GuardContext:
    return GuardContext(task_id="t1")


def _verdict(guard, name: str) -> Verdict:
    call = ProposedToolCall(ToolCall(tool_name=name, arguments={}, call_id="c1"))
    return guard.check(call, _ctx()).verdict


def _guard(**config) -> "ApprovalModesGuard":
    return ApprovalModesGuard(build_policy(config))


# ---------------------------------------------------------------------------
# 1. The four modes' verdicts
# ---------------------------------------------------------------------------


def test_chat_mode_denies_all_tool_calls():
    guard = _guard(mode="chat")
    assert _verdict(guard, "read") is Verdict.DENY
    assert _verdict(guard, "write") is Verdict.DENY
    assert _verdict(guard, "shell_run") is Verdict.DENY


def test_approve_mode_requires_approval_on_all():
    guard = _guard(mode="approve")
    assert _verdict(guard, "read") is Verdict.REQUIRE_APPROVAL
    assert _verdict(guard, "write") is Verdict.REQUIRE_APPROVAL


def test_default_mode_is_approve():
    # Empty config → default "approve" (require approval on every tool).
    guard = _guard()
    assert _verdict(guard, "read") is Verdict.REQUIRE_APPROVAL
    assert _verdict(guard, "write") is Verdict.REQUIRE_APPROVAL


def test_smart_approve_allows_low_risk_asks_otherwise():
    guard = _guard(mode="smart_approve")
    for low in ("read", "grep", "glob", "ls"):
        assert _verdict(guard, low) is Verdict.ALLOW
    assert _verdict(guard, "write") is Verdict.REQUIRE_APPROVAL
    assert _verdict(guard, "shell_run") is Verdict.REQUIRE_APPROVAL


def test_smart_approve_classification_is_configurable():
    guard = _guard(
        mode="smart_approve", low_risk_tools=["read", "custom_safe"]
    )
    assert _verdict(guard, "custom_safe") is Verdict.ALLOW
    assert _verdict(guard, "read") is Verdict.ALLOW
    # The supplied set REPLACES the default, so 'grep' is no longer low-risk.
    assert _verdict(guard, "grep") is Verdict.REQUIRE_APPROVAL


def test_auto_mode_allows_all():
    guard = _guard(mode="auto")
    assert _verdict(guard, "read") is Verdict.ALLOW
    assert _verdict(guard, "write") is Verdict.ALLOW
    assert _verdict(guard, "shell_run") is Verdict.ALLOW


# ---------------------------------------------------------------------------
# 2. Per-tool override precedence (override always wins over the mode)
# ---------------------------------------------------------------------------


def test_override_always_beats_chat_deny():
    guard = _guard(mode="chat", overrides={"read": "always"})
    assert _verdict(guard, "read") is Verdict.ALLOW  # override wins
    assert _verdict(guard, "write") is Verdict.DENY  # mode still applies


def test_override_never_beats_auto_allow():
    guard = _guard(mode="auto", overrides={"write": "never"})
    assert _verdict(guard, "write") is Verdict.DENY  # override wins
    assert _verdict(guard, "read") is Verdict.ALLOW  # mode still applies


def test_override_ask_beats_smart_approve_allow():
    guard = _guard(mode="smart_approve", overrides={"read": "ask"})
    # 'read' is low-risk (would allow) but the override forces approval.
    assert _verdict(guard, "read") is Verdict.REQUIRE_APPROVAL
    assert _verdict(guard, "grep") is Verdict.ALLOW  # unaffected low-risk tool


# ---------------------------------------------------------------------------
# 3. Only tool calls are gated
# ---------------------------------------------------------------------------


def test_non_tool_call_actions_pass_through_even_in_chat():
    guard = _guard(mode="chat")
    spawn = ProposedSpawnSubtask(
        SpawnSubtaskDecision(agent_name="worker", goal="do it")
    )
    assert guard.check(spawn, _ctx()).verdict is Verdict.ALLOW
    assert guard.check(ProposedFinish(answer="done"), _ctx()).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# 4. Config validation (loud failure)
# ---------------------------------------------------------------------------


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        build_policy({"mode": "yolo"})


def test_bad_override_token_raises():
    with pytest.raises(ValueError):
        build_policy({"overrides": {"read": "maybe"}})


def test_non_list_low_risk_tools_raises():
    with pytest.raises(ValueError):
        build_policy({"low_risk_tools": "read"})


# ---------------------------------------------------------------------------
# 5. Manifest: env-configured guard + the new loading API
# ---------------------------------------------------------------------------


def test_module_exposes_builder_and_configured_guard():
    assert isinstance(_mod.plugin, PluginBuilder)
    assert _mod.plugin.name == "approval-modes"
    assert isinstance(_mod.GUARD, ApprovalModesGuard)
    assert _mod.GUARD.name == "approval_modes"


def _sole_guard(pset):
    guards, observers = pset.process_hooks()
    assert observers == ()
    assert len(guards) == 1, "exactly one approval-modes guard should resolve"
    return guards[0]


def test_load_plugin_set_lists_the_guard_without_execution():
    pset = load_plugin_set(builtins=False, modules=[str(_PLUGIN_PATH)])
    assert pset.names() == ("approval-modes",)
    listed = pset.contributions("guard")
    assert [(p, c.surface, c.name) for p, c in listed] == [
        ("approval-modes", "guard", "approval_modes")
    ]


def test_process_hooks_resolves_env_configured_mode():
    # NOETA_APPROVAL_MODE selects the mode when the plugin module is executed.
    os.environ["NOETA_APPROVAL_MODE"] = "smart_approve"
    try:
        pset = load_plugin_set(builtins=False, modules=[str(_PLUGIN_PATH)])
        guard = _sole_guard(pset)
    finally:
        os.environ.pop("NOETA_APPROVAL_MODE", None)
    assert _verdict(guard, "read") is Verdict.ALLOW  # low-risk under smart_approve
    assert _verdict(guard, "shell_run") is Verdict.REQUIRE_APPROVAL


def test_shipped_default_mode_is_approve():
    # No env ⇒ the shipped guard defaults to "approve" (require approval on all).
    pset = load_plugin_set(builtins=False, modules=[str(_PLUGIN_PATH)])
    guard = _sole_guard(pset)
    assert _verdict(guard, "read") is Verdict.REQUIRE_APPROVAL
    assert _verdict(guard, "write") is Verdict.REQUIRE_APPROVAL
