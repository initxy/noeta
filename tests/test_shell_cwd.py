"""``Bash`` keeps its working directory between calls, as Claude Code does.

The directory a command leaves the shell in is recorded on its
``ToolResultRecorded`` as a ``shell_cwd`` side effect and read back by the next
command, so it survives a restart (a fresh ``ToolRuntime`` rebuilds it from the
log) and never outlives a sealed or rewound stretch of history. A command that
leaves the workspace is moved back with Claude Code's ``Shell cwd was reset``
note.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from noeta.builtins.fs.impl import ShellRunTool
from noeta.protocols.decisions import ToolCall
from noeta.protocols.tool import ToolContext
from noeta.runtime.background_shell import ProcessRegistry
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.tool import RecordedToolState, ToolRuntime, live_side_effects
from noeta.runtime.workspace import WorkspaceRoot
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


class _Session:
    """One task's calls through a ToolRuntime over a shared log."""

    def __init__(self, root: Path, log: InMemoryEventLog, store: InMemoryContentStore) -> None:
        self.root = Path(os.path.realpath(root))
        self.log, self.store = log, store
        self.registry = ProcessRegistry(event_log=log, content_store=store)
        self.runtime = ToolRuntime(
            event_log=log, content_store=store, background_runner=self.registry
        )
        self.tool = ShellRunTool(
            workspace=WorkspaceRoot.from_path(root), mode=ShellMode.ARBITRARY
        )
        self.calls = 0

    def bash(self, command: str, **extra: Any) -> Any:
        self.calls += 1
        return self.runtime.invoke(
            self.tool,
            ToolCall(
                tool_name="Bash",
                arguments={"command": command, **extra},
                call_id=f"c{self.calls}-{id(self)}",
            ),
            task_id="t",
            lease_id="lease",
            trace_id="trace",
        )


def _fresh(tmp_path: Path) -> tuple[_Session, InMemoryEventLog, InMemoryContentStore]:
    log, store = InMemoryEventLog(), InMemoryContentStore()
    return _Session(tmp_path, log, store), log, store


def test_the_directory_persists_between_commands(tmp_path: Path) -> None:
    session, log, _ = _fresh(tmp_path)
    first = session.bash("mkdir -p sub/deeper && cd sub")
    assert first.success and first.output == "(no output)"
    assert session.bash("pwd").output.strip() == str(session.root / "sub")
    session.bash("cd deeper")
    assert session.bash("pwd").output.strip() == str(session.root / "sub" / "deeper")
    recorded = [
        effect
        for env in log.read("t")
        if env.type == "ToolResultRecorded"
        for effect in env.payload.side_effects
    ]
    assert recorded == [
        {"type": "shell_cwd", "cwd": str(session.root / "sub")},
        {"type": "shell_cwd", "cwd": str(session.root / "sub" / "deeper")},
    ]


def test_a_fresh_process_rebuilds_the_directory_from_the_log(tmp_path: Path) -> None:
    session, log, store = _fresh(tmp_path)
    session.bash("mkdir sub && cd sub")
    restarted = _Session(tmp_path, log, store)
    assert restarted.bash("pwd").output.strip() == str(session.root / "sub")


def test_output_and_exit_status_are_untouched(tmp_path: Path) -> None:
    session, _, _ = _fresh(tmp_path)
    result = session.bash("printf hi; exit 3")
    assert not result.success
    assert result.summary == "Exit code 3"
    assert result.output == "hi"
    assert "NOETA" not in session.bash("echo out; echo err >&2").output


def test_leaving_the_workspace_resets_to_the_root(tmp_path: Path) -> None:
    session, _, _ = _fresh(tmp_path)
    session.bash("mkdir sub && cd sub")
    result = session.bash("cd / && echo there")
    assert result.output == f"there\nShell cwd was reset to {session.root}"
    assert session.bash("pwd").output.strip() == str(session.root)


def test_a_vanished_directory_starts_back_at_the_root(tmp_path: Path) -> None:
    session, _, _ = _fresh(tmp_path)
    session.bash("mkdir gone && cd gone")
    os.rmdir(session.root / "gone")
    result = session.bash("pwd")
    assert result.output == f"{session.root}\nShell cwd was reset to {session.root}"
    assert session.bash("pwd").output.strip() == str(session.root)


def test_a_background_command_starts_in_the_session_directory(tmp_path: Path) -> None:
    session, _, _ = _fresh(tmp_path)
    session.bash("mkdir sub && cd sub")
    started = session.bash("cd .. && pwd > where.txt", run_in_background=True)
    assert started.success
    assert started.output.endswith(
        f"\nSession cwd remains {session.root / 'sub'}; directory changes made by "
        "the backgrounded command do not apply to subsequent commands."
    )
    job = re.search(r"ID: ([\w-]+)", started.output)
    assert job is not None
    assert session.registry.wait(job.group(1), timeout_s=5) == "done"
    assert (session.root / "where.txt").read_text().strip() == str(session.root)
    assert session.bash("pwd").output.strip() == str(session.root / "sub")


def test_a_hand_built_context_keeps_no_directory(tmp_path: Path) -> None:
    tool = ShellRunTool(workspace=WorkspaceRoot.from_path(tmp_path), mode=ShellMode.ARBITRARY)
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    (tmp_path / "sub").mkdir()
    assert tool.invoke({"command": "cd sub"}, ctx).side_effects == []
    assert tool.invoke({"command": "pwd"}, ctx).output.strip() == os.path.realpath(tmp_path)


def test_the_allowlist_tier_never_moves(tmp_path: Path) -> None:
    tool = ShellRunTool(workspace=WorkspaceRoot.from_path(tmp_path), mode=ShellMode.ALLOWLIST)
    state = RecordedToolState()
    state.note([{"type": "shell_cwd", "cwd": "/"}])
    ctx = ToolContext(artifact_store=InMemoryContentStore(), recorded=state.latest)
    (tmp_path / "marker").write_text("")
    result = tool.invoke({"command": "ls"}, ctx)
    assert result.output.strip() == "marker"
    assert result.side_effects == []


# -- live history: sealed and rewound stretches vouch for nothing -------------


def _env(seq: int, type_: str, **payload: Any) -> Any:
    return SimpleNamespace(seq=seq, type=type_, payload=SimpleNamespace(**payload))


def test_live_side_effects_skip_sealed_and_rewound_stretches() -> None:
    def result(seq: int, cwd: str) -> Any:
        return _env(seq, "ToolResultRecorded", side_effects=[{"type": "shell_cwd", "cwd": cwd}])

    events = [
        result(1, "/a"),
        result(3, "/sealed"),
        _env(4, "StepAttemptAbandoned", abandoned_from_seq=2),
        result(5, "/b"),
        result(7, "/rewound"),
        _env(8, "TaskRewound", target_seq=6),
        result(9, "/c"),
        _env(10, "ToolResultRecorded", side_effects=["junk", {"no": "type"}]),
    ]
    assert [e["cwd"] for e in live_side_effects(events)] == ["/a", "/b", "/c"]


def test_recorded_state_seeds_once_and_resets() -> None:
    state = RecordedToolState()
    events = [_env(1, "ToolResultRecorded", side_effects=[
        {"type": "shell_cwd", "cwd": "/a"},
        {"type": "file_read", "path": "/x.py", "digest": "d1"},
    ])]
    state.seed(events)
    state.seed([_env(2, "ToolResultRecorded", side_effects=[{"type": "shell_cwd", "cwd": "/b"}])])
    assert state.latest("shell_cwd") == {"type": "shell_cwd", "cwd": "/a"}
    assert state.reads.digest("/x.py") == "d1"
    state.reset()
    assert state.latest("shell_cwd") is None and state.reads.digest("/x.py") is None
    assert not state.seeded
