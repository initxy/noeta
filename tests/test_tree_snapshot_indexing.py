"""Issue 46 — skill indexing batches container IO into one tree snapshot.

Sandbox-mode ``SkillIndexer`` used one container round-trip per file
(``rglob`` / ``is_file`` / ``read_text``), so ``seed_start`` scaled linearly
with the number of files under the skill mounts (~minutes at a few dozen
skills). ``load_workspace_skills`` now takes ONE ``ExecEnv.tree_snapshot``
spanning every tier and hands it to each per-tier indexer.

Pinned here:

* ``LocalExecEnv.tree_snapshot`` — the host reference semantics of the batch
  primitive (regular files only, missing roots skipped, named contents inlined
  byte-exact).
* The prefetched indexer path produces a registry **equal** to the legacy
  per-file container path (same descriptions — source_path, resources,
  metadata — so the rendered ``semi_stable`` bytes cannot drift).
* ``load_workspace_skills`` performs exactly one snapshot call spanning all
  tiers and NO per-file container IO when the backend supports it.
* Fallbacks: an ExecEnv without ``tree_snapshot`` (duck-typed fakes / older
  backends) or a snapshot that raises falls back to the legacy per-file path —
  correctness over speed.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from pathlib import Path
from typing import Iterable

from noeta.context.skills import SkillIndexer
from noeta.execution.skills import load_workspace_skills
from noeta.tools.fs.exec_env import LocalExecEnv, TreeSnapshot


_REVIEW = b"---\nname: review\ndescription: builtin review\n---\n\nReview body.\n"
_REVIEW_WS = b"---\nname: review\ndescription: workspace review\n---\n\nShadow body.\n"
_DEPLOY = b"---\nname: deploy\ndescription: global deploy\n---\n\nDeploy body.\n"
_TIDY = b"---\nname: tidy\ndescription: workspace tidy\n---\n\nTidy body.\n"


class CountingContainer:
    """In-memory container fs exposing the legacy per-file ExecEnv surface,
    counting every call so tests can assert which path the indexer took."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.calls: Counter[str] = Counter()

    def is_dir(self, path: Path) -> bool:
        self.calls["is_dir"] += 1
        prefix = str(path).rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self.files)

    def is_file(self, path: Path) -> bool:
        self.calls["is_file"] += 1
        return str(path) in self.files

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        self.calls["read_text"] += 1
        try:
            return self.files[str(path)].decode(encoding)
        except KeyError as exc:
            raise FileNotFoundError(str(path)) from exc

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        self.calls["rglob"] += 1
        prefix = str(base).rstrip("/") + "/"
        return [
            Path(p) for p in self.files
            if p.startswith(prefix)
            and (pattern == "*" or fnmatch.fnmatch(Path(p).name, pattern))
        ]

    def legacy_io_calls(self) -> int:
        return sum(
            self.calls[m] for m in ("is_dir", "is_file", "read_text", "rglob")
        )


class SnapshotContainer(CountingContainer):
    """A CountingContainer that also implements the batch primitive."""

    def __init__(self, files: dict[str, bytes], *, fail: bool = False) -> None:
        super().__init__(files)
        self._fail = fail
        self.snapshot_requests: list[tuple[tuple[Path, ...], str]] = []

    def tree_snapshot(
        self, roots: tuple[Path, ...], *, content_name: str
    ) -> TreeSnapshot:
        self.calls["tree_snapshot"] += 1
        self.snapshot_requests.append((tuple(roots), content_name))
        if self._fail:
            raise OSError("container walk failed")
        prefixes = [str(r).rstrip("/") + "/" for r in roots]
        listed = sorted(
            Path(p) for p in self.files
            if any(p.startswith(prefix) for prefix in prefixes)
        )
        return TreeSnapshot(
            tuple(listed),
            {p: self.files[str(p)] for p in listed if p.name == content_name},
        )


_TIER_FILES = {
    "/opt/noeta/skills/builtin/review/SKILL.md": _REVIEW,
    "/opt/noeta/skills/builtin/review/references/guide.md": b"guide\n",
    "/opt/noeta/skills/global/deploy/SKILL.md": _DEPLOY,
    "/opt/noeta/skills/global/deploy/scripts/run.sh": b"echo hi\n",
    "/workspace/.noeta/skills/review/SKILL.md": _REVIEW_WS,
    "/workspace/.noeta/skills/tidy/SKILL.md": _TIDY,
}

_BUILTIN = Path("/opt/noeta/skills/builtin")
_GLOBAL = Path("/opt/noeta/skills/global")


# --------------------------------------------------------------------------- #
# LocalExecEnv.tree_snapshot — host reference semantics
# --------------------------------------------------------------------------- #


def test_local_tree_snapshot_lists_files_and_inlines_named_contents(
    tmp_path: Path,
) -> None:
    skill_md = b"---\nname: demo\ndescription: d\n---\n\nBody.\n"
    root_a = tmp_path / "a"
    (root_a / "demo").mkdir(parents=True)
    (root_a / "demo" / "SKILL.md").write_bytes(skill_md)
    (root_a / "demo" / "notes.txt").write_bytes(b"n\n")
    root_b = tmp_path / "b"
    root_b.mkdir()
    (root_b / "loose.md").write_bytes(b"x\n")

    snap = LocalExecEnv().tree_snapshot(
        [root_a, root_b, tmp_path / "missing"], content_name="SKILL.md"
    )
    assert snap.files == tuple(sorted([
        root_a / "demo" / "SKILL.md",
        root_a / "demo" / "notes.txt",
        root_b / "loose.md",
    ]))
    # Named contents come back byte-exact; other files are listed only.
    assert snap.contents == {root_a / "demo" / "SKILL.md": skill_md}


def test_local_tree_snapshot_skips_directories(tmp_path: Path) -> None:
    (tmp_path / "pack" / "empty-dir").mkdir(parents=True)
    (tmp_path / "pack" / "f.md").write_bytes(b"f\n")
    snap = LocalExecEnv().tree_snapshot([tmp_path], content_name="SKILL.md")
    assert snap.files == (tmp_path / "pack" / "f.md",)


def test_local_tree_snapshot_follows_dir_symlinks_without_looping(
    tmp_path: Path,
) -> None:
    # Same semantics as the sandbox ``find -L`` and the host indexer walk:
    # a symlinked skill directory is traversed; a symlink cycle terminates.
    import os

    real = tmp_path / "outside" / "linked-skill"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_bytes(b"---\nname: linked\n---\nL\n")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(real, root / "linked")
    os.symlink(root, root / "cycle")  # would loop without the realpath guard

    snap = LocalExecEnv().tree_snapshot([root], content_name="SKILL.md")
    assert root / "linked" / "SKILL.md" in snap.contents


# --------------------------------------------------------------------------- #
# prefetched indexer path ≡ legacy per-file container path
# --------------------------------------------------------------------------- #


def test_prefetched_registry_equals_legacy_container_registry() -> None:
    legacy = SkillIndexer(_BUILTIN, exec_env=CountingContainer(_TIER_FILES)).index()

    # The prefetched path must never touch the per-file surface…
    container = CountingContainer(_TIER_FILES)
    snapshot = SnapshotContainer(_TIER_FILES).tree_snapshot(
        (_BUILTIN, _GLOBAL, Path("/workspace/.noeta/skills")),
        content_name="SKILL.md",
    )
    prefetched = SkillIndexer(
        _BUILTIN, exec_env=container, prefetched=snapshot
    ).index()
    assert container.legacy_io_calls() == 0

    # …and yields byte-equal descriptions (dataclass equality covers name /
    # body / source_path / metadata / resources — the rendered bytes).
    assert prefetched.names() == legacy.names()
    for name in legacy.names():
        assert prefetched.get(name) == legacy.get(name)
    assert prefetched.get("review").resources == ("references/guide.md",)


# --------------------------------------------------------------------------- #
# load_workspace_skills — one snapshot spanning every tier, zero per-file IO
# --------------------------------------------------------------------------- #


def test_load_workspace_skills_takes_one_snapshot_and_no_per_file_io() -> None:
    container = SnapshotContainer(_TIER_FILES)
    registry = load_workspace_skills(
        Path("/workspace"),
        lower_skill_dirs=[_BUILTIN, _GLOBAL],
        exec_env=container,
    )
    assert container.calls["tree_snapshot"] == 1
    assert container.legacy_io_calls() == 0
    assert container.snapshot_requests == [(
        (_BUILTIN, _GLOBAL, Path("/workspace/.noeta/skills")),
        "SKILL.md",
    )]
    # Merge semantics are unchanged: builtin < global < workspace, and the
    # workspace-local ``review`` shadows the builtin one.
    assert set(registry.names()) == {"review", "deploy", "tidy"}
    assert registry.get("review").description == "workspace review"
    assert registry.get("deploy").source_path == Path(
        "/opt/noeta/skills/global/deploy/SKILL.md"
    )
    assert registry.get("deploy").resources == ("scripts/run.sh",)


def test_load_workspace_skills_falls_back_without_tree_snapshot() -> None:
    # A duck-typed ExecEnv without the batch primitive (older backends, test
    # fakes) keeps working on the legacy per-file path.
    container = CountingContainer(_TIER_FILES)
    registry = load_workspace_skills(
        Path("/workspace"),
        lower_skill_dirs=[_BUILTIN, _GLOBAL],
        exec_env=container,
    )
    assert set(registry.names()) == {"review", "deploy", "tidy"}
    assert container.legacy_io_calls() > 0


def test_load_workspace_skills_falls_back_when_snapshot_raises() -> None:
    container = SnapshotContainer(_TIER_FILES, fail=True)
    registry = load_workspace_skills(
        Path("/workspace"),
        lower_skill_dirs=[_BUILTIN, _GLOBAL],
        exec_env=container,
    )
    assert container.calls["tree_snapshot"] == 1
    # Fell back: the registry is still complete, via per-file container IO.
    assert set(registry.names()) == {"review", "deploy", "tidy"}
    assert container.legacy_io_calls() > 0
