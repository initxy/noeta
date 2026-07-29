"""Tests for the ``git-checkpoint`` example plugin (M2).

Loads the plugin by explicit path (it is an in-tree example, not an installed
distribution) and drives it with synthetic ``ToolCallStarted`` events against a
throwaway ``tmp_path`` git repo. The invariants under test:

1. A mutating tool call records a checkpoint commit and advances the dedicated
   ref (chained on the previous checkpoint).
2. The user's branch (``HEAD``) and staging area (``.git/index``) are byte-for
   -byte untouched by checkpointing, and the checkpoint ref is not a branch.
3. Non-mutating tool calls record nothing.
4. ``restore_checkpoint`` writes a checkpoint's tree back into the working tree
   (tip or a specific commit) without moving ``HEAD`` or the index; and a repo
   with **no configured git identity** still records checkpoints, attributed to
   the plugin's own identity rather than the user.
5. The Observer swallows failures (guard-observer ADR) — a non-git path never
   raises into the caller.
6. The plugin factory wires the Observer onto ``Options.observers`` via the
   SDK loader, honouring operator ``config``.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from noeta.protocols.events import EventEnvelope, ToolCallStartedPayload
from noeta.sdk import PluginBuilder, load_plugin_set


_PLUGIN_PATH = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "plugins"
    / "git-checkpoint"
    / "plugin.py"
)
_CHECKPOINT_REF = "refs/noeta/checkpoints"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_module():
    """Import the example plugin module by path (bypassing installation)."""
    spec = importlib.util.spec_from_file_location(
        "_git_checkpoint_example", _PLUGIN_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "User")
    _git(repo, "config", "user.email", "user@example.com")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    return repo


def _started(tool_name: str, seq: int = 1) -> EventEnvelope:
    return EventEnvelope.build(
        task_id="task-1",
        type="ToolCallStarted",
        payload=ToolCallStartedPayload(
            call_id=f"call-{seq}", tool_name=tool_name
        ),
    ).with_seq(seq)


def _ref_tip(repo: Path) -> str:
    return _git(repo, "rev-parse", "--verify", "--quiet", _CHECKPOINT_REF)


def _ref_exists(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", _CHECKPOINT_REF],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# 1. Checkpoint recorded + ref advances + chains
# ---------------------------------------------------------------------------


def test_mutating_call_records_checkpoint_and_ref_advances(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    observer = mod.GitCheckpointObserver(repo)

    assert not _ref_exists(repo)

    observer(_started("write", 1))
    c1 = _ref_tip(repo)
    assert c1  # a first checkpoint exists

    # Mutate the worktree, then a second mutating call.
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    observer(_started("edit", 2))
    c2 = _ref_tip(repo)

    assert c2 != c1  # the ref advanced
    # The second checkpoint is chained on the first.
    assert _git(repo, "rev-parse", f"{c2}^") == c1
    # The two checkpoints captured the two worktree states.
    assert _git(repo, "show", f"{c1}:a.txt") == "v1"
    assert _git(repo, "show", f"{c2}:a.txt") == "v2"


def test_apply_patch_also_triggers_a_checkpoint(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    observer = mod.GitCheckpointObserver(repo)
    observer(_started("apply_patch", 1))
    assert _ref_exists(repo)


# ---------------------------------------------------------------------------
# 2. User branch / index untouched; checkpoint ref is not a branch
# ---------------------------------------------------------------------------


def test_user_branch_and_index_untouched(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    observer = mod.GitCheckpointObserver(repo)

    head_before = _git(repo, "rev-parse", "HEAD")
    branch_before = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    index_before = (repo / ".git" / "index").read_bytes()

    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    observer(_started("write", 1))

    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
    assert (repo / ".git" / "index").read_bytes() == index_before

    # The checkpoint ref is not a branch and never appears in the user's log.
    branches = _git(repo, "branch", "--format=%(refname:short)").split()
    assert "noeta" not in " ".join(branches)
    assert _CHECKPOINT_REF not in _git(repo, "log", "--format=%D")


# ---------------------------------------------------------------------------
# 3. Non-mutating tools are ignored
# ---------------------------------------------------------------------------


def test_non_mutating_tool_records_nothing(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    observer = mod.GitCheckpointObserver(repo)

    observer(_started("read", 1))
    observer(_started("grep", 2))
    assert not _ref_exists(repo)


def test_non_started_event_records_nothing(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    observer = mod.GitCheckpointObserver(repo)

    other = EventEnvelope.build(
        task_id="task-1",
        type="ToolResultRecorded",
        payload=ToolCallStartedPayload(call_id="c", tool_name="write"),
    )
    observer(other)
    assert not _ref_exists(repo)


# ---------------------------------------------------------------------------
# 4. Restore
# ---------------------------------------------------------------------------


def test_restore_tip_rewrites_worktree_without_touching_head_or_index(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    observer = mod.GitCheckpointObserver(repo)

    # Checkpoint the v1 worktree.
    observer(_started("write", 1))

    head_before = _git(repo, "rev-parse", "HEAD")
    index_before = (repo / ".git" / "index").read_bytes()

    # Make an unrelated mess in the worktree.
    (repo / "a.txt").write_text("v3-uncommitted\n", encoding="utf-8")

    restored = mod.restore_checkpoint(repo)

    assert restored == _ref_tip(repo)
    assert (repo / "a.txt").read_text() == "v1\n"
    # HEAD and the index did not move.
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_restore_specific_commit(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    observer = mod.GitCheckpointObserver(repo)

    observer(_started("write", 1))
    c1 = _ref_tip(repo)
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    observer(_started("edit", 2))

    # Worktree currently v2; restore the earlier checkpoint explicitly.
    restored = mod.restore_checkpoint(repo, commit=c1)
    assert restored == c1
    assert (repo / "a.txt").read_text() == "v1\n"


def test_restore_on_missing_ref_raises(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    with pytest.raises(mod.GitCheckpointError):
        mod.restore_checkpoint(repo)


# ---------------------------------------------------------------------------
# 4b. Checkpoint identity — works without a configured git user
# ---------------------------------------------------------------------------


def test_checkpoint_records_in_a_repo_with_no_configured_identity(
    tmp_path, monkeypatch
):
    """A repo with no ``user.name`` / ``user.email`` still gets checkpoints.

    ``git commit-tree`` refuses to guess an identity ("unable to auto-detect
    email address"), and the Observer swallows failures — so without an explicit
    identity a fresh repo would silently record nothing. The plugin exports its
    own (``noeta-checkpoint``), which also keeps a checkpoint from masquerading
    as the user's own commit.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)

    mod = _load_module()
    repo = tmp_path / "no_identity"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")

    mod.GitCheckpointObserver(repo)(_started("write", 1))

    tip = _ref_tip(repo)
    assert tip, "no checkpoint recorded in a repo without a git identity"
    who = _git(repo, "show", "-s", "--format=%an <%ae>|%cn <%ce>", tip)
    assert who == (
        "noeta-checkpoint <noeta-checkpoint@localhost>|"
        "noeta-checkpoint <noeta-checkpoint@localhost>"
    )


# ---------------------------------------------------------------------------
# 5. Observer swallows failures (guard-observer ADR)
# ---------------------------------------------------------------------------


def test_observer_swallows_non_git_path(tmp_path):
    mod = _load_module()
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    observer = mod.GitCheckpointObserver(not_a_repo)

    # Must not raise, and must not create a ref.
    observer(_started("write", 1))
    assert not _ref_exists(not_a_repo)


# ---------------------------------------------------------------------------
# 6. Manifest wiring through the new loader (load_plugin_set + process_hooks)
# ---------------------------------------------------------------------------


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_git_checkpoint_manifest", _PLUGIN_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_module_exposes_builder_and_configured_observer():
    mod = _load_module()
    assert isinstance(mod.plugin, PluginBuilder)
    assert mod.plugin.name == "git-checkpoint"
    assert type(mod.OBSERVER).__name__ == "GitCheckpointObserver"
    assert mod.OBSERVER.name == "git_checkpoint"


def test_load_plugin_set_lists_the_observer_without_execution():
    pset = load_plugin_set(builtins=False, modules=[str(_PLUGIN_PATH)])
    assert pset.names() == ("git-checkpoint",)
    listed = pset.contributions("observer")
    assert [(p, c.surface, c.name) for p, c in listed] == [
        ("git-checkpoint", "observer", "git_checkpoint")
    ]


def test_process_hooks_resolves_env_configured_observer(tmp_path):
    # The observer's repo + ref come from the environment (read at plugin import).
    repo = _init_repo(tmp_path)
    saved = {
        k: os.environ.get(k)
        for k in ("NOETA_GIT_CHECKPOINT_REPO", "NOETA_GIT_CHECKPOINT_REF")
    }
    os.environ["NOETA_GIT_CHECKPOINT_REPO"] = str(repo)
    os.environ["NOETA_GIT_CHECKPOINT_REF"] = _CHECKPOINT_REF
    try:
        pset = load_plugin_set(builtins=False, modules=[str(_PLUGIN_PATH)])
        guards, observers = pset.process_hooks()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    assert guards == ()  # no guard contributions
    assert len(observers) == 1  # governance: process-wide, no activation
    observer = observers[0]
    assert type(observer).__name__ == "GitCheckpointObserver"
    assert observer.repo_path == repo
    assert observer.ref == _CHECKPOINT_REF
    # Functionally live: a mutating tool call records a checkpoint.
    observer(_started("write", 1))
    assert _ref_exists(repo)


def test_custom_mutating_tools_via_direct_construction(tmp_path):
    # The mutating-tool set is a construction knob (the manifest ships the
    # default set); construct the Observer directly to narrow it.
    mod = _load_module()
    repo = _init_repo(tmp_path)
    observer = mod.GitCheckpointObserver(repo, mutating_tools=["write"])

    observer(_started("edit", 1))  # "edit" not in the custom set → ignored
    assert not _ref_exists(repo)
    observer(_started("write", 2))
    assert _ref_exists(repo)
