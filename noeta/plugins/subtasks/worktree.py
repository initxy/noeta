"""The git worktree an isolated agent works in: made when it starts,
removed when it ends unchanged."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from typing import Any

from noeta.core.execenv import ExecEnv
from noeta.core.extension import TaskView
from noeta.core.tool import ToolContext
from noeta.plugins.subtasks import texts
from noeta.toolkit.worktrees import (
    WORKTREES,
    add_worktree,
    git,
    remove_worktree,
    repository_top,
    unsaved_work,
)


async def make_agent_worktree(ctx: ToolContext,
                              config: Mapping[str, Any]) -> dict[str, str] | str:
    """A fresh git worktree for an isolated agent, or why there is none."""
    env = ctx.env
    if env is None:
        return texts.WORKTREE_FAILED.format(reason="this task has no execution environment")
    cwd = str(config.get("cwd") or env.cwd)
    root = await repository_top(env, cwd)
    if root is None:
        return texts.WORKTREE_FAILED.format(reason=f"{cwd} is not in a git repository")
    _, base = await git(env, ["rev-parse", "HEAD"], root)
    path, branch = _place(root, ctx.call_id)
    err = await add_worktree(env, root, path, branch, "HEAD")
    if err:
        return texts.WORKTREE_FAILED.format(reason=err)
    return {"path": path, "branch": branch, "base": base, "root": root}


def _place(root: str, call_id: str) -> tuple[str, str]:
    """The path and branch of the worktree the call ``call_id`` makes."""
    name = "agent-" + re.sub(r"[^A-Za-z0-9_-]", "", call_id)[-16:]
    return posixpath.join(root, WORKTREES, name), f"worktree-{name}"


async def remove_unlaunched_worktree(ctx: ToolContext, config: Mapping[str, Any]) -> None:
    """Remove the worktree this call made before a crash stopped it from
    launching its agent (the launch and its record are one write, so no
    agent ever worked in it)."""
    env = ctx.env
    if env is None:
        return
    root = await repository_top(env, str(config.get("cwd") or env.cwd))
    if root is None:
        return
    path, branch = _place(root, ctx.call_id)
    if await env.stat(path) is not None:
        await remove_worktree(env, root, path, branch)


async def worktree_exists(env: ExecEnv | None, tree: Mapping[str, str]) -> bool:
    """Whether the agent's worktree directory is still there."""
    return env is not None and await env.stat(tree["path"]) is not None


async def close_worktree(view: TaskView, tree: Mapping[str, str]) -> str:
    """Remove an unchanged worktree; otherwise (or when git cannot tell)
    say where the work is. A worktree already gone keeps nothing."""
    env = view.env
    if env is None or not await worktree_exists(env, tree):
        return ""
    lost, failed = await unsaved_work(env, tree["path"], tree["base"])
    if not lost and not failed:
        await remove_worktree(env, tree["root"], tree["path"], tree["branch"])
        return ""
    return texts.WORKTREE_KEPT.format(path=tree["path"], branch=tree["branch"])
