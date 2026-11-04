"""Each record a plugin publishes for others, written by that plugin's own
fold and read back through its reader in ``noeta.toolkit.plugin_records``:
a writer that changes its record's shape fails here, not silently in the
plugin that reads it."""

from __future__ import annotations

import asyncio

from noeta.core.events import Recorded
from noeta.core.extension import Extension, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.types import ModelRequest
from noeta.plugins.checkpoints import Checkpoints
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.window import derive_window
from noeta.plugins.fs import FileTools
from noeta.plugins.hooks import Hooks
from noeta.plugins.instructions import Instructions
from noeta.plugins.mcp import Mcp
from noeta.plugins.mcp.config import McpServer
from noeta.plugins.mcp.extension import _server_entry
from noeta.plugins.memory import Memory
from noeta.plugins.permissions import Permissions
from noeta.plugins.plan_mode import PlanMode
from noeta.plugins.shell import Shell
from noeta.plugins.title import Title
from noeta.toolkit import plugin_records as pr


def _folded(plugins: list[Extension], *records: tuple[str, str, dict[str, object]]) -> TaskView:
    """A view whose state has ``records`` folded by ``plugins``' own reducers."""
    view = TaskView(state=TaskState(task_id="t", config={"cwd": "/w"}), extensions=plugins)
    for extension, kind, data in records:
        view.record(extension, kind, data)
    state = view.folded({p.name: p.reduce for p in plugins})
    return TaskView(state=state, extensions=plugins)


def test_the_plugins_are_named_as_their_readers_name_them() -> None:
    plugins: list[Extension] = [Checkpoints(), Compaction(), FileTools(), Hooks(),
                                Instructions(), Mcp(), Memory(), Permissions(), PlanMode(),
                                Shell(), Title()]
    assert [p.name for p in plugins] == [
        pr.CHECKPOINTS, pr.COMPACTION, pr.FS, pr.HOOKS, pr.INSTRUCTIONS, pr.MCP, pr.MEMORY,
        pr.PERMISSIONS, pr.PLAN_MODE, pr.SHELL, pr.TITLE]


def test_each_record_reads_back_as_its_writer_folds_it() -> None:
    files = [{"path": "/w/NOETA.md", "real": "/w/NOETA.md", "kind": "project",
              "digest": "d1", "mtime": 1.0}]
    nested = [{"path": "/w/sub/AGENTS.md", "real": "/w/sub/AGENTS.md", "kind": "project",
               "digest": "d2", "mtime": 2.0}]
    entry = _server_entry(McpServer(alias="S" * 130, command=["x"]), [{"name": "a"}])
    view = _folded(
        [Permissions(), PlanMode(), Shell(), Memory(), Instructions(), FileTools(), Mcp()],
        (pr.PERMISSIONS, "mode", {"mode": "plan", "plan": {"path": "/p/plan.md",
                                                          "pre_mode": "default"}}),
        (pr.SHELL, "cwd", {"cwd": "/w/sub"}),
        (pr.MEMORY, "dir", {"dir": "/m/"}),
        (pr.INSTRUCTIONS, "start", {"files": files, "memory": {"/m/MEMORY.md": "d3"}}),
        (pr.INSTRUCTIONS, "nested", {"files": nested}),
        (pr.FS, "read", {"path": "/w/a.py", "digest": "x", "mtime": 0.0, "by": "Bash",
                         "call_id": "c1"}),
        (pr.MCP, "listed", {"servers": [entry]}),
        (pr.MCP, "server_down", {"alias": "gh", "reason": "refused"}),
    )
    ext = view.state.ext
    assert pr.permission_mode(ext) == "plan"
    assert pr.plan_file(ext) == "/p/plan.md"
    assert pr.shell_cwd(ext) == "/w/sub"
    assert pr.memory_dir(ext) == "/m/"
    assert [e["path"] for e in pr.shown_instruction_files(ext)] == [
        "/w/NOETA.md", "/w/sub/AGENTS.md"]
    assert dict(pr.instruction_digests(ext)) == {
        "/w/NOETA.md": "d1", "/m/MEMORY.md": "d3", "/w/sub/AGENTS.md": "d2"}
    assert pr.files_shown_by_commands(ext) == {"c1": ["/w/a.py"]}
    assert pr.mcp_down(ext) == {"gh": "refused"}
    assert pr.mcp_names_too_long(ext) == {"S" * 130: 1}


def test_a_missing_plugin_reads_as_the_default() -> None:
    view = TaskView(state=TaskState(task_id="t", config={"cwd": "/w"}))
    ext = view.state.ext
    assert (pr.permission_mode(ext), pr.plan_file(ext), pr.shell_cwd(ext),
            pr.memory_dir(ext)) == (None, None, None, None)
    assert pr.shown_instruction_files(ext) == [] and pr.instruction_digests(ext) == {}
    assert pr.files_shown_by_commands(ext) == {} and pr.mcp_down(ext) == {}
    assert pr.mcp_names_too_long(ext) == {}
    assert (pr.mode_of(view), pr.plan_start_mode(view), pr.working_directories(view),
            pr.read_deny_rule(view)) == (None, None, (), None)
    assert not pr.instructions_rehang(view) and not pr.instructions_managed_only(view)


def test_the_methods_one_plugin_calls_on_another_are_there() -> None:
    view = TaskView(state=TaskState(task_id="t", config={"cwd": "/w"}),
                    extensions=[Permissions(mode="acceptEdits"),
                                Instructions(rehang=False, managed_only=True)])
    assert pr.mode_of(view) == "acceptEdits"
    assert pr.plan_start_mode(view) == "acceptEdits"
    assert pr.working_directories(view)[0] == "/w"
    assert pr.read_deny_rule(view) is not None
    assert not pr.instructions_rehang(view) and pr.instructions_managed_only(view)


def test_the_auto_compact_threshold_reads_back_once_per_change() -> None:
    plugin = Compaction(auto=False)
    view = TaskView(state=TaskState(task_id="t", config={"cwd": "/w"}), extensions=[plugin])
    request = ModelRequest(model="claude-sonnet-4-5")
    asyncio.run(plugin.before_model(view, request))
    written = [Recorded(seq=i, at=0.0, event=e) for i, e in enumerate(view.pending)]
    expected = derive_window("claude-sonnet-4-5")
    assert pr.auto_compact(written) == pr.AutoCompact(
        threshold=expected.threshold, window=expected.effective, auto=False)
    # The same numbers on the next request: nothing more is written.
    again = TaskView(state=view.folded({plugin.name: plugin.reduce}), extensions=[plugin])
    asyncio.run(plugin.before_model(again, request))
    assert again.pending == []
    assert pr.auto_compact([]) is None
