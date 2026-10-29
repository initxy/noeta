"""The shell's folded state: where the next ``Bash`` command starts, the
background jobs and the worktree, rebuilt from what the shell recorded."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = ["SHELL", "ShellState", "reduce_shell", "shell_state"]

#: The extension name the shell records under (``cwd``, and the background
#: job kinds).
SHELL = "shell"


@dataclass(frozen=True)
class ShellState:
    #: Where the next ``Bash`` command starts; ``None`` = the workspace root.
    cwd: str | None = None
    #: Background job id -> what the log says about it.
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The worktree ``EnterWorktree`` moved the shell into (``worktree.py``).
    worktree: dict[str, Any] | None = None


def reduce_shell(state: ShellState | None, kind: str, data: Any) -> ShellState:
    """``state`` with one recorded entry applied. Never mutates ``state``."""
    base = state if isinstance(state, ShellState) else ShellState()
    if not isinstance(data, Mapping):
        return base
    if kind == "cwd":
        cwd = data.get("cwd")
        return base if cwd is not None and not isinstance(cwd, str) \
            else replace(base, cwd=cwd or None)
    if kind == "worktree":
        tree = data.get("worktree")
        return replace(base, worktree=dict(tree) if isinstance(tree, Mapping) else None)
    jobs = dict(base.jobs)
    job_id = data.get("job_id")
    if not isinstance(job_id, str):
        return base
    if kind == "started":
        jobs[job_id] = {**data, "status": "running"}
    elif kind in ("ended", "lost") and job_id in jobs:
        job = dict(jobs[job_id])
        job["status"] = data.get("status") or "lost"
        if data.get("exit_code") is not None:
            job["exit_code"] = data["exit_code"]
        jobs[job_id] = job
    else:
        return base
    return replace(base, jobs=jobs)


def shell_state(state: Mapping[str, Any]) -> ShellState:
    """The ``shell`` entry of a tool context's folded state."""
    raw = state.get(SHELL)
    return raw if isinstance(raw, ShellState) else ShellState()
