"""Tier 2 widening — skill index / script / workspace loaders read the container.

In sandbox mode every one of these reads/executes THROUGH the session's ExecEnv
(the container), not the host filesystem, and the paths it works with are
CONTAINER paths. These drive each widened seam with an in-memory container fake
so no daemon runs and no socket opens:

* ``SkillIndexer(exec_env=...)`` finds SKILL.md via the container's recursive
  glob, reads it through the container, and its ``source_path`` (hence the
  rendered base directory) is the container path;
* ``RunSkillScriptTool(exec_env=...)`` reads the script bytes + runs the
  interpreter through the container, cwd = the container workspace root;
* ``load_instructions`` / ``load_environment`` / ``load_project_shell_allowlist``
  read their files THROUGH the container (fixing the v1 "container path read
  against the host FS" bug).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterable, Optional

from noeta.context.skills import SkillIndexer
from noeta.execution.environment import load_environment
from noeta.execution.instructions import load_instructions
from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.fs._subprocess import _RunOutcome
from noeta.tools.fs._workspace import WorkspaceRoot
from noeta.tools.fs.shell import load_project_shell_allowlist
from noeta.tools.fs.skill_script import RunSkillScriptTool


class FakeContainer:
    """An in-memory container filesystem exposing the ExecEnv surface."""

    def __init__(
        self,
        files: Optional[dict[str, bytes]] = None,
        run_results: Optional[dict[tuple[str, ...], _RunOutcome]] = None,
    ) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.run_calls: list[tuple[list[str], str]] = []
        self._run_results = run_results or {}

    def _under(self, base: Path) -> list[Path]:
        prefix = str(base).rstrip("/") + "/"
        return [Path(p) for p in self.files if p.startswith(prefix)]

    def is_dir(self, path: Path) -> bool:
        prefix = str(path).rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self.files)

    def is_file(self, path: Path) -> bool:
        return str(path) in self.files

    def is_symlink(self, path: Path) -> bool:
        return False

    def exists(self, path: Path) -> bool:
        return self.is_file(path) or self.is_dir(path)

    def read_bytes(self, path: Path) -> bytes:
        try:
            return self.files[str(path)]
        except KeyError as exc:
            raise FileNotFoundError(str(path)) from exc

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        return [
            p for p in self._under(base)
            if pattern == "*" or fnmatch.fnmatch(p.name, pattern)
        ]

    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        return []

    def write_bytes(self, path: Path, body: bytes) -> None:
        self.files[str(path)] = body

    def create_exclusive(self, path: Path, body: bytes) -> None:
        self.files[str(path)] = body

    def unlink(self, path: Path) -> None:
        self.files.pop(str(path), None)

    def mkdir(self, path: Path) -> None: ...

    def run_argv(self, argv, *, cwd, timeout_s, output_cap, runner=None) -> _RunOutcome:
        self.run_calls.append((list(argv), str(cwd)))
        result = self._run_results.get(tuple(argv))
        if result is not None:
            return result
        return _RunOutcome(0, 1, b"ok", b"", False, False, False)


# --------------------------------------------------------------------------- #
# S6 — skill indexer through the container
# --------------------------------------------------------------------------- #


_SKILL_MD = (
    b"---\nname: demo\ndescription: a demo skill\n---\n\nThe body of the demo skill.\n"
)


def test_indexer_reads_skill_md_through_container() -> None:
    container = FakeContainer(
        {
            "/opt/noeta/skills/builtin/demo/SKILL.md": _SKILL_MD,
            "/opt/noeta/skills/builtin/demo/scripts/run.sh": b"echo hi\n",
        }
    )
    registry = SkillIndexer(
        Path("/opt/noeta/skills/builtin"), exec_env=container
    ).index()
    desc = registry.get("demo")
    assert desc is not None
    # source_path is the CONTAINER path (so the rendered base directory is one
    # the model can read inside the container).
    assert desc.source_path == Path("/opt/noeta/skills/builtin/demo/SKILL.md")
    assert "scripts/run.sh" in desc.resources
    # the rendered base directory line points at the container dir
    rendered = registry.render(["demo"]).messages[0].content[0].text
    assert "Base directory for this skill: /opt/noeta/skills/builtin/demo" in rendered


def test_indexer_empty_when_root_absent_in_container() -> None:
    container = FakeContainer({})
    registry = SkillIndexer(Path("/opt/noeta/skills/global"), exec_env=container).index()
    assert registry.names() == ()


# --------------------------------------------------------------------------- #
# S7 — run_skill_script through the container
# --------------------------------------------------------------------------- #


def test_run_skill_script_reads_and_runs_in_container() -> None:
    root = Path("/opt/noeta/skills/builtin/demo")
    container = FakeContainer(
        {str(root / "scripts/run.sh"): b"echo hello\n"},
        run_results={
            ("bash", str(root / "scripts/run.sh")): _RunOutcome(
                0, 5, b"hello\n", b"", False, False, False
            )
        },
    )
    tool = RunSkillScriptTool(
        workspace=WorkspaceRoot.for_container(Path("/workspace")),
        scripts=(("demo", "scripts/run.sh", root),),
        exec_env=container,
    )
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    result = tool.invoke(
        {"skill": "demo", "relpath": "scripts/run.sh"}, ctx=ctx
    )
    assert result.success
    assert result.output["exit_code"] == 0
    # executed INSIDE the container, cwd = the container workspace root
    argv, cwd = container.run_calls[0]
    assert argv == ["bash", str(root / "scripts/run.sh")]
    assert cwd == "/workspace"


def test_run_skill_script_rejects_escape_lexically_in_container() -> None:
    root = Path("/opt/noeta/skills/builtin/demo")
    container = FakeContainer({str(root / "scripts/run.sh"): b"x\n"})
    tool = RunSkillScriptTool(
        workspace=WorkspaceRoot.for_container(Path("/workspace")),
        scripts=(("demo", "../evil.sh", root),),
        exec_env=container,
    )
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    # "../evil.sh" is not a discovered script → refused before any container read.
    result = tool.invoke({"skill": "demo", "relpath": "../evil.sh"}, ctx=ctx)
    assert not result.success


# --------------------------------------------------------------------------- #
# S8 — workspace loaders through the container (fix v1 bug)
# --------------------------------------------------------------------------- #


def test_load_instructions_reads_container_file() -> None:
    container = FakeContainer({"/workspace/NOETA.md": b"# Project rules\n"})
    snap = load_instructions(Path("/workspace"), exec_env=container)
    assert snap is not None
    assert snap.name == "NOETA.md"
    assert "Project rules" in snap.text


def test_load_instructions_none_when_absent_in_container() -> None:
    container = FakeContainer({})
    assert load_instructions(Path("/workspace"), exec_env=container) is None


def test_load_environment_probes_git_in_container() -> None:
    container = FakeContainer(
        {"/workspace/.git/HEAD": b"ref: refs/heads/main\n"},
        run_results={
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): _RunOutcome(
                0, 1, b"main\n", b"", False, False, False
            ),
            ("git", "status", "--short"): _RunOutcome(
                0, 1, b" M file.py\n", b"", False, False, False
            ),
        },
    )
    snap = load_environment(Path("/workspace"), exec_env=container)
    assert snap.is_git_repo is True
    assert snap.git_branch == "main"
    assert "file.py" in snap.git_status
    assert snap.workspace_display == "/workspace"


def test_load_shell_allowlist_reads_container_file() -> None:
    container = FakeContainer(
        {"/workspace/.noeta/shell-allowlist.json": b'[{"program": "ls"}]'}
    )
    rules = load_project_shell_allowlist(Path("/workspace"), exec_env=container)
    assert rules == ({"program": "ls"},)


def test_load_shell_allowlist_empty_when_absent_in_container() -> None:
    container = FakeContainer({})
    assert load_project_shell_allowlist(Path("/workspace"), exec_env=container) == ()
