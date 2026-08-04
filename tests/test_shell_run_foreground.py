"""``Bash`` foreground execution — real exit code AND real stdout bytes.

These drive a real subprocess through the default ``LocalExecEnv`` and assert
on the plain-text output the model reads, covering the whole happy path: tool →
``exec_env.run_argv`` → captured stdout/exit code, with the workspace root as
cwd.
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
    assert result.output == "hello-noeta"


def test_foreground_python_computes_and_returns_stdout(tmp_path: Path) -> None:
    tool, ctx = _tool_and_ctx(tmp_path)
    result = tool.invoke({"command": 'python3 -c "print(6*7)"'}, ctx)
    assert result.success
    assert "42" in result.output


def test_foreground_nonzero_exit_is_reported(tmp_path: Path) -> None:
    tool, ctx = _tool_and_ctx(tmp_path)
    result = tool.invoke({"command": "sh -c 'exit 3'"}, ctx)
    # A nonzero exit surfaces as a failed result whose summary names the code —
    # the adapter renders it ahead of the output text.
    assert result.success is False
    assert "Exit code 3" in result.summary


def test_foreground_stderr_labeled_only_when_both_streams(tmp_path: Path) -> None:
    tool, ctx = _tool_and_ctx(tmp_path)
    both = tool.invoke(
        {"command": "sh -c 'echo out; echo err 1>&2'"}, ctx
    )
    assert both.success
    assert "out" in both.output and "stderr:\nerr" in both.output
    only_err = tool.invoke({"command": "sh -c 'echo lone-err 1>&2'"}, ctx)
    assert only_err.output == "lone-err"


def test_foreground_empty_output_is_marked(tmp_path: Path) -> None:
    tool, ctx = _tool_and_ctx(tmp_path)
    result = tool.invoke({"command": "true"}, ctx)
    assert result.success
    assert result.output == "(no output)"


def test_foreground_runs_in_workspace_cwd(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("x")
    tool, ctx = _tool_and_ctx(tmp_path)
    result = tool.invoke({"command": "ls"}, ctx)
    assert result.success
    assert "marker.txt" in result.output


def test_foreground_full_streams_offloaded_as_artifacts(tmp_path: Path) -> None:
    tool, ctx = _tool_and_ctx(tmp_path)
    result = tool.invoke({"command": "printf audit-me"}, ctx)
    assert result.success
    assert len(result.artifacts) == 1
    assert ctx.artifact_store.get(result.artifacts[0]) == b"audit-me"
