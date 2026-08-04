"""A sandbox backend refuses a background shell launch cleanly.

``shell_run(run_in_background=True)`` hands off to the host background runner,
which spawns a detached HOST subprocess — it cannot reach into a container, and
the sandbox API exposes no durable job handle to poll instead. Under a sandbox
backend the launch is therefore refused with a clear tool error rather than
silently running the command on the wrong machine. Only the background path is
refused: foreground still routes through the backend, and the local backend
keeps both.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.builtins.fs.impl import build_fs_tools
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.subproc import RunOutcome
from noeta.runtime.workspace import WorkspaceRoot
from noeta.builtins.sandbox.impl.exec_env import AioSandboxExecEnv
from noeta.runtime.exec_env import LocalExecEnv


class _SandboxLike:
    """A backend that declines background (like ``AioSandboxExecEnv``)."""

    supports_background = False

    def run_argv(self, argv, *, cwd, timeout_s, output_cap, runner=None) -> RunOutcome:
        return RunOutcome(0, 1, b"ran", b"", False, False, False)

    # minimal remaining ExecEnv surface (unused here)
    def read_bytes(self, path: Path) -> bytes:
        raise FileNotFoundError(str(path))

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        raise FileNotFoundError(str(path))

    def write_bytes(self, path: Path, body: bytes) -> None: ...
    def create_exclusive(self, path: Path, body: bytes) -> None: ...
    def unlink(self, path: Path) -> None: ...
    def mkdir(self, path: Path) -> None: ...
    def exists(self, path: Path) -> bool:
        return False

    def is_file(self, path: Path) -> bool:
        return False

    def is_dir(self, path: Path) -> bool:
        return True

    def is_symlink(self, path: Path) -> bool:
        return False

    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        return []

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        return []


def _shell_tool(exec_env):
    ws = WorkspaceRoot.for_container("/c/ws")
    tools = build_fs_tools(ws, shell_mode=ShellMode.ARBITRARY, exec_env=exec_env)
    return tools["Bash"]


def test_concrete_backends_report_background_capability() -> None:
    assert LocalExecEnv().supports_background is True
    assert AioSandboxExecEnv(base_url="http://box:8080").supports_background is False


def test_sandbox_refuses_background_shell() -> None:
    tool = _shell_tool(_SandboxLike())
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    result = tool.invoke(
        {"command": "sleep 100", "run_in_background": True}, ctx=ctx
    )
    assert not result.success
    assert "not supported in sandbox mode" in (result.summary or "")


def test_sandbox_foreground_shell_still_runs() -> None:
    tool = _shell_tool(_SandboxLike())
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    result = tool.invoke({"command": "echo hi"}, ctx=ctx)
    assert result.success
