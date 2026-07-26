"""Tests for the first-party ``protected-paths`` example plugin (M2).

Covered:

* the ``ProtectedPathsGuard`` verdicts — writes inside an allowed root pass,
  ``..`` traversal and absolute paths outside the roots are denied, ``..`` that
  stays inside is allowed, ``apply_patch`` is denied if *any* edit escapes, and
  non-mutating / non-tool actions are ignored;
* deny-glob precedence (a glob denies even inside an allowed root) and
  deny-glob-only mode (no roots);
* the factory's loud misconfiguration failure and config validation;
* the end-to-end load-by-path + ``merge_plugins`` wiring — the guard lands on
  ``Options.guards`` and enforces there.

The plugin lives in a hyphenated directory (not an importable package), so —
like the plugin loader — the module is loaded by explicit file path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from noeta.protocols.decisions import SpawnSubtaskDecision, ToolCall
from noeta.protocols.hooks import (
    GuardContext,
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    Verdict,
)
from noeta.sdk import (
    Options,
    PluginAPI,
    PluginError,
    load_plugins,
    merge_plugins,
)


# ---------------------------------------------------------------------------
# Load the example plugin module by explicit path (it is not importable).
# ---------------------------------------------------------------------------

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "plugins"
    / "protected-paths"
    / "plugin.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "protected_paths_plugin_under_test", PLUGIN_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load_module()
ProtectedPathsGuard = _MOD.ProtectedPathsGuard
noeta_plugin = _MOD.noeta_plugin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_verdict(guard, tool_name: str, arguments: dict):
    action = ProposedToolCall(
        call=ToolCall(tool_name=tool_name, arguments=arguments, call_id="c1")
    )
    return guard.check(action, GuardContext(task_id="t1"))


# ---------------------------------------------------------------------------
# Guard verdicts — containment
# ---------------------------------------------------------------------------


def test_write_relative_inside_root_allows(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    v = _tool_verdict(guard, "write", {"path": "notes/x.md", "content": "hi"})
    assert v.verdict is Verdict.ALLOW


def test_write_absolute_inside_root_allows(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    target = str(tmp_path / "a.txt")
    v = _tool_verdict(guard, "write", {"path": target, "content": "hi"})
    assert v.verdict is Verdict.ALLOW


def test_relative_dotdot_traversal_denies(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    v = _tool_verdict(guard, "write", {"path": "../outside.txt", "content": "x"})
    assert v.verdict is Verdict.DENY
    assert "protected-paths" in (v.reason or "")


def test_deep_traversal_denies(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    v = _tool_verdict(guard, "edit", {"path": "../../../etc/passwd", "old": "a", "new": "b"})
    assert v.verdict is Verdict.DENY


def test_absolute_path_outside_roots_denies(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    v = _tool_verdict(guard, "write", {"path": "/etc/passwd", "content": "x"})
    assert v.verdict is Verdict.DENY


def test_dotdot_that_stays_inside_allows(tmp_path):
    # ``sub/../a.txt`` collapses to ``a.txt`` — still inside the root.
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    v = _tool_verdict(guard, "write", {"path": "sub/../a.txt", "content": "x"})
    assert v.verdict is Verdict.ALLOW


def test_second_allowed_root_permits(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    guard = ProtectedPathsGuard(allowed_roots=[root_a, root_b])
    v = _tool_verdict(guard, "write", {"path": str(root_b / "ok.txt"), "content": "x"})
    assert v.verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# Guard verdicts — apply_patch (batch) + scope
# ---------------------------------------------------------------------------


def test_apply_patch_all_inside_allows(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    args = {
        "edits": [
            {"op": "replace", "path": "one.txt", "old": "a", "new": "b"},
            {"op": "create", "path": "sub/two.txt", "content": "c"},
        ]
    }
    assert _tool_verdict(guard, "apply_patch", args).verdict is Verdict.ALLOW


def test_apply_patch_any_escaping_edit_denies(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    args = {
        "edits": [
            {"op": "replace", "path": "ok.txt", "old": "a", "new": "b"},
            {"op": "create", "path": "../evil.txt", "content": "c"},
        ]
    }
    assert _tool_verdict(guard, "apply_patch", args).verdict is Verdict.DENY


def test_non_mutating_tool_is_ignored(tmp_path):
    # ``read`` is not a mutating tool — even a blatant traversal is allowed.
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    v = _tool_verdict(guard, "read", {"path": "../../etc/passwd"})
    assert v.verdict is Verdict.ALLOW


def test_shell_run_is_ignored(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    v = _tool_verdict(guard, "shell_run", {"command": "rm -rf /"})
    assert v.verdict is Verdict.ALLOW


def test_spawn_and_finish_are_allowed(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    ctx = GuardContext(task_id="t1")
    spawn = ProposedSpawnSubtask(
        decision=SpawnSubtaskDecision(agent_name="worker", goal="go")
    )
    assert guard.check(spawn, ctx).verdict is Verdict.ALLOW
    assert guard.check(ProposedFinish(answer="done"), ctx).verdict is Verdict.ALLOW


def test_missing_path_defers_to_tool(tmp_path):
    # No readable path ⇒ the guard has nothing to rule on and allows (the tool
    # rejects the malformed call; no write happens).
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path])
    assert _tool_verdict(guard, "write", {"content": "x"}).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# Guard verdicts — deny globs
# ---------------------------------------------------------------------------


def test_deny_glob_wins_inside_allowed_root(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path], deny_globs=["*.env"])
    v = _tool_verdict(guard, "write", {"path": "config.env", "content": "x"})
    assert v.verdict is Verdict.DENY
    assert "deny glob" in (v.reason or "")


def test_deny_glob_basename_match(tmp_path):
    guard = ProtectedPathsGuard(allowed_roots=[tmp_path], deny_globs=["id_rsa"])
    v = _tool_verdict(guard, "write", {"path": "keys/id_rsa", "content": "x"})
    assert v.verdict is Verdict.DENY


def test_deny_glob_only_mode_without_roots():
    # No allowed_roots ⇒ containment disabled; only deny globs bite.
    guard = ProtectedPathsGuard(deny_globs=["id_rsa"])
    assert _tool_verdict(
        guard, "write", {"path": "/home/u/.ssh/id_rsa", "content": "x"}
    ).verdict is Verdict.DENY
    assert _tool_verdict(
        guard, "write", {"path": "/home/u/ok.txt", "content": "x"}
    ).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# Factory — config validation + loud misconfiguration
# ---------------------------------------------------------------------------


def test_factory_requires_some_protection():
    api = PluginAPI("protected-paths")
    with pytest.raises(PluginError):
        noeta_plugin(api)  # no config at all → protects nothing


def test_factory_rejects_bare_string_roots():
    api = PluginAPI("protected-paths")
    with pytest.raises(PluginError):
        noeta_plugin(api, {"allowed_roots": "/ws"})


def test_factory_rejects_empty_root_entry():
    api = PluginAPI("protected-paths")
    with pytest.raises(PluginError):
        noeta_plugin(api, {"allowed_roots": ["  "]})


def test_factory_adds_one_guard(tmp_path):
    api = PluginAPI("protected-paths")
    noeta_plugin(api, {"allowed_roots": [str(tmp_path)]})
    contributions = api._contributions()
    assert len(contributions.guards) == 1
    assert contributions.guards[0].name == "protected_paths"


# ---------------------------------------------------------------------------
# End-to-end — load by explicit path + merge_plugins
# ---------------------------------------------------------------------------


def test_end_to_end_load_and_merge(tmp_path):
    plugins = load_plugins(
        modules=[str(PLUGIN_PATH)],
        config={"protected-paths": {"allowed_roots": [str(tmp_path)]}},
    )
    # The module override sets the name to ``protected-paths`` despite plugin.py.
    assert [p.name for p in plugins] == ["protected-paths"]

    base = Options(
        system_prompt="root",
        allowed_tools=("read", "write", "edit", "apply_patch"),
    )
    merged = merge_plugins(base, plugins)

    guards = [g for g in merged.guards if g.name == "protected_paths"]
    assert len(guards) == 1
    guard = guards[0]

    # The wired guard enforces: inside allows, traversal denies.
    assert _tool_verdict(
        guard, "write", {"path": "a.txt", "content": "x"}
    ).verdict is Verdict.ALLOW
    assert _tool_verdict(
        guard, "write", {"path": "../a.txt", "content": "x"}
    ).verdict is Verdict.DENY


def test_end_to_end_missing_config_fails_loudly():
    # Enabled without config ⇒ the factory raises, and the loader surfaces it
    # as a PluginError at build time (never a silent skip, never mid-session).
    with pytest.raises(PluginError):
        load_plugins(modules=[str(PLUGIN_PATH)])
