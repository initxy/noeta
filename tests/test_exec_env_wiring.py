"""Wiring the ExecEnv seam into config and the tool builder.

Four seams have to line up before a sandbox backend is reachable at all: the
lexical (container) ``WorkspaceRoot``, which contains paths without ever
consulting the host filesystem; ``build_fs_tools(exec_env=...)`` threading one
backend into every fs tool; the ``SandboxExecEnvConfig`` / ``HostConfig.exec_env``
config surface; and ``build_session_inputs(exec_env=...)`` picking the lexical
workspace and routing the pack's IO through the injected backend — all without
perturbing the default host path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import pytest

from tests._session_inputs import default_factory_kwargs
from noeta.client.host_config import HostConfig, SandboxExecEnvConfig
from noeta.client.parts import derive_compaction_config
from noeta.execution.builder import build_session_inputs
from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.builtins.fs.impl import build_fs_tools
from noeta.runtime.subproc import RunOutcome
from noeta.runtime.workspace import WorkspaceEscape, WorkspaceRoot
from noeta.runtime.exec_env import ExecEnv, LocalExecEnv

from tests._sdk_session import coding_replay_budget


# --------------------------------------------------------------------------- #
# lexical (container) WorkspaceRoot
# --------------------------------------------------------------------------- #


def test_for_container_is_lexical_and_touches_no_host_fs() -> None:
    # A path that does not exist on the host — from_path would raise, but a
    # container root is never checked against the host FS.
    ws = WorkspaceRoot.for_container("/home/gem/workspace")
    assert ws.lexical is True
    assert ws.root == Path("/home/gem/workspace")


def test_for_container_normalises_lexically() -> None:
    ws = WorkspaceRoot.for_container("/home/gem/../gem/workspace/")
    assert ws.root == Path("/home/gem/workspace")


def test_for_container_requires_absolute() -> None:
    with pytest.raises(WorkspaceEscape):
        WorkspaceRoot.for_container("relative/workspace")


def test_lexical_resolve_joins_relative_under_root() -> None:
    ws = WorkspaceRoot.for_container("/home/gem/workspace")
    assert ws.resolve("src/main.py") == Path("/home/gem/workspace/src/main.py")


def test_lexical_resolve_collapses_dotdot_within_root() -> None:
    ws = WorkspaceRoot.for_container("/home/gem/workspace")
    assert ws.resolve("src/../pkg/x.py") == Path("/home/gem/workspace/pkg/x.py")


def test_lexical_resolve_rejects_dotdot_escape() -> None:
    ws = WorkspaceRoot.for_container("/home/gem/workspace")
    with pytest.raises(WorkspaceEscape):
        ws.resolve("../secrets")


def test_lexical_resolve_rejects_absolute_outside() -> None:
    ws = WorkspaceRoot.for_container("/home/gem/workspace")
    with pytest.raises(WorkspaceEscape):
        ws.resolve("/etc/passwd")


def test_lexical_resolve_allows_absolute_inside() -> None:
    ws = WorkspaceRoot.for_container("/home/gem/workspace")
    assert ws.resolve("/home/gem/workspace/a/b") == Path("/home/gem/workspace/a/b")


def test_lexical_relative_still_works() -> None:
    ws = WorkspaceRoot.for_container("/home/gem/workspace")
    assert ws.relative(Path("/home/gem/workspace/a/b.py")) == "a/b.py"


# --------------------------------------------------------------------------- #
# a recording ExecEnv fake — proves the pack routes IO through the seam
# --------------------------------------------------------------------------- #


class RecordingExecEnv:
    """A minimal :class:`ExecEnv` that records every read and canned-replies."""

    def __init__(self, files: Optional[dict[str, bytes]] = None) -> None:
        self.files = dict(files or {})
        self.reads: list[str] = []

    def read_bytes(self, path: Path) -> bytes:
        self.reads.append(str(path))
        return self.files[str(path)]

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def write_bytes(self, path: Path, body: bytes) -> None:
        self.files[str(path)] = body

    def create_exclusive(self, path: Path, body: bytes) -> None:
        self.files[str(path)] = body

    def unlink(self, path: Path) -> None:
        self.files.pop(str(path), None)

    def exists(self, path: Path) -> bool:
        return str(path) in self.files

    def is_file(self, path: Path) -> bool:
        return str(path) in self.files

    def is_dir(self, path: Path) -> bool:
        return True

    def is_symlink(self, path: Path) -> bool:
        return False

    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        return []

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        return []

    def run_argv(self, argv, *, cwd, timeout_s, output_cap, runner=None) -> RunOutcome:
        return RunOutcome(0, 1, b"ran", b"", False, False, False)


# --------------------------------------------------------------------------- #
# build_fs_tools(exec_env=...)
# --------------------------------------------------------------------------- #


def test_build_fs_tools_default_uses_local_exec_env(tmp_path: Path) -> None:
    ws = WorkspaceRoot.from_path(tmp_path)
    tools = build_fs_tools(ws)
    assert isinstance(tools["Read"].exec_env, LocalExecEnv)
    # one shared instance across the pack
    assert tools["Read"].exec_env is tools["Write"].exec_env


def test_build_fs_tools_threads_injected_backend() -> None:
    ws = WorkspaceRoot.for_container("/c/ws")
    fake = RecordingExecEnv()
    tools = build_fs_tools(ws, exec_env=fake)
    for name in ("Read", "Glob", "Grep", "Edit", "Write", "shell_run"):
        assert tools[name].exec_env is fake


def test_build_fs_tools_pack_reads_through_injected_backend() -> None:
    ws = WorkspaceRoot.for_container("/c/ws")
    fake = RecordingExecEnv({"/c/ws/f.txt": b"remote-bytes"})
    tools = build_fs_tools(ws, exec_env=fake)
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    result = tools["Read"].invoke({"file_path": "f.txt"}, ctx=ctx)
    assert result.success
    assert fake.reads == ["/c/ws/f.txt"]


# --------------------------------------------------------------------------- #
# SandboxExecEnvConfig + HostConfig.exec_env
# --------------------------------------------------------------------------- #


def test_sandbox_config_defaults() -> None:
    cfg = SandboxExecEnvConfig(base_url="http://box:8080")
    assert cfg.api_key_env == "SANDBOX_API_KEY"
    assert cfg.workdir == "/workspace"


def test_sandbox_config_rejects_the_removed_provision_field() -> None:
    """``SandboxExecEnvConfig`` carries no ``provision`` knob — per-session
    provisioning is the ``SandboxProvider`` seam. A caller who passes one must
    fail loudly instead of quietly getting attach-only behaviour."""
    with pytest.raises(TypeError):
        SandboxExecEnvConfig(  # type: ignore[call-arg]
            base_url="http://box:8080", provision="eager"
        )


def test_sandbox_config_resolve_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SandboxExecEnvConfig(base_url="http://box:8080", api_key_env="MY_KEY")
    monkeypatch.delenv("MY_KEY", raising=False)
    assert cfg.resolve_api_key() is None
    monkeypatch.setenv("MY_KEY", "s3cr3t")
    assert cfg.resolve_api_key() == "s3cr3t"


def test_host_config_exec_env_defaults_none() -> None:
    assert HostConfig().exec_env is None
    cfg = SandboxExecEnvConfig(base_url="http://box:8080")
    assert HostConfig(exec_env=cfg).exec_env is cfg


# --------------------------------------------------------------------------- #
# build_session_inputs(exec_env=...) — the reachable seam
# --------------------------------------------------------------------------- #

_SYSTEM = "you are a coding agent"


def _session(*, workspace_dir: Path, exec_env: ExecEnv | None):
    return build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=workspace_dir,
        system_prompt=_SYSTEM,
        allowed_tools=frozenset({"Read", "Write", "Edit", "shell_run"}),
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=derive_compaction_config("stub-model"),
        budget=coding_replay_budget(None),
        exec_env=exec_env,
    )


def test_session_with_sandbox_backend_uses_lexical_container_workspace() -> None:
    # A container path that does NOT exist on the host: the build must not do a
    # host existence / realpath check (from_path would raise) — it uses the
    # lexical container root instead.
    fake = RecordingExecEnv()
    inputs = _session(workspace_dir=Path("/home/gem/workspace"), exec_env=fake)
    read = inputs.tools["Read"]
    assert read.workspace.lexical is True
    assert read.workspace.root == Path("/home/gem/workspace")
    assert read.exec_env is fake


def test_default_session_keeps_host_workspace_and_local_backend(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    inputs = _session(workspace_dir=ws, exec_env=None)
    read = inputs.tools["Read"]
    assert read.workspace.lexical is False
    assert isinstance(read.exec_env, LocalExecEnv)


def test_session_pack_reads_through_sandbox_backend() -> None:
    fake = RecordingExecEnv({"/home/gem/workspace/note.md": b"in-container"})
    inputs = _session(workspace_dir=Path("/home/gem/workspace"), exec_env=fake)
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    result = inputs.tools["Read"].invoke({"file_path": "note.md"}, ctx=ctx)
    assert result.success
    assert fake.reads == ["/home/gem/workspace/note.md"]
