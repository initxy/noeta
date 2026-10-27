"""The shell: ``Bash`` (whose working directory carries over between
commands), and background jobs: ``run_in_background``, ``BashOutput``,
``KillShell`` and ``Monitor``.

Jobs belong to the worker process (a process-local table keyed by the task
tree's root, see :mod:`.jobs`). What the log keeps is what a reader needs:
a ``started`` record in the task that launched the job (with the worker that
runs it), an ``ended`` record when its end notice is taken, and ``lost`` for
a job whose worker is gone (a crash or restart killed it; nothing is probed
or killed by PID).

**Background jobs pin their task tree to one worker.** A task releases its
claim whenever it waits (for input, an approval, children), and another
worker may pick it up next. That worker cannot read, stop or clean up a job
running in the first one: the model is told once that the job "is running in
another worker process; it cannot be read or stopped from here", and the job
is stopped only by its own worker (when the root ends, is cancelled or its
turn is stopped there). Deployments with several workers that offer
background shells should route every task of a tree to the same worker
(e.g. by queue, ``ctx.spawn(..., queue=...)``); there is no cross-worker job
control.

A job's end — and each monitor event — reaches the model as Claude Code's
``<task-notification>``, a system-origin message posted to the task that
started it (the root once that task has ended). Stopping the root's turn,
cancelling or ending the root kills every job of the tree without a notice;
with ``background_ends_with_turn`` a job also dies, silently, when the turn
of the task that started it ends.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from noeta.core.events import ExtensionRecorded, InputTaken, Recorded, Say, TaskEnded, Waiting
from noeta.core.extension import Extension, TaskView
from noeta.core.loop.state import CORE
from noeta.core.tool import Tool
from noeta.core.types import user
from noeta.plugins.shell.bash import SHELL, BashTool, ShellState, reduce_shell
from noeta.plugins.shell.jobs import DEFAULT_LOG_DIR, REGISTRY, Registry, worker_alive
from noeta.plugins.shell.tools import (
    BackgroundBash,
    BashOutputTool,
    KillShellTool,
    MonitorTool,
)

__all__ = ["SHELL", "BashTool", "Registry", "Shell", "elsewhere_text"]


def elsewhere_text(job_id: str) -> str:
    return (f"Background job {job_id} is running in another worker process; it cannot be "
            "read or stopped from here.")

_NOTICE = re.compile(
    r"\A<task-notification>\n<task-id>(bg-[0-9a-f]+)</task-id>\n"
    r"(?:<tool-use-id>[^<]*</tool-use-id>\n)?<status>(\w+)</status>")


class Shell(Extension):
    """``Bash``, and background jobs for ``Bash`` and ``Monitor``.

    Arguments a host sets:

    * ``background`` — offer background jobs (``run_in_background``,
      ``BashOutput``, ``KillShell``, ``Monitor``); off = foreground ``Bash``
      only.
    * ``background_ends_with_turn`` — end a task's jobs when its turn ends
      (one-shot workers); the launch results then say so (0.6's
      ``HostConfig.background_ends_with_turn``).
    * ``bash`` — keyword arguments for the ``Bash`` this extension offers
      (``timeout_s``, ``output_cap``, ``shell_env``, ``hardening_rules``),
      the same as ``BashTool``'s.
    * ``registry`` — the job table; by default the process-wide one, so every
      agent of a worker shares the per-root cap of 8 jobs. ``max_jobs`` /
      ``output_cap`` / ``log_dir`` configure a private one instead.
    """

    name = SHELL

    def __init__(self, *, background: bool = True, background_ends_with_turn: bool = False,
                 bash: dict[str, Any] | None = None, registry: Registry | None = None,
                 max_jobs: int | None = None, output_cap: int | None = None,
                 log_dir: str | None = None) -> None:
        if registry is None and (max_jobs or output_cap or log_dir):
            registry = Registry(max_jobs=max_jobs or 8, output_cap=output_cap or 256 * 1024,
                                log_dir=log_dir or DEFAULT_LOG_DIR)
        self.registry = registry if registry is not None else REGISTRY
        self.ends_with_turn = background_ends_with_turn
        if not background:
            self._tools: list[Tool] = [BashTool(**(bash or {}))]
            return
        self.bash = BackgroundBash(self.registry, ends_with_turn=background_ends_with_turn,
                                   **(bash or {}))
        self._tools = [self.bash, BashOutputTool(self.registry),
                       KillShellTool(self.registry), MonitorTool(self.bash)]

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return ShellState()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return reduce_shell(state, kind, data)

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools

    # ------------------------------------------------------------ lifecycle

    async def on_start(self, view: TaskView) -> None:
        """Settle jobs the log still shows running: ended ones get their
        status; one running in another live worker is noted to the model
        (once per picking worker); one whose worker is gone is lost."""
        st = view.ext_state(self.name)
        jobs = st.jobs if isinstance(st, ShellState) else {}
        for job_id, job in jobs.items():
            if job.get("status") != "running":
                continue
            live = self.registry.get(job_id)
            owner = job.get("worker")
            if live is None and owner and owner != self.registry.worker \
                    and worker_alive(owner):
                if job.get("noted") != self.registry.worker:
                    view.record(self.name, "elsewhere",
                                {"job_id": job_id, "worker": self.registry.worker},
                                message=user(elsewhere_text(job_id), origin="system"))
            elif live is None:
                view.record(self.name, "lost", {"job_id": job_id})
            elif live.status != "running":
                code = live.exit_code
                status = "killed" if live.status == "killed" else (
                    "completed" if code == 0 else "failed")
                view.record(self.name, "ended",
                            {"job_id": job_id, "status": status, "exit_code": code})

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        event = recorded.event
        state = view.state
        root = state.root_id or state.task_id
        if isinstance(event, InputTaken):
            st = view.ext_state(self.name)
            known = st.jobs if isinstance(st, ShellState) else {}
            for item in event.items:
                body = item.body
                if not isinstance(body, Say) or body.message.origin != "system":
                    continue
                match = _NOTICE.match(body.message.text)
                if match and known.get(match.group(1), {}).get("status") == "running":
                    view.record(self.name, "ended",
                                {"job_id": match.group(1), "status": match.group(2)})
        elif isinstance(event, TaskEnded):
            self.registry.mark_ended(state.task_id, root)
            if state.parent_id is None:
                self.registry.retire(root)
            elif self.ends_with_turn:
                self.registry.kill_where(root, spawner_id=state.task_id)
        elif isinstance(event, Waiting) and event.on == "input" and self.ends_with_turn:
            self.registry.kill_where(root, spawner_id=state.task_id)
        elif (isinstance(event, ExtensionRecorded) and event.extension == CORE
              and event.kind == "turn_stopped" and state.parent_id is None):
            self.registry.kill_where(root)
