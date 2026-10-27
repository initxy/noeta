"""Background shells: ``Bash`` with ``run_in_background``, ``BashOutput``,
``KillShell`` and ``Monitor``.

Jobs belong to the worker process (a process-local table keyed by the task
tree's root, see :mod:`.jobs`). What the log keeps is what a reader needs:
a ``started`` record in the task that launched the job, an ``ended`` record
when its end notice is taken, and ``lost`` for a job this process no longer
knows (a crash or restart killed it; nothing is probed or killed by PID).

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

from noeta.events import ExtensionRecorded, InputTaken, Recorded, Say, TaskEnded, Waiting
from noeta.ext.background_shell.jobs import DEFAULT_LOG_DIR, REGISTRY, Registry
from noeta.ext.background_shell.tools import (
    EXTENSION,
    BackgroundBash,
    BashOutputTool,
    KillShellTool,
    MonitorTool,
)
from noeta.ext.base import Extension, TaskView
from noeta.loop.state import CORE
from noeta.tools.base import Tool

__all__ = ["BackgroundShells", "Registry"]

_NOTICE = re.compile(
    r"\A<task-notification>\n<task-id>(bg-[0-9a-f]+)</task-id>\n"
    r"(?:<tool-use-id>[^<]*</tool-use-id>\n)?<status>(\w+)</status>")


class BackgroundShells(Extension):
    """Background jobs for ``Bash`` and ``Monitor``.

    Arguments a host sets:

    * ``background_ends_with_turn`` — end a task's jobs when its turn ends
      (one-shot workers); the launch results then say so (0.6's
      ``HostConfig.background_ends_with_turn``).
    * ``bash`` — keyword arguments for the ``Bash`` this extension offers
      (``timeout_s``, ``output_cap``, ``shell_env``, ``hardening_rules``),
      the same as ``noeta.tools.BashTool``'s.
    * ``registry`` — the job table; by default the process-wide one, so every
      agent of a worker shares the per-root cap of 8 jobs. ``max_jobs`` /
      ``output_cap`` / ``log_dir`` configure a private one instead.

    Ordering: this extension offers ``Bash`` too, and the first extension
    offering a name wins, so it goes before ``FileTools``.
    """

    name = EXTENSION

    def __init__(self, *, background_ends_with_turn: bool = False,
                 bash: dict[str, Any] | None = None, registry: Registry | None = None,
                 max_jobs: int | None = None, output_cap: int | None = None,
                 log_dir: str | None = None) -> None:
        if registry is None and (max_jobs or output_cap or log_dir):
            registry = Registry(max_jobs=max_jobs or 8, output_cap=output_cap or 256 * 1024,
                                log_dir=log_dir or DEFAULT_LOG_DIR)
        self.registry = registry if registry is not None else REGISTRY
        self.ends_with_turn = background_ends_with_turn
        self.bash = BackgroundBash(self.registry, ends_with_turn=background_ends_with_turn,
                                   **(bash or {}))
        self._tools: list[Tool] = [self.bash, BashOutputTool(self.registry),
                                   KillShellTool(self.registry), MonitorTool(self.bash)]

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        jobs = dict(state or {})
        if kind == "started":
            jobs[data["job_id"]] = {**data, "status": "running"}
        elif kind in ("ended", "lost") and data.get("job_id") in jobs:
            job = dict(jobs[data["job_id"]])
            job["status"] = data.get("status") or "lost"
            if data.get("exit_code") is not None:
                job["exit_code"] = data["exit_code"]
            jobs[data["job_id"]] = job
        return jobs

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools

    # ------------------------------------------------------------ lifecycle

    async def on_start(self, view: TaskView) -> None:
        """Settle jobs the log still shows running: ended ones get their
        status, ones this process does not know are lost."""
        for job_id, job in (view.ext_state(self.name) or {}).items():
            if job.get("status") != "running":
                continue
            live = self.registry.get(job_id)
            if live is None:
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
            known = view.ext_state(self.name) or {}
            for item in event.items:
                body = item.body
                if not isinstance(body, Say) or body.message.origin != "system":
                    continue
                match = _NOTICE.match(body.message.text)
                if match and known.get(match.group(1), {}).get("status") == "running":
                    view.record(self.name, "ended",
                                {"job_id": match.group(1), "status": match.group(2)})
        elif isinstance(event, TaskEnded):
            self.registry.ended.add(state.task_id)
            if state.parent_id is None:
                self.registry.retire(root)
            elif self.ends_with_turn:
                self.registry.kill_where(root, spawner_id=state.task_id)
        elif isinstance(event, Waiting) and event.on == "input" and self.ends_with_turn:
            self.registry.kill_where(root, spawner_id=state.task_id)
        elif (isinstance(event, ExtensionRecorded) and event.extension == CORE
              and event.kind == "turn_stopped" and state.parent_id is None):
            self.registry.kill_where(root)
