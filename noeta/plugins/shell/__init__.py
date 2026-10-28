"""The shell: ``Bash`` (whose working directory carries over between
commands), and background jobs: ``run_in_background``, ``Monitor`` and
``TaskStop`` (both deferred).

Jobs belong to the worker process (a process-local table keyed by the task
tree's root, see :mod:`.jobs`), so while a job runs its worker holds the task
tree (``ToolContext.hold``): no other worker claims a task of it, and the
next turn can stop the job. What the log keeps is what a reader
needs: a ``started`` record in the task that launched the job (with its
worker and hold), then exactly one of ``ended`` or ``lost``, decided by the
task's runner from its inbox, in inbox order: the job's end
(``External(JOB_ENDED)``, posted before the job gives its hold back) makes it
``ended``; ``hold_lost`` for the hold it ran under (its worker died, froze
past the hold, or stopped; the job died with the hold) makes it ``lost``. An
end that arrives after its job was called lost is dropped.

A job's end, its loss, and each monitor event reach the model as Claude
Code's ``<task-notification>``, a system-origin message in the task that
started it (the root once that task has ended), opening a turn if the task
is idle. The log of a task that ended keeps its jobs as they were then: the
root is told of their end, or, when ``hold_lost`` names the hold one ran
under, of its loss (one notice per job either way, by its dedupe key).
Stopping the root's turn, cancelling or ending the root kills every job of
the tree without a notice; with ``background_ends_with_turn`` a job also
dies, silently, when the turn of the task that started it ends.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from noeta.core import records
from noeta.core.events import (
    ExtensionRecorded,
    External,
    InboxItem,
    InputTaken,
    Recorded,
    Say,
    TaskEnded,
    Waiting,
)
from noeta.core.extension import Extension, TaskView
from noeta.core.store import HOLD_LOST
from noeta.core.tool import Tool
from noeta.core.types import user
from noeta.plugins.shell.bash import SHELL, BashTool, ShellState, reduce_shell, sandbox_description
from noeta.plugins.shell.jobs import DEFAULT_LOG_DIR, JOB_ENDED, REGISTRY, Registry
from noeta.plugins.shell.notices import lost_summary, task_notification
from noeta.plugins.shell.tools import LOST_REASON, BackgroundBash, MonitorTool, TaskStopTool
from noeta.plugins.shell.worktree import EnterWorktreeTool, ExitWorktreeTool
from noeta.toolkit.notices import NOTICE_GUARD
from noeta.toolkit.sandbox import sandbox_of

__all__ = ["JOB_ENDED", "SHELL", "BashTool", "Registry", "Shell", "lost_notice"]


def lost_notice(job_id: str, job: Mapping[str, Any]) -> str:
    """What the model is told of a job whose worker is gone."""
    summary = lost_summary(str(job.get("description") or job.get("command") or job_id),
                           monitor=bool(job.get("monitor")), reason=LOST_REASON)
    text = task_notification(task_id=job_id, tool_use_id=str(job.get("tool_use_id") or ""),
                             output_file=str(job.get("output_file") or ""), status="failed",
                             summary=summary)
    return f"{NOTICE_GUARD}\n\n{text}"


def _notice_key(job_id: str) -> str:
    return f"shell:notice:{job_id}"


def _worktree_tools() -> list[Tool]:
    return [EnterWorktreeTool(), ExitWorktreeTool()]


class Shell(Extension):
    """``Bash``, and background jobs for ``Bash`` and ``Monitor``.

    Arguments a host sets:

    * ``background`` — offer background jobs (``run_in_background``,
      ``Monitor``, ``TaskStop``); off = foreground ``Bash`` only.
    * ``background_ends_with_turn`` — end a task's jobs when its turn ends
      (one-shot workers); the launch results then say so (0.6's
      ``HostConfig.background_ends_with_turn``).
    * ``bash`` — keyword arguments for the ``Bash`` this extension offers
      (``timeout_s``, ``output_cap``, ``shell_env``, ``hardening_rules``),
      the same as ``BashTool``'s.
    * ``registry`` — the job table; by default the process-wide one, so every
      agent of a worker shares the per-root cap of 8 jobs. ``max_jobs`` /
      ``log_dir`` configure a private one instead.
    """

    name = SHELL

    def __init__(self, *, background: bool = True, background_ends_with_turn: bool = False,
                 bash: dict[str, Any] | None = None, registry: Registry | None = None,
                 max_jobs: int | None = None, log_dir: str | None = None) -> None:
        if registry is None and (max_jobs or log_dir):
            registry = Registry(max_jobs=max_jobs or 8, log_dir=log_dir or DEFAULT_LOG_DIR)
        self.registry = registry if registry is not None else REGISTRY
        self.ends_with_turn = background_ends_with_turn
        #: The tool list with ``Bash`` describing the sandbox, per settings
        #: and working directory.
        self._sandboxed: dict[tuple[Any, str], list[Tool]] = {}
        if not background:
            self._tools: list[Tool] = [BashTool(**(bash or {})), *_worktree_tools()]
            return
        self.bash = BackgroundBash(self.registry, ends_with_turn=background_ends_with_turn,
                                   **(bash or {}))
        self._tools = [self.bash, MonitorTool(self.bash), TaskStopTool(self.registry),
                       *_worktree_tools()]

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return ShellState()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return reduce_shell(state, kind, data)

    def continue_state(self, state: Any) -> Any:
        """A continuation keeps the cwd and finished jobs: a running job
        belongs to the source task, whose notices it posts."""
        if not isinstance(state, ShellState):
            return state
        return replace(state, jobs={k: j for k, j in state.jobs.items()
                                    if j.get("status") != "running"})

    def tools(self, view: TaskView) -> Sequence[Tool]:
        sandbox = sandbox_of(view.env)
        if sandbox is None or sandbox.unavailable() is not None:
            return self._tools
        root = str(getattr(view.env, "cwd", "") or "")
        key = (sandbox.settings, root)
        if key not in self._sandboxed:
            bash = copy.copy(self._tools[0])
            bash.description = sandbox_description(bash.description, sandbox, root)
            self._sandboxed[key] = [bash, *self._tools[1:]]
        return self._sandboxed[key]

    # ------------------------------------------------------------ lifecycle

    async def on_start(self, view: TaskView) -> None:
        """Decide again what a crash may have left undecided: ends and
        losses this task took from its inbox while a job still shows running
        (the runner died between taking them and recording the decision).
        Deciding twice changes nothing: a settled job is skipped."""
        st = view.ext_state(self.name)
        if not isinstance(st, ShellState) or view.read_log is None or not any(
                j.get("status") == "running" for j in st.jobs.values()):
            return
        items = [i for r in await view.read_log(view.task_id)
                 if isinstance(r.event, InputTaken) for i in r.event.items]
        self._decide(view, items)

    def _decide(self, view: TaskView, items: Sequence[InboxItem]) -> None:
        """Settle jobs from what the task took, in inbox order: a job's end
        makes it ended and tells the model; ``hold_lost`` for the hold it ran
        under makes it lost and tells the model so; anything about a job
        already settled is dropped. The notice is posted with the record
        (``enqueue``), so it opens a turn on an idle task."""
        st = view.ext_state(self.name)
        jobs = {k: dict(v) for k, v in (st.jobs if isinstance(st, ShellState) else {}).items()}
        for item in items:
            body = item.body
            if not isinstance(body, External) or not isinstance(body.data, Mapping):
                continue
            if body.name == JOB_ENDED:
                job_id = str(body.data.get("job_id") or "")
                job = jobs.get(job_id)
                notice = body.data.get("notice")
                if job is not None and job.get("status") != "running":
                    continue  # lost (or ended) already: one story only
                if job is not None:
                    job["status"] = "ended"
                    view.record(self.name, "ended", {
                        "job_id": job_id, "status": body.data.get("status"),
                        "exit_code": body.data.get("exit_code")})
                if isinstance(notice, str):
                    # (A job of a task that has ended reports to the root,
                    # whose log does not have it: its notice still goes in.)
                    view.enqueue(Say(message=user(notice, origin="system"), when="now"),
                                 dedupe_key=_notice_key(job_id))
            elif body.name == HOLD_LOST:
                for job_id, job in jobs.items():
                    if job.get("status") != "running" or "hold" not in job \
                            or job.get("hold") != body.data.get("epoch") \
                            or job.get("worker") != body.data.get("worker"):
                        continue
                    job["status"] = "lost"
                    view.record(self.name, "lost", {"job_id": job_id})
                    view.enqueue(Say(message=user(lost_notice(job_id, job), origin="system"),
                                     when="now"), dedupe_key=_notice_key(job_id))

    async def _tell_lost_of_ended(self, view: TaskView, items: Sequence[InboxItem]) -> None:
        """The root learns that a hold is gone: jobs that ran under it for
        a task of the tree that has ended (which no longer hears anything)
        are lost, and the root says so in its place. A job whose end
        already reached the root keeps that story: both notices share the
        job's dedupe key."""
        lost = [(i.body.data.get("worker"), i.body.data.get("epoch")) for i in items
                if isinstance(i.body, External) and i.body.name == HOLD_LOST
                and isinstance(i.body.data, Mapping)]
        if not lost or view.state.parent_id is not None or view.child_tasks is None \
                or view.read_log is None:
            return
        todo, seen = await view.child_tasks(view.task_id), set()
        while todo:
            task_id = todo.pop()
            if task_id in seen:
                continue
            seen.add(task_id)
            todo += await view.child_tasks(task_id)
            st: ShellState | None = None
            ended = False
            for r in await view.read_log(task_id):
                ev = r.event
                ended = ended or isinstance(ev, TaskEnded)
                if isinstance(ev, ExtensionRecorded) and ev.extension == self.name:
                    st = reduce_shell(st, ev.kind, ev.data)
            if not ended or st is None:
                continue  # a live task hears ``hold_lost`` itself
            for job_id, job in st.jobs.items():
                if job.get("status") == "running" and (job.get("worker"), job.get("hold")) in lost:
                    view.enqueue(Say(message=user(lost_notice(job_id, job), origin="system"),
                                     when="now"), dedupe_key=_notice_key(job_id))

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        event = recorded.event
        state = view.state
        root = state.root_id or state.task_id
        if isinstance(event, InputTaken):
            self._decide(view, event.items)
            await self._tell_lost_of_ended(view, event.items)
        elif isinstance(event, TaskEnded):
            self.registry.mark_ended(state.task_id, root)
            if state.parent_id is None:
                self.registry.retire(root)
            elif self.ends_with_turn:
                self.registry.kill_where(root, spawner_id=state.task_id)
        elif isinstance(event, Waiting) and event.on == "input" and self.ends_with_turn:
            self.registry.kill_where(root, spawner_id=state.task_id)
        elif isinstance(records.decode(event), records.TurnStopped) and state.parent_id is None:
            self.registry.kill_where(root)
