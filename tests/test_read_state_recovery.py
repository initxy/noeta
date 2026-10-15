"""The read-first record survives a restart, as Claude Code's does on resume.

Every ``Read`` (and every applied ``Edit`` / ``Write``) leaves a ``file_read``
side effect on its ``ToolResultRecorded``. A process that never saw the read —
a restart, a crash-recovery re-drive — rebuilds the record from the task's live
history, so ``Edit`` no longer fails "File has not been read yet" on a file the
conversation did read; a read inside a sealed attempt (no longer in the
conversation) vouches for nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.builtins.fs.impl import build_fs_tools
from noeta.protocols.decisions import ToolCall
from noeta.protocols.events import StepAttemptAbandonedPayload
from noeta.runtime.task_local import TaskLocalRegistry
from noeta.runtime.tool import ToolRuntime
from noeta.runtime.workspace import FsWriteMode, WorkspaceRoot
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


class _Process:
    """One process's view of task ``t``: a fresh ToolRuntime over the log."""

    def __init__(self, ws: Path, log: InMemoryEventLog, store: InMemoryContentStore,
                 locals_: TaskLocalRegistry | None = None) -> None:
        self.log, self.store = log, store
        self.tools = build_fs_tools(WorkspaceRoot.from_path(ws), mode=FsWriteMode.APPLY)
        kwargs: dict[str, Any] = {}
        if locals_ is not None:
            kwargs = {
                "file_read_registry": locals_.read_registry("t"),
                "recorded_state": locals_.tool_state("t"),
            }
        self.runtime = ToolRuntime(event_log=log, content_store=store, **kwargs)
        self.calls = 0

    def call(self, tool: str, **arguments: Any) -> Any:
        self.calls += 1
        return self.runtime.invoke(
            self.tools[tool],
            ToolCall(tool_name=tool, arguments=arguments, call_id=f"{tool}-{self.calls}-{id(self)}"),
            task_id="t", lease_id="lease", trace_id="trace",
        )


def _setup(tmp_path: Path) -> tuple[Path, InMemoryEventLog, InMemoryContentStore]:
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    return tmp_path, InMemoryEventLog(), InMemoryContentStore()


def test_a_read_before_a_restart_lets_edit_run_after_it(tmp_path: Path) -> None:
    ws, log, store = _setup(tmp_path)
    assert _Process(ws, log, store).call("Read", file_path="a.py").success
    edited = _Process(ws, log, store).call(
        "Edit", file_path="a.py", old_string="x = 1", new_string="x = 10"
    )
    assert edited.success, edited.summary
    assert (ws / "a.py").read_text() == "x = 10\ny = 2\n"


def test_an_applied_edit_vouches_for_the_next_one_across_a_restart(tmp_path: Path) -> None:
    ws, log, store = _setup(tmp_path)
    first = _Process(ws, log, store)
    first.call("Read", file_path="a.py")
    assert first.call("Edit", file_path="a.py", old_string="x = 1", new_string="x = 10").success
    second = _Process(ws, log, store).call(
        "Edit", file_path="a.py", old_string="y = 2", new_string="y = 20"
    )
    assert second.success, second.summary


def test_a_file_changed_since_the_recorded_read_still_needs_a_read(tmp_path: Path) -> None:
    ws, log, store = _setup(tmp_path)
    _Process(ws, log, store).call("Read", file_path="a.py")
    (ws / "a.py").write_text("x = 1\ny = 2\nz = 3\n")
    result = _Process(ws, log, store).call(
        "Edit", file_path="a.py", old_string="x = 1", new_string="x = 10"
    )
    assert not result.success
    assert "modified since it was read" in result.summary


def test_never_read_is_still_refused(tmp_path: Path) -> None:
    ws, log, store = _setup(tmp_path)
    result = _Process(ws, log, store).call(
        "Edit", file_path="a.py", old_string="x = 1", new_string="x = 10"
    )
    assert "File has not been read yet" in result.summary


def test_a_sealed_read_vouches_for_nothing_after_the_reset(tmp_path: Path) -> None:
    ws, log, store = _setup(tmp_path)
    locals_ = TaskLocalRegistry()
    process = _Process(ws, log, store, locals_)
    read = process.call("Read", file_path="a.py")
    assert read.success
    started = next(e.seq for e in log.read("t") if e.type == "ToolCallStarted")
    log.emit(
        task_id="t",
        type="StepAttemptAbandoned",
        payload=StepAttemptAbandonedPayload(
            abandoned_from_seq=started,
            state_ref=store.put(b"{}", media_type="application/json"),
            reason="auto_redrive",
        ),
        lease_id="lease",
    )
    # Same process: the record still holds the read until recovery resets it.
    locals_.reset_tool_state("t")
    result = process.call("Edit", file_path="a.py", old_string="x = 1", new_string="x = 10")
    assert "File has not been read yet" in result.summary


def test_the_read_rides_the_result_as_a_side_effect(tmp_path: Path) -> None:
    ws, log, store = _setup(tmp_path)
    _Process(ws, log, store).call("Read", file_path="a.py")
    recorded = [e for e in log.read("t") if e.type == "ToolResultRecorded"]
    (effect,) = recorded[0].payload.side_effects
    assert effect["type"] == "file_read"
    assert effect["path"] == str((ws / "a.py").resolve())
    assert len(effect["digest"]) == 64


def test_the_host_resets_a_tasks_carried_state(tmp_path: Path) -> None:
    from noeta.execution.resolver import GenericEngineResolver  # noqa: PLC0415

    locals_ = TaskLocalRegistry()
    state = locals_.tool_state("t")
    state.seed([])
    holder = type("H", (), {"_task_locals": locals_})()
    GenericEngineResolver.reset_recorded_tool_state(holder, "t")  # type: ignore[arg-type]
    assert not state.seeded
    GenericEngineResolver.reset_recorded_tool_state(type("N", (), {})(), "t")  # type: ignore[arg-type]
