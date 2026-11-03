"""After a compaction, a status line for each background agent and each
background shell job still running (Claude Code's ``task_status``), given
with the next request and then no more."""

from __future__ import annotations

from typing import Any

from noeta.core.events import ChildEnded
from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.plugins.shell import Shell
from noeta.plugins.shell.state import ShellState
from noeta.plugins.subtasks import Subtasks
from noeta.plugins.subtasks.roster import SubagentSpec
from noeta.toolkit.notices import STATUS_ORDER


async def _status(ext: Any, state: TaskState) -> list[str]:
    """Run ``ext``'s compaction hook, fold what it recorded, and return the
    status lines the next request carries (checked to be sent once)."""
    view = TaskView(state=state)
    view.keeps_tail = True
    assert await ext.resident(view) == ()
    for rec in view.pending:
        state.ext[ext.name] = ext.reduce(state.ext.get(ext.name), rec.kind, rec.data)
    reminders = [r for r in await ext.reminders(TaskView(state=state))
                 if r.kind == "task_status_sent"]
    assert all(r.role == "system" and r.order == STATUS_ORDER for r in reminders)
    for r in reminders:
        state.ext[ext.name] = ext.reduce(state.ext.get(ext.name), r.kind, r.data)
    assert not [r for r in await ext.reminders(TaskView(state=state))
                if r.kind == "task_status_sent"]
    return [r.text for r in reminders]


async def test_running_shell_jobs_are_told_after_a_compaction() -> None:
    jobs = {
        "bg-1": {"job_id": "bg-1", "status": "running", "command": "npm run\n  dev",
                 "description": "dev server", "output_file": "/o/bg-1.out"},
        "bg-2": {"job_id": "bg-2", "status": "completed", "command": "ls"},
        "bg-3": {"job_id": "bg-3", "status": "running", "command": "tail -f log",
                 "description": "x" * 300, "monitor": True},
    }
    texts = await _status(Shell(), TaskState(task_id="t", ext={"shell": ShellState(jobs=jobs)}))
    assert texts[0] == (
        'Background shell bg-1 ("dev server") is still running (its command, on one line: '
        "`npm run dev`). Do not start it again; to restart it, stop it with TaskStop first. "
        "Its output is in /o/bg-1.out.")
    assert texts[1].startswith(f'Background monitor bg-3 ("{"x" * 199}…")')
    assert len(texts) == 2


async def test_running_agents_are_told_after_a_compaction() -> None:
    sub = Subtasks([SubagentSpec(name="general-purpose", description="Does work.",
                                 agent="worker")])
    agents = {"a1": {"task_id": "c1", "description": "scan the repo",
                     "output_file": "/s/a1.output"},
              "a2": {"task_id": "c2", "description": "done already"}}
    state = TaskState(task_id="t", ext={"subtasks": {"agents": agents}},
                      children={"c2": ChildEnded(child_id="c2", status="done")})
    [text] = await _status(sub, state)
    assert text == (
        'Background agent "scan the repo" (a1) is still running. Do not start it again; its '
        "completion notice will come by itself. Its transcript so far is at /s/a1.output. To "
        "ask it how far it got before then, send it a message with SendMessage.")


async def test_no_status_outside_a_compaction() -> None:
    jobs = {"bg-1": {"job_id": "bg-1", "status": "running", "command": "sleep 9"}}
    view = TaskView(state=TaskState(task_id="t", ext={"shell": ShellState(jobs=jobs)}))
    assert await Shell().resident(view) == () and view.pending == []
