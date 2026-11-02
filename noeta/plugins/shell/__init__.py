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

Unless the task starts in ``auto`` or ``bypassPermissions`` mode,
``Bash``'s description steers file reading and editing to the dedicated
tools, as Claude Code's does. The sandbox commands run in is described in
a ``sandbox_instructions`` reminder, sent again whenever it changes (and
after a compaction); one that went away is followed by a note that it is off.
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
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.store import HOLD_LOST
from noeta.core.tool import Tool
from noeta.core.types import Message, user
from noeta.plugins.shell.bash import BashTool
from noeta.plugins.shell.job_logs import DEFAULT_LOG_DIR
from noeta.plugins.shell.jobs import JOB_ENDED, REGISTRY, Registry
from noeta.plugins.shell.notices import lost_summary, running_status, task_notification
from noeta.plugins.shell.sandbox import SANDBOX_OFF, sandbox_instructions
from noeta.plugins.shell.state import SANDBOX_KIND, SHELL, ShellState, reduce_shell
from noeta.plugins.shell.tools import LOST_REASON, BackgroundBash, MonitorTool, TaskStopTool
from noeta.plugins.shell.worktree import EnterWorktreeTool, ExitWorktreeTool
from noeta.toolkit.notices import NOTICE_GUARD, STATUS_ORDER
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


#: More than the core records a pickup writes before ``on_start`` (the
#: prompt files, the plugin pins): how far back a pickup looks for the take
#: its last runner may have died after.
_START_RECORDS = 4
#: Where the sandbox instructions go among a request's harness notes: first,
#: as Claude Code sends them.
SANDBOX_ORDER = -20
#: The permission modes a task may start in without ``Bash`` steering file
#: work to the dedicated tools (Claude Code's: the auto-mode guidance says
#: Bash is fine for it there).
_UNSTEERED_MODES = ("auto", "bypassPermissions")
#: The line that steers it, after the description's lines on the shell's state.
_STEER_AFTER = "- Variables and functions you define do not survive between calls.\n"
_STEER = ("- Do not run `cat`, `head`, `tail`, `sed`, `awk` or `echo` with this tool unless "
          "you are told to or have checked that no dedicated tool can do the job: the "
          "dedicated tool serves the user better.\n")


def _ran_under(job: Mapping[str, Any], lost: Sequence[tuple[Any, Any]]) -> bool:
    """Whether ``job`` ran under one of the ``lost`` holds (worker, epoch).
    Epoch 0 is a version 1 store's hold, told on upgrade: it covers the jobs
    a version 1 worker recorded, which carry no epoch, and whose worker is
    that worker's process rather than the hold's worker."""
    if "hold" not in job:
        return any(epoch == 0 for _worker, epoch in lost)
    return (job.get("worker"), job.get("hold")) in lost


def _notice_key(job_id: str) -> str:
    return f"shell:notice:{job_id}"


def _worktree_tools() -> list[Tool]:
    return [EnterWorktreeTool(), ExitWorktreeTool()]


def _steered(tools: list[Tool]) -> list[Tool]:
    """``tools`` with ``Bash`` (the first) steering file work to the
    dedicated tools."""
    bash = copy.copy(tools[0])
    assert _STEER_AFTER in bash.description
    bash.description = bash.description.replace(_STEER_AFTER, _STEER_AFTER + _STEER, 1)
    return [bash, *tools[1:]]


def _starts_steered(view: TaskView) -> bool:
    """Whether ``Bash`` steers file work for this task: decided by the
    permission mode it is in (the permissions plugin's ``mode``, else its
    config's) when it first asks."""
    mode = getattr(view.ext_state("permissions"), "mode", None) \
        or view.state.config.get("permission_mode")
    return mode not in _UNSTEERED_MODES


class Shell(Extension):
    """``Bash``, and background jobs for ``Bash`` and ``Monitor``.

    Arguments a host sets:

    * ``background`` — offer background jobs (``run_in_background``,
      ``Monitor``); off = foreground ``Bash`` only (``TaskStop`` stays, for
      background agents and Workflow runs).
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
        if not background:
            # TaskStop also stops the agents and Workflow runs a task started
            # in the background (Claude Code always offers it).
            self._tools: list[Tool] = [BashTool(**(bash or {})), TaskStopTool(self.registry),
                                       *_worktree_tools()]
        else:
            self.bash = BackgroundBash(self.registry, ends_with_turn=background_ends_with_turn,
                                       **(bash or {}))
            self._tools = [self.bash, MonitorTool(self.bash), TaskStopTool(self.registry),
                           *_worktree_tools()]
        self._steered_tools = _steered(self._tools)

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
        st = view.ext_state(self.name)
        steer = st.steer if isinstance(st, ShellState) and st.steer is not None \
            else _starts_steered(view)
        return self._steered_tools if steer else self._tools

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """Record once whether ``Bash`` steers file work (so a later mode
        switch leaves its description, and the prompt cache, alone); the
        sandbox instructions whenever they differ from what the model was
        told; the status lines of jobs still running at a compaction."""
        st = view.ext_state(self.name)
        st = st if isinstance(st, ShellState) else ShellState()
        if st.steer is None:
            view.record(self.name, "steer", {"steer": _starts_steered(view)})
        out: list[Reminder] = []
        if view.find_tool("Bash") is not None:
            root = str(getattr(view.env, "cwd", "") or "")
            text = sandbox_instructions(sandbox_of(view.env), root)
            if text != st.sandbox_told:
                out.append(Reminder(text=text or SANDBOX_OFF, kind=SANDBOX_KIND,
                                    data={"text": text}, role="system", order=SANDBOX_ORDER))
        out.extend(Reminder(text=t, kind="task_status_sent", data={}, role="system",
                            order=STATUS_ORDER) for t in st.held_status)
        return out

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """At a compaction: the sandbox instructions are told again, and a
        status line is owed for each background job still running (Claude
        Code's ``task_status``)."""
        st = view.ext_state(SHELL)
        if view.keeps_tail is None or not isinstance(st, ShellState):
            return ()
        if st.sandbox_told:
            view.record(self.name, SANDBOX_KIND, {"text": ""})
        texts = running_status(st.jobs)
        if texts:
            view.record(SHELL, "task_status", {"texts": texts})
        return ()

    # ------------------------------------------------------------ lifecycle

    async def on_start(self, view: TaskView) -> None:
        """Decide again what a crash may have left undecided: ends and
        losses this task took from its inbox while a job still shows running
        (the runner died between taking them and recording the decision),
        and a root's losses of ended tasks' jobs. Deciding twice changes
        nothing: a settled job is skipped, a notice is posted once."""
        await self._retell_last_take(view)
        st = view.ext_state(self.name)
        if not isinstance(st, ShellState) or view.read_log is None or not any(
                j.get("status") == "running" for j in st.jobs.values()):
            return
        items = [i for r in await view.read_log(view.task_id)
                 if isinstance(r.event, InputTaken) for i in r.event.items]
        self._decide(view, items)

    async def _retell_last_take(self, view: TaskView) -> None:
        """A root that died after taking ``hold_lost`` and before posting
        the notices it decided for ended tasks' jobs: the take is then the
        last thing in its log (but for what this pickup's start recorded),
        so it is told again; the notices' dedupe keys make that a no-op when
        they were posted."""
        if view.state.parent_id is not None or view.read_log is None:
            return
        tail = await view.read_log(view.task_id, max(0, view.state.last_seq - _START_RECORDS))
        for rec in reversed(tail):
            ev = rec.event
            if isinstance(ev, ExtensionRecorded) and ev.extension == records.CORE:
                continue
            if isinstance(ev, InputTaken):
                await self._tell_lost_of_ended(view, ev.items)
            return

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
                lost = (body.data.get("worker"), body.data.get("epoch"))
                for job_id, job in jobs.items():
                    if job.get("status") != "running" or not _ran_under(job, [lost]):
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
                continue  # a live task hears ``hold_lost`` itself: it cannot end unread
            for job_id, job in st.jobs.items():
                if job.get("status") == "running" and _ran_under(job, lost):
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
