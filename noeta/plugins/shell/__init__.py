"""The shell: ``Bash`` (whose working directory carries over between
commands), and background jobs: ``run_in_background``, ``Monitor`` and
``TaskStop`` (both deferred).

Jobs belong to the worker process (a process-local table keyed by the task
tree's root, see :mod:`.jobs`), so while a job runs its worker holds the task
tree (``ToolContext.hold``): no other worker claims a task of it, and the
next turn can stop the job. What the log keeps is what a reader
needs: a ``started`` record in the task that launched the job, an ``ended``
record when its end notice is taken, and ``lost`` for a job missing from the
worker that picks the task up — its worker stopped and the hold ran out
(nothing is probed or killed by PID).

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
from noeta.plugins.shell.jobs import DEFAULT_LOG_DIR, REGISTRY, Registry
from noeta.plugins.shell.tools import LOST_REASON, BackgroundBash, MonitorTool, TaskStopTool

__all__ = ["SHELL", "BashTool", "Registry", "Shell", "lost_text"]


def lost_text(job_id: str) -> str:
    return f"Background job {job_id} was lost: {LOST_REASON}."

_NOTICE = re.compile(
    r"(?:\A|\n\n)<task-notification>\n<task-id>([0-9a-z]{9})</task-id>\n"
    r"(?:<tool-use-id>[^<]*</tool-use-id>\n)?(?:<output-file>[^<]*</output-file>\n)?"
    r"<status>(\w+)</status>")


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
        if not background:
            self._tools: list[Tool] = [BashTool(**(bash or {}))]
            return
        self.bash = BackgroundBash(self.registry, ends_with_turn=background_ends_with_turn,
                                   **(bash or {}))
        self._tools = [self.bash, MonitorTool(self.bash), TaskStopTool(self.registry)]

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
        status; one missing from this worker's table is lost (its worker
        held the tree while it ran, so that worker is gone)."""
        st = view.ext_state(self.name)
        jobs = st.jobs if isinstance(st, ShellState) else {}
        for job_id, job in jobs.items():
            if job.get("status") != "running":
                continue
            live = self.registry.get(job_id)
            if live is None:
                view.record(self.name, "lost", {"job_id": job_id},
                            message=user(lost_text(job_id), origin="system"))
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
                match = _NOTICE.search(body.message.text)
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
