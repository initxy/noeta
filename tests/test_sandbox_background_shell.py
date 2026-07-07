"""T8 — a sandbox backend refuses a background shell launch cleanly (D5).

``shell_run(run_in_background=True)`` hands off to the host background runner,
which spawns a detached HOST subprocess — it cannot reach into a container, and
AIO has no durable job handle (v1). So under a sandbox backend the launch is
refused with a clear tool error instead of silently running on the wrong
machine; the local backend keeps the existing background path.

Teardown (D6) is host-level: ``Client.shutdown`` reaps the sandbox backend (T5).
Per-conversation teardown is deliberately deferred — v1 shares one container per
host, so tearing it down when a single conversation closes would break every
other live conversation on the host (a v2 per-container concern).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.fs import ShellMode, build_fs_tools
from noeta.tools.fs._subprocess import _RunOutcome
from noeta.tools.fs._workspace import WorkspaceRoot
from noeta.tools.fs.exec_env import AioSandboxExecEnv, LocalExecEnv


class _SandboxLike:
    """A backend that declines background (like ``AioSandboxExecEnv``)."""

    supports_background = False

    def run_argv(self, argv, *, cwd, timeout_s, output_cap, runner=None) -> _RunOutcome:
        return _RunOutcome(0, 1, b"ran", b"", False, False, False)

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
    return tools["shell_run"]


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
    # only the BACKGROUND launch is refused — foreground still routes to the
    # backend's run_argv.
    tool = _shell_tool(_SandboxLike())
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    result = tool.invoke({"command": "echo hi"}, ctx=ctx)
    assert result.success
