"""The git worktree an isolated agent works in: made when it starts,
removed when it ends unchanged."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from noeta.core.extension import TaskView
from noeta.core.tool import ToolContext
from noeta.plugins.subtasks import texts


async def add_worktree(ctx: ToolContext, config: Mapping[str, Any]) -> dict[str, str] | str:
    """A fresh git worktree for an isolated agent, or why there is none."""
    env = ctx.env
    if env is None:
        return texts.WORKTREE_FAILED.format(reason="this task has no execution environment")
    cwd = str(config.get("cwd") or env.cwd)
    top = await env.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if top.exit_code != 0:
        return texts.WORKTREE_FAILED.format(reason=f"{cwd} is not in a git repository")
    root = top.stdout.decode().strip()
    base = (await env.run(["git", "rev-parse", "HEAD"], cwd=root)).stdout.decode().strip()
    name = "agent-" + re.sub(r"[^A-Za-z0-9_-]", "", ctx.call_id)[-16:]
    path, branch = f"{root}/.noeta/worktrees/{name}", f"worktree-{name}"
    made = await env.run(["git", "worktree", "add", "-b", branch, path, "HEAD"], cwd=root)
    if made.exit_code != 0:
        reason = made.stderr.decode(errors="replace").strip() or "git worktree add failed"
        return texts.WORKTREE_FAILED.format(reason=reason)
    return {"path": path, "branch": branch, "base": base, "root": root}


async def close_worktree(view: TaskView, tree: Mapping[str, str]) -> str:
    """Remove an unchanged worktree; otherwise say where the work is."""
    env = view.env
    if env is None:
        return ""
    status = await env.run(["git", "status", "--porcelain"], cwd=tree["path"])
    ahead = await env.run(["git", "rev-list", "--count", f"{tree['base']}..HEAD"],
                          cwd=tree["path"])
    if status.exit_code == 0 and not status.stdout.strip() \
            and ahead.stdout.decode().strip() == "0":
        await env.run(["git", "worktree", "remove", "--force", tree["path"]],
                      cwd=tree["root"])
        await env.run(["git", "branch", "-D", tree["branch"]], cwd=tree["root"])
        return ""
    return texts.WORKTREE_KEPT.format(path=tree["path"], branch=tree["branch"])
