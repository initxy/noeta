"""Three-tier skill merge + single global memory tier.

Skills go from a single directory to three tiers (builtin < global < workspace-local,
workspace wins), merged via the existing ``merge_skill_registries``. Memory is pinned to
one global directory: all reads/writes happen there, independent of the workspace. Skills
and memory no longer drift with the working directory.

Acceptance criteria covered:

* Same-named skill: workspace-local shadows global shadows builtin.
* A fresh empty workspace can still use global/builtin skills (the skill set is non-empty).
* Memory reads/writes target the global directory, independent of the current workspace;
  switching workspaces leaves memory unchanged.
* Global skills default to ``~/.noeta/skills`` and memory to ``~/.noeta/memories``; the
  agent layer can override both.
* ``merge_skill_registries`` actually enters the execution path (previously never called).
"""

from __future__ import annotations

from pathlib import Path

from tests._skill_fixtures import write_skill_raw

from noeta.agent.skills import (
    BUILTIN_SKILLS_DIR,
    DEFAULT_GLOBAL_SKILLS_DIR,
    load_builtin_skills,
)
from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
from noeta.execution.memory import DEFAULT_GLOBAL_MEMORY_DIR, load_memory_store
from noeta.execution.skills import load_workspace_skills
from noeta.guards.budget import Budget
from noeta.storage.memory import InMemoryContentStore


def _skill_body(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\nBody of {name}.\n"


# ---------------------------------------------------------------------------
# load_workspace_skills — three-tier merge precision
# ---------------------------------------------------------------------------


def test_three_tiers_disjoint_names_all_present(tmp_path: Path) -> None:
    """Builtin/global/workspace each contribute a unique skill -> the union is all present."""
    builtin = tmp_path / "builtin"
    write_skill_raw(builtin, "review", _skill_body("review", "builtin review"))
    glob = tmp_path / "global"
    write_skill_raw(glob, "deploy", _skill_body("deploy", "global deploy"))
    ws = tmp_path / "ws"
    ws_local = ws / ".noeta" / "skills"
    write_skill_raw(ws_local, "tidy", _skill_body("tidy", "workspace tidy"))

    registry = load_workspace_skills(
        ws, lower_skill_dirs=[builtin, glob]
    )
    assert set(registry.names()) == {"review", "deploy", "tidy"}


def test_workspace_local_shadows_global_shadows_builtin(tmp_path: Path) -> None:
    """``edit`` exists in all three tiers: workspace-local wins (builtin < global < workspace)."""
    builtin = tmp_path / "builtin"
    write_skill_raw(builtin, "edit", _skill_body("edit", "BUILTIN edit"))
    glob = tmp_path / "global"
    write_skill_raw(glob, "edit", _skill_body("edit", "GLOBAL edit"))
    ws = tmp_path / "ws"
    ws_local = ws / ".noeta" / "skills"
    write_skill_raw(ws_local, "edit", _skill_body("edit", "WORKSPACE edit"))

    registry = load_workspace_skills(ws, lower_skill_dirs=[builtin, glob])
    desc = registry.get("edit")
    assert desc is not None
    assert desc.description == "WORKSPACE edit"


def test_global_shadows_builtin_when_no_workspace_clash(tmp_path: Path) -> None:
    """Same name only in builtin + global: global wins (order builtin < global)."""
    builtin = tmp_path / "builtin"
    write_skill_raw(builtin, "edit", _skill_body("edit", "BUILTIN edit"))
    glob = tmp_path / "global"
    write_skill_raw(glob, "edit", _skill_body("edit", "GLOBAL edit"))
    ws = tmp_path / "ws"
    ws.mkdir()

    registry = load_workspace_skills(ws, lower_skill_dirs=[builtin, glob])
    desc = registry.get("edit")
    assert desc is not None
    assert desc.description == "GLOBAL edit"


def test_empty_workspace_still_sees_lower_tiers(tmp_path: Path) -> None:
    """A fresh empty workspace (no .noeta/skills) can still use global/builtin skills."""
    builtin = tmp_path / "builtin"
    write_skill_raw(builtin, "review", _skill_body("review", "builtin review"))
    glob = tmp_path / "global"
    write_skill_raw(glob, "deploy", _skill_body("deploy", "global deploy"))
    ws = tmp_path / "ws"
    ws.mkdir()  # completely empty workspace

    registry = load_workspace_skills(ws, lower_skill_dirs=[builtin, glob])
    assert set(registry.names()) == {"review", "deploy"}


def test_missing_lower_dir_is_skipped_not_errored(tmp_path: Path) -> None:
    """A nonexistent lower-tier directory yields an empty registry, not an error."""
    missing = tmp_path / "does-not-exist"
    ws = tmp_path / "ws"
    ws_local = ws / ".noeta" / "skills"
    write_skill_raw(ws_local, "tidy", _skill_body("tidy", "workspace tidy"))

    registry = load_workspace_skills(ws, lower_skill_dirs=[missing])
    assert registry.names() == ("tidy",)


def test_no_lower_dirs_keeps_single_dir_behaviour(tmp_path: Path) -> None:
    """Empty ``lower_skill_dirs`` (the default) = legacy single-tier behavior, byte-for-byte."""
    ws = tmp_path / "ws"
    ws_local = ws / ".noeta" / "skills"
    write_skill_raw(ws_local, "tidy", _skill_body("tidy", "workspace tidy"))

    registry = load_workspace_skills(ws)
    assert registry.names() == ("tidy",)


# ---------------------------------------------------------------------------
# build_session_inputs — three tiers enter the execution path
# (merge_skill_registries actually gets called)
# ---------------------------------------------------------------------------


def _inputs(ws: Path, **kwargs):
    return build_session_inputs(
        workspace_dir=ws,
        system_prompt="p",
        allowed_tools=frozenset({"read_file"}),
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        **kwargs,
    )


def test_builder_merges_three_tiers_into_registry(tmp_path: Path) -> None:
    """build_session_inputs merges builtin + global into workspace-local; workspace wins."""
    builtin = tmp_path / "builtin"
    write_skill_raw(builtin, "review", _skill_body("review", "builtin review"))
    glob = tmp_path / "global"
    write_skill_raw(glob, "edit", _skill_body("edit", "GLOBAL edit"))
    ws = tmp_path / "ws"
    ws_local = ws / ".noeta" / "skills"
    write_skill_raw(ws_local, "edit", _skill_body("edit", "WORKSPACE edit"))

    inputs = _inputs(
        ws, builtin_skills_dirs=(builtin,), global_skills_dir=glob
    )
    names = set(inputs.skill_registry.names())
    assert names == {"review", "edit"}
    edit = inputs.skill_registry.get("edit")
    assert edit is not None
    assert edit.description == "WORKSPACE edit"


def test_builder_no_global_tiers_unchanged(tmp_path: Path) -> None:
    """No lower-tier dirs passed: registry holds only the workspace-local tier (single-tier behavior unchanged)."""
    ws = tmp_path / "ws"
    ws_local = ws / ".noeta" / "skills"
    write_skill_raw(ws_local, "tidy", _skill_body("tidy", "workspace tidy"))

    inputs = _inputs(ws)
    assert set(inputs.skill_registry.names()) == {"tidy"}


def test_builtin_pack_enters_execution_path(tmp_path: Path) -> None:
    """``BUILTIN_SKILLS_DIR`` actually enters the execution path (previously only loaded, never merged).

    Feed the real builtin pack to build_session_inputs as a lower tier: even with empty
    workspace and global tiers, all builtin skills are present in the final registry.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    inputs = _inputs(ws, builtin_skills_dirs=(BUILTIN_SKILLS_DIR,))
    assert set(load_builtin_skills().names()).issubset(
        set(inputs.skill_registry.names())
    )


# ---------------------------------------------------------------------------
# Single global memory tier — independent of the workspace
# ---------------------------------------------------------------------------


def test_memory_root_is_global_not_workspace_derived(tmp_path: Path) -> None:
    """Memory uses global_memory_dir, not ``<workspace>/.noeta/memories``."""
    glob_mem = tmp_path / "global-memories"
    glob_mem.mkdir()
    (glob_mem / "deploy.md").write_text("# Deploy\n", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    # Also put a memory under the workspace that used to be picked up, to prove it is now ignored.
    ws_mem = ws / ".noeta" / "memories"
    ws_mem.mkdir(parents=True)
    (ws_mem / "stale.md").write_text("# Stale\n", encoding="utf-8")

    inputs = _inputs(ws, memory_enabled=True, global_memory_dir=glob_mem)
    assert inputs.memory_store is not None
    assert inputs.memory_store.root == glob_mem
    names = [n for n, _, _ in inputs.memory_entries]
    assert names == ["deploy"]
    assert "stale" not in names


def test_memory_unchanged_across_workspace_switch(tmp_path: Path) -> None:
    """Switching workspaces leaves the memory directory and contents unchanged (single global tier)."""
    glob_mem = tmp_path / "global-memories"
    glob_mem.mkdir()
    (glob_mem / "shared.md").write_text("# Shared\n", encoding="utf-8")

    ws_a = tmp_path / "ws-a"
    ws_a.mkdir()
    ws_b = tmp_path / "ws-b"
    ws_b.mkdir()

    inputs_a = _inputs(ws_a, memory_enabled=True, global_memory_dir=glob_mem)
    inputs_b = _inputs(ws_b, memory_enabled=True, global_memory_dir=glob_mem)

    assert inputs_a.memory_store is not None and inputs_b.memory_store is not None
    assert inputs_a.memory_store.root == inputs_b.memory_store.root == glob_mem
    assert (
        [n for n, _, _ in inputs_a.memory_entries]
        == [n for n, _, _ in inputs_b.memory_entries]
        == ["shared"]
    )


def test_memory_dir_override_beats_global(tmp_path: Path) -> None:
    """An explicit memory_dir override takes priority over global_memory_dir."""
    glob_mem = tmp_path / "global-memories"
    glob_mem.mkdir()
    (glob_mem / "global-one.md").write_text("# G\n", encoding="utf-8")
    override = tmp_path / "override-memories"
    override.mkdir()
    (override / "override-one.md").write_text("# O\n", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()

    inputs = _inputs(
        ws,
        memory_enabled=True,
        memory_dir=override,
        global_memory_dir=glob_mem,
    )
    assert inputs.memory_store is not None
    assert inputs.memory_store.root == override
    assert [n for n, _, _ in inputs.memory_entries] == ["override-one"]


def test_load_memory_store_takes_root_directly(tmp_path: Path) -> None:
    """load_memory_store takes the global root directly, no longer derived from the workspace."""
    root = tmp_path / "anywhere"
    store = load_memory_store(root=root)
    assert store.root == root


# ---------------------------------------------------------------------------
# Default global directory resolution (agent layer can override; defaults under ~/.noeta)
# ---------------------------------------------------------------------------


def test_default_global_dirs_resolve_under_noeta_home() -> None:
    """Default global skills/memory dirs are pinned to ``~/.noeta/{skills,memories}``.

    These two names bind to the source constants at this module's import time (computed in
    their defining modules via ``Path("~/.noeta/...").expanduser()``), so they are not
    affected by conftest's hermetic redirect of *module attributes* — hence they stay the
    real defaults under home, proving the defaults are independent of the workspace.
    """
    home_noeta = Path("~/.noeta").expanduser()
    assert DEFAULT_GLOBAL_SKILLS_DIR == home_noeta / "skills"
    assert DEFAULT_GLOBAL_MEMORY_DIR == home_noeta / "memories"
