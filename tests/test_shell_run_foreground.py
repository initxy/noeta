"""``shell_run`` foreground execution — assert real exit code AND stdout bytes.

The existing foreground ``shell_run`` tests check the permission gate, the
result *shape* (``returncode`` present, ``stdout_tail`` key exists), or a
stub-provider canned call — none pin the actual executed *output*, so mutating
``run_argv``'s stdout would not fail them. These drive a real subprocess through
the default ``LocalExecEnv`` and assert on the produced bytes, so the seam's
happy path (tool → ``exec_env.run_argv`` → captured stdout/exit) is covered.
"""

from __future__ import annotations

from pathlib import Path

from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.fs.shell import ShellMode, ShellRunTool
from noeta.tools.fs._workspace import WorkspaceRoot


def _tool_and_ctx(tmp_path: Path) -> tuple[ShellRunTool, ToolContext]:
    ws = WorkspaceRoot.from_path(tmp_path)
    tool = ShellRunTool(workspace=ws, mode=ShellMode.ARBITRARY)
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    return tool, ctx


def test_foreground_echo_captures_stdout(tmp_path: Path) -> None:
    tool, ctx = _tool_and_ctx(tmp_path)
    result = tool.invoke({"command": "printf hello-noeta"}, ctx)
    assert result.success
    assert result.output["returncode"] == 0
    assert result.output["stdout_tail"] == "hello-noeta"


def test_foreground_python_computes_and_returns_stdout(tmp_path: Path) -> None:
    tool, ctx = _tool_and_ctx(tmp_path)
    result = tool.invoke({"command": 'python3 -c "print(6*7)"'}, ctx)
    assert result.success
    assert result.output["returncode"] == 0
    assert "42" in result.output["stdout_tail"]


def test_foreground_nonzero_exit_is_reported(tmp_path: Path) -> None:
    tool, ctx = _tool_and_ctx(tmp_path)
    result = tool.invoke({"command": "sh -c 'exit 3'"}, ctx)
    # the tool still "succeeds" (it ran); the command's exit code is surfaced.
    assert result.output["returncode"] == 3


def test_foreground_runs_in_workspace_cwd(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("x")
    tool, ctx = _tool_and_ctx(tmp_path)
    result = tool.invoke({"command": "ls"}, ctx)
    assert result.success
    assert "marker.txt" in result.output["stdout_tail"]
