"""Tests for the first-party ``approval-modes`` example plugin (M2).

Covers the spec's acceptance criterion for ``approval-modes``: the four
goose-style modes produce the expected verdicts (``chat`` denies tools;
``approve`` requires approval on all; ``smart_approve`` allows low-risk and
requires approval otherwise; ``auto`` allows), with per-tool overrides winning;
plus config validation and loading through ``load_plugins`` + ``merge_plugins``
end-to-end via the loader's explicit-path mechanism (no install).

The plugin is loaded twice under two module objects: once here by explicit
``importlib`` to reach its classes (``ApprovalModesGuard`` / ``build_policy``),
and once inside ``load_plugins`` for the end-to-end path. Those two module loads
carry *distinct* class objects, so the end-to-end assertions identify the
contributed guard by its ``name`` attribute and exercise ``check`` directly
rather than by ``isinstance`` (``Verdict`` / ``VerdictResult`` come from the
shared ``noeta.protocols.hooks`` singleton, so verdict comparisons are stable
across both loads).
"""

from __future__ import annotations

import importlib.util
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
from noeta.sdk import Options, load_plugins, merge_plugins


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
# 5. End-to-end: load_plugins + merge_plugins
# ---------------------------------------------------------------------------


def _find_guard(options: Options):
    landed = [
        g for g in options.guards if getattr(g, "name", None) == "approval_modes"
    ]
    assert len(landed) == 1, "exactly one approval-modes guard should land"
    return landed[0]


def test_load_and_merge_end_to_end():
    plugins = load_plugins(
        modules=[str(_PLUGIN_PATH)],
        config={
            "approval-modes": {
                "mode": "smart_approve",
                "overrides": {"write": "never"},
            }
        },
    )
    # The module fixes its own name via ``noeta_plugin_name``.
    assert [p.name for p in plugins] == ["approval-modes"]
    assert len(plugins[0].contributions.guards) == 1

    base = Options(system_prompt="root", allowed_tools=("read", "write"))
    merged = merge_plugins(base, plugins)

    guard = _find_guard(merged)
    assert _verdict(guard, "read") is Verdict.ALLOW  # low-risk under smart_approve
    assert _verdict(guard, "write") is Verdict.DENY  # override "never" wins
    assert _verdict(guard, "shell_run") is Verdict.REQUIRE_APPROVAL


def test_load_without_config_defaults_to_approve():
    plugins = load_plugins(modules=[str(_PLUGIN_PATH)])
    assert [p.name for p in plugins] == ["approval-modes"]
    merged = merge_plugins(Options(system_prompt="root"), plugins)
    guard = _find_guard(merged)
    assert _verdict(guard, "read") is Verdict.REQUIRE_APPROVAL
    assert _verdict(guard, "write") is Verdict.REQUIRE_APPROVAL
