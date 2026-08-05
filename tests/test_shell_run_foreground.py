"""``Bash`` foreground execution — real exit code AND real stdout bytes.

These drive a real subprocess through the default ``LocalExecEnv`` and assert
on the plain-text output the model reads, covering the whole happy path: tool →
``exec_env.run_argv`` → captured stdout/exit code, with the workspace root as
cwd.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.builtins.fs.impl.shell import ShellRunTool
from noeta.runtime.background_shell import ProcessRegistry
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import WorkspaceRoot


def _tool_and_ctx(tmp_path: Path) -> tuple[ShellRunTool, ToolContext]:
    ws = WorkspaceRoot.from_path(tmp_path)
    tool = ShellRunTool(workspace=ws, mode=ShellMode.ARBITRARY)
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    return tool, ctx


def _tool_with_registry(
    tmp_path: Path, task_id: str
) -> tuple[ShellRunTool, ToolContext, ProcessRegistry]:
    """Foreground tool wired the way the ToolRuntime wires it: the registry as
    ``background_runner`` and the task identity in the metadata bag — the two
    pieces the session kill-table registration keys off."""
    store = InMemoryContentStore()
    registry = ProcessRegistry(
        event_log=InMemoryEventLog(), content_store=store
    )
    ws = WorkspaceRoot.from_path(tmp_path)
    tool = ShellRunTool(workspace=ws, mode=ShellMode.ARBITRARY)
    ctx = ToolContext(
        artifact_store=store,
        metadata={"task_id": task_id, "trace_id": "tr"},
        background_runner=registry,
    )
    return tool, ctx, registry


def _await_foreground_registered(
    registry: ProcessRegistry, root_task_id: str, timeout_s: float = 5.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with registry._lock:  # noqa: SLF001 — observe the live kill table
            if registry._foreground.get(root_task_id):  # noqa: SLF001
                return
        time.sleep(0.01)
    raise AssertionError(
        f"no foreground run registered under {root_task_id!r} within {timeout_s}s"
    )


def _foreground_table(registry: ProcessRegistry) -> dict:
    with registry._lock:  # noqa: SLF001 — observe the live kill table
        return dict(registry._foreground)  # noqa: SLF001


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


# ---------------------------------------------------------------------------
# session kill table — a human stop reaps the FOREGROUND run mid-communicate()
# ---------------------------------------------------------------------------


def test_session_kill_reaps_foreground_run_promptly(tmp_path: Path) -> None:
    """The session kill cascade (what interrupt / cancel / close route through)
    reaps a registered foreground run: the step thread blocked in
    ``communicate()`` returns promptly — never running out the 30 s sleep —
    with a failed, explicitly *interrupted* result."""
    tool, ctx, registry = _tool_with_registry(tmp_path, "t-fg")
    results: list[ToolResult] = []
    worker = threading.Thread(
        target=lambda: results.append(tool.invoke({"command": "sleep 30"}, ctx)),
        daemon=True,
    )
    worker.start()
    _await_foreground_registered(registry, "t-fg")

    killed_at = time.monotonic()
    registry.kill_root_task("t-fg")
    worker.join(timeout=10.0)
    assert not worker.is_alive(), "foreground run not reaped by the session kill"
    # Prompt: SIGTERM kills the sleep at once; well under the SIGKILL grace,
    # and nowhere near the command's own 30 s runtime.
    assert time.monotonic() - killed_at < 8.0

    result = results[0]
    assert result.success is False
    # The interrupt must be named as such — not a generic timeout.
    assert "interrupted" in result.summary.lower()
    assert "timed out" not in result.summary.lower()
    # The finally-unregister left the kill table clean.
    assert _foreground_table(registry) == {}


def test_foreground_completion_unregisters(tmp_path: Path) -> None:
    """A normally completing foreground run leaves the kill table empty and an
    un-interrupted (successful) result — registration is scoped to the run."""
    tool, ctx, registry = _tool_with_registry(tmp_path, "t-fg")
    result = tool.invoke({"command": "printf done"}, ctx)
    assert result.success
    assert result.output == "done"
    assert _foreground_table(registry) == {}
