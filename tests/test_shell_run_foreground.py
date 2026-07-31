"""``shell_run`` foreground execution — real exit code AND real stdout bytes.

Shape assertions (``returncode`` present, ``stdout_tail`` key exists) stay green
even when the seam hands back nothing the command actually produced. These drive
a real subprocess through the default ``LocalExecEnv`` and assert on the bytes,
covering the whole happy path: tool → ``exec_env.run_argv`` → captured
stdout/exit code, with the workspace root as cwd.
"""

from __future__ import annotations

from pathlib import Path

from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.builtins.fs.impl.shell import ShellRunTool
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import WorkspaceRoot


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
    # A nonzero command exit is not a tool failure: the tool ran, and the model
    # needs the code to decide what to do next.
    assert result.output["returncode"] == 3


def test_foreground_runs_in_workspace_cwd(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("x")
    tool, ctx = _tool_and_ctx(tmp_path)
    result = tool.invoke({"command": "ls"}, ctx)
    assert result.success
    assert "marker.txt" in result.output["stdout_tail"]
