"""The first-party ``protected-paths`` manifest plugin.

Covered:

* the ``ProtectedPathsGuard`` verdicts — writes inside an allowed root pass,
  ``..`` traversal and absolute paths outside the roots are denied, ``..`` that
  stays inside is allowed, ``apply_patch`` is denied if *any* edit escapes, and
  non-mutating / non-tool actions are ignored;
* deny-glob precedence (a glob denies even inside an allowed root) and
  deny-glob-only mode (no roots);
* the manifest ships a configured guard built from the environment;
* the loading API — ``load_plugins`` lists the ``guard`` contribution
  without executing plugin code, and ``process_hooks`` resolves the configured
  guard (governance, process-wide).

The plugin lives in a hyphenated directory (not an importable package), so —
like the plugin loader — the module is loaded by explicit file path.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

from noeta.protocols.decisions import SpawnSubtaskDecision, ToolCall
from noeta.protocols.hooks import (
    GuardContext,
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    Verdict,
)
from noeta.sdk import PluginBuilder, load_plugins


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


def _load_with_env(**env: str) -> ModuleType:
    """Re-execute the plugin module with ``env`` applied (it reads env at import)."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return _load_module()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_MOD = _load_module()
ProtectedPathsGuard = _MOD.ProtectedPathsGuard


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
# Manifest — the configured guard is built from the environment
# ---------------------------------------------------------------------------


def test_module_exposes_builder_and_configured_guard():
    assert isinstance(_MOD.plugin, PluginBuilder)
    assert _MOD.plugin.name == "protected-paths"
    assert isinstance(_MOD.GUARD, ProtectedPathsGuard)
    assert _MOD.GUARD.name == "protected_paths"


def test_manifest_guard_fences_the_env_configured_root(tmp_path):
    mod = _load_with_env(NOETA_PROTECTED_PATHS_ROOTS=str(tmp_path))
    guard = mod.GUARD
    # Configured from NOETA_PROTECTED_PATHS_ROOTS: inside allows, outside denies.
    assert _tool_verdict(
        guard, "write", {"path": str(tmp_path / "a.txt"), "content": "x"}
    ).verdict is Verdict.ALLOW
    assert _tool_verdict(
        guard, "write", {"path": "/etc/passwd", "content": "x"}
    ).verdict is Verdict.DENY


def test_manifest_deny_globs_from_env(tmp_path):
    mod = _load_with_env(
        NOETA_PROTECTED_PATHS_ROOTS=str(tmp_path),
        NOETA_PROTECTED_PATHS_DENY_GLOBS="*.env,id_rsa",
    )
    assert _tool_verdict(
        mod.GUARD, "write", {"path": str(tmp_path / "config.env"), "content": "x"}
    ).verdict is Verdict.DENY
    assert _tool_verdict(
        mod.GUARD, "write", {"path": str(tmp_path / "ok.txt"), "content": "x"}
    ).verdict is Verdict.ALLOW


def test_default_config_protects_cwd_not_nothing():
    # No env ⇒ the guard defaults to the cwd (never an empty root set — a fence
    # that protects nothing is a bug, not a default).
    guard = ProtectedPathsGuard(allowed_roots=_MOD._roots_from_env())
    assert guard._allowed_roots  # non-empty


# ---------------------------------------------------------------------------
# End-to-end — the manifest loading API (load_plugins + process_hooks)
# ---------------------------------------------------------------------------


def test_load_plugins_lists_the_guard_without_execution(tmp_path):
    pset = load_plugins(builtins=False, modules=[str(PLUGIN_PATH)])
    assert pset.names() == ("protected-paths",)
    # Listing reads only the static manifest.
    listed = pset.contributions("guard")
    assert [(p, c.surface, c.name) for p, c in listed] == [
        ("protected-paths", "guard", "protected_paths")
    ]


def test_process_hooks_resolves_the_configured_guard(tmp_path):
    # Env is read when the plugin module is executed by the loader.
    os.environ["NOETA_PROTECTED_PATHS_ROOTS"] = str(tmp_path)
    try:
        pset = load_plugins(builtins=False, modules=[str(PLUGIN_PATH)])
        guards, observers = pset.process_hooks()
    finally:
        os.environ.pop("NOETA_PROTECTED_PATHS_ROOTS", None)

    assert observers == ()  # no observer contributions
    assert len(guards) == 1  # governance: in force process-wide, no activation
    guard = guards[0]
    assert guard.name == "protected_paths"
    assert _tool_verdict(
        guard, "write", {"path": str(tmp_path / "a.txt"), "content": "x"}
    ).verdict is Verdict.ALLOW
    assert _tool_verdict(
        guard, "write", {"path": "/etc/passwd", "content": "x"}
    ).verdict is Verdict.DENY
