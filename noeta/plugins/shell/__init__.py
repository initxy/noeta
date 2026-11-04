"""The shell: ``Bash`` (whose working directory carries over between
commands), and background jobs: ``run_in_background``, ``Monitor`` and
``TaskStop`` (both deferred).

Jobs belong to the worker process and to the task that started them (a
process-local table keyed by task, see :mod:`.jobs`), so while a job runs
the task keeps its claim (``ToolContext.keep``), over its waits too: no
other worker runs the task, and its next turn can stop the job. A parent
stops a child's job through the child's inbox (``STOP_JOB``). What the log
keeps is what a reader needs: a ``started`` record, then exactly one of
``ended`` or ``lost``, decided by the task's runner from its inbox, in inbox
order: the job's end (``External(JOB_ENDED)``, posted before the job gives
the claim back) makes it ``ended``; ``JOBS_LOST`` makes it ``lost``, which
a pickup posts for a job still running in the log that this worker does not
have (its worker died or stopped, and the job with the claim). An end that
arrives after its job was called lost is dropped.

A job's end, its loss, and each monitor event reach the model as Claude
Code's ``<task-notification>``, a system-origin message in the task that
started it, opening a turn if the task is idle. A job dies, without a
notice, with its task's end (a sub-agent's when it hands back);
with ``background_ends_with_turn`` also when the task's turn ends.

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

from noeta.core.events import (
    External,
    InboxItem,
    InputTaken,
    Recorded,
    Say,
    TaskEnded,
    Waiting,
)
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.tool import Tool
from noeta.core.types import Message, user
from noeta.plugins.shell.bash import BashTool
from noeta.plugins.shell.job_logs import DEFAULT_LOG_DIR
from noeta.plugins.shell.jobs import JOB_ENDED, REGISTRY, Registry
from noeta.plugins.shell.notices import lost_summary, running_status, task_notification
from noeta.plugins.shell.sandbox import SANDBOX_OFF, sandbox_instructions
from noeta.plugins.shell.state import SANDBOX_KIND, SHELL, ShellState, reduce_shell
from noeta.plugins.shell.tools import (
    LOST_REASON,
    STOP_JOB,
    BackgroundBash,
    MonitorTool,
    TaskStopTool,
)
from noeta.plugins.shell.worktree import EnterWorktreeTool, ExitWorktreeTool
from noeta.toolkit.notices import NOTICE_GUARD, STATUS_ORDER
from noeta.toolkit.sandbox import sandbox_of

__all__ = ["JOBS_LOST", "JOB_ENDED", "SHELL", "BashTool", "Registry", "Shell", "lost_notice"]

#: The ``External`` a pickup posts to its own task for the jobs still
#: running in its log that this worker does not have (``data = {"jobs":
#: [ids]}``): last in the inbox, so an end posted before is decided first.
JOBS_LOST = "shell_jobs_lost"


def lost_notice(job_id: str, job: Mapping[str, Any]) -> str:
    """What the model is told of a job whose worker is gone."""
    summary = lost_summary(str(job.get("description") or job.get("command") or job_id),
                           monitor=bool(job.get("monitor")), reason=LOST_REASON)
    text = task_notification(task_id=job_id, tool_use_id=str(job.get("tool_use_id") or ""),
                             output_file=str(job.get("output_file") or ""), status="failed",
                             summary=summary)
    return f"{NOTICE_GUARD}\n\n{text}"


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
      agent of a worker shares one table (8 running jobs per task). ``max_jobs`` /
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
        """Decide again what a crash may have left undecided (what this task
        took from its inbox while a job still shows running: the runner
        died between taking it and recording the decision; deciding twice
        changes nothing), then post ``JOBS_LOST`` for the jobs still
        running in the log that this worker does not have."""
        st = view.ext_state(self.name)
        if not isinstance(st, ShellState) or not any(
                j.get("status") == "running" for j in st.jobs.values()):
            return
        if view.read_log is not None:
            items = [i for r in await view.read_log(view.task_id)
                     if isinstance(r.event, InputTaken) for i in r.event.items]
            self._decide(view, items)
        gone = sorted(k for k, j in st.jobs.items()
                      if j.get("status") == "running" and not self.registry.live(view.task_id, k))
        if gone:
            view.enqueue(External(name=JOBS_LOST, data={"jobs": gone}))

    def _decide(self, view: TaskView, items: Sequence[InboxItem]) -> None:
        """Settle jobs from what the task took, in inbox order: a job's end
        makes it ended and tells the model; ``JOBS_LOST`` makes the jobs it
        names lost and tells the model so; anything about a job already
        settled is dropped. ``STOP_JOB`` (a parent's ``TaskStop``) stops the
        job here, its end told as any stop's. The notice is posted with the
        record (``enqueue``), so it opens a turn on an idle task."""
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
                    view.enqueue(Say(message=user(notice, origin="system"), when="now"),
                                 dedupe_key=_notice_key(job_id))
            elif body.name == JOBS_LOST:
                for job_id in body.data.get("jobs") or ():
                    job = jobs.get(str(job_id))
                    if job is None or job.get("status") != "running":
                        continue
                    job["status"] = "lost"
                    view.record(self.name, "lost", {"job_id": job_id})
                    view.enqueue(Say(message=user(lost_notice(job_id, job), origin="system"),
                                     when="now"), dedupe_key=_notice_key(job_id))
            elif body.name == STOP_JOB:
                running = self.registry.find(view.task_id, str(body.data.get("job_id") or ""))
                if running is not None:
                    self.registry.kill(running, quiet=True)

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        event = recorded.event
        task_id = view.state.task_id
        if isinstance(event, InputTaken):
            self._decide(view, event.items)
        elif isinstance(event, TaskEnded):
            self.registry.retire(task_id)
        elif isinstance(event, Waiting) and event.on == "input" and self.ends_with_turn:
            self.registry.kill_all(task_id)
