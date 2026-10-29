"""``EnterWorktree`` / ``ExitWorktree``: move the shell into a git worktree
under ``.noeta/worktrees/`` and back.

The shell's working directory is what moves (``shell`` / ``cwd``, which
``Bash`` starts in): the task's root — its environment note, the file tools'
relative paths and the permission scope — stays the launch directory, so a
worktree lives inside it, also when the task started in a subdirectory of
the repository; the shell then starts in the worktree's copy of that
subdirectory. The worktree the tool made is recorded (``shell`` /
``worktree``) so ``ExitWorktree`` touches only that one.
"""

from __future__ import annotations

import posixpath
import re
import secrets
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.toolkit.tools import SCHEMA_DIALECT, load_description, tool_error
from noeta.toolkit.worktrees import (
    WORKTREES,
    add_worktree,
    git,
    remove_worktree,
    repository_top,
    unsaved_work,
)

__all__ = ["EnterWorktreeTool", "ExitWorktreeTool"]

SHELL = "shell"
_NAME = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
MAX_NAME = 64

ENTERED = ("Now working in the worktree {path} on branch {branch}: Bash commands start there. "
           "Give file tools absolute paths inside it. Leave it with ExitWorktree.")
SWITCHED = ("Now working in the worktree {path}: Bash commands start there. Give file tools "
            "absolute paths inside it. A worktree entered by path is never deleted by "
            "ExitWorktree: leave it with action \"keep\".")
NOT_ACTIVE = "No worktree session is active: nothing was changed."
KEPT = "Back in {root}. The worktree {path} (branch {branch}) is kept."
REMOVED = "Back in {root}. The worktree {path} and its branch {branch} were removed."
CHANGES = ("The worktree {path} was not removed: it has {what}. Confirm with the user, then "
           "call again with discard_changes: true to remove it anyway, or use action \"keep\".")
UNCHECKED = ("The worktree {path} was not removed: git could not tell whether it holds unsaved "
             "work ({error}). Confirm with the user, then call again with discard_changes: "
             "true to remove it anyway, or use action \"keep\".")


def name_problem(name: Any) -> str | None:
    """Why ``name`` cannot name a new worktree, else ``None``."""
    if not isinstance(name, str):
        return "the name must be a string"
    if len(name) > MAX_NAME:
        return f"the name is {len(name)} characters long; at most {MAX_NAME} are allowed"
    for segment in name.split("/"):
        if segment in (".", ".."):
            return f"the name {name!r} may not have a \".\" or \"..\" segment"
        if segment.lower().rstrip(".") == ".git":
            return f"the name {name!r} may not have a segment {segment!r}: git reserves it"
    if not _NAME.match(name):
        return (f"the name {name!r} may hold only letters, digits, dots, underscores and "
                "dashes, in non-empty \"/\"-joined segments")
    return None


def _worktree(ctx: ToolContext) -> dict[str, Any] | None:
    st = ctx.state.get(SHELL)
    tree = getattr(st, "worktree", None)
    return dict(tree) if isinstance(tree, dict) else None


class EnterWorktreeTool:
    """Creates a worktree on a new branch (or enters an existing one) and
    moves the shell into it."""

    name = "EnterWorktree"
    description = load_description("noeta.plugins.shell", "enter_worktree")
    input_schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": (
                "A name for a new worktree: letters, digits, dots, underscores and dashes, "
                "segments joined by \"/\", at most 64 characters; random when left out. Not "
                "together with `path`.")},
            "path": {"type": "string", "description": (
                "An existing worktree of this repository (as `git worktree list` shows) to "
                "move into instead of making one. Not together with `name`.")},
        },
        "additionalProperties": False,
    }
    read_only = False
    clearable = False
    deferred = True

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        name, path = input.get("name"), input.get("path")
        if name is not None and path is not None:
            return tool_error(self.name, "give `name` or `path`, not both")
        start = getattr(ctx.state.get(SHELL), "cwd", None) or ctx.env.cwd
        top = await repository_top(ctx.env, ctx.env.cwd)
        if top is None:
            return tool_error(self.name, f"{ctx.env.cwd} is not in a git repository")
        current = _worktree(ctx)
        if path is not None:
            return await self._enter_existing(ctx, str(path), top, current)
        if current is not None:
            return tool_error(self.name, f"already in the worktree {current['path']}; leave it "
                                         "with ExitWorktree first")
        if name is None:
            name = secrets.token_hex(4)
        problem = name_problem(name)
        if problem is not None:
            return tool_error(self.name, problem)
        assert isinstance(name, str)
        root = await ctx.env.realpath(ctx.env.cwd)
        target = posixpath.join(root, WORKTREES, name)
        branch = f"worktree-{name.replace('/', '-')}"
        code, _ = await git(ctx.env, ["rev-parse", "--verify", "--quiet", "origin/HEAD"], top)
        base = "origin/HEAD" if code == 0 else "HEAD"
        err = await add_worktree(ctx.env, top, target, branch, base)
        if err:
            return tool_error(self.name, err)
        _, base_sha = await git(ctx.env, ["rev-parse", "HEAD"], target)
        # Started below the repository's top: the same subdirectory of the
        # worktree, when the base commit has it.
        inside = target
        rel = posixpath.relpath(root, top)
        if rel != "." and not rel.startswith(".."):
            st = await ctx.env.stat(posixpath.join(target, rel))
            if st is not None and st.is_dir:
                inside = posixpath.join(target, rel)
        ctx.record(SHELL, "worktree", {"worktree": {
            "path": target, "branch": branch, "root": top, "base": base_sha,
            "created": True, "back": start}})
        ctx.record(SHELL, "cwd", {"cwd": inside})
        return text_output(ENTERED.format(path=inside, branch=branch), cwd=inside)

    async def _enter_existing(self, ctx: ToolContext, path: str, top: str,
                              current: dict[str, Any] | None) -> ToolOutput:
        target = posixpath.normpath(path if posixpath.isabs(path)
                                    else posixpath.join(ctx.env.cwd, path))
        code, listing = await git(ctx.env, ["worktree", "list", "--porcelain"], top)
        trees = [line[len("worktree "):] for line in listing.splitlines()
                 if line.startswith("worktree ")] if code == 0 else []
        root = await ctx.env.realpath(ctx.env.cwd)
        if not (target + "/").startswith(root.rstrip("/") + "/"):
            # Bash starts only inside the working directory.
            return tool_error(self.name, f"{target} is outside the working directory {root}")
        if target not in trees:
            return tool_error(self.name, f"{target} is not a worktree of this repository "
                                         "(see `git worktree list`)")
        if current is not None and not target.startswith(
                posixpath.join(root, WORKTREES) + "/"):
            return tool_error(self.name, "from a worktree, only another worktree under "
                                         f"{WORKTREES}/ can be entered")
        back = (current or {}).get("back") or ctx.env.cwd
        tracked = current if current is not None and current.get("created") else None
        ctx.record(SHELL, "worktree", {"worktree": {
            "path": target, "root": top, "created": False, "back": back,
            # The worktree this tool made stays the one an exit may remove.
            **({"made": tracked} if tracked else {})}})
        ctx.record(SHELL, "cwd", {"cwd": target})
        return text_output(SWITCHED.format(path=target), cwd=target)


class ExitWorktreeTool:
    """Moves the shell back to where it was before ``EnterWorktree``,
    keeping or removing the worktree that tool made."""

    name = "ExitWorktree"
    description = load_description("noeta.plugins.shell", "exit_worktree")
    input_schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["keep", "remove"], "description": (
                "\"keep\" leaves the worktree and its branch on disk; \"remove\" deletes "
                "both.")},
            "discard_changes": {"type": "boolean", "description": (
                "Set to true to remove a worktree that still holds files not yet committed or "
                "commits the starting branch lacks; otherwise the tool refuses and says what it "
                "found.")},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    read_only = False
    clearable = False
    deferred = True

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        tree = _worktree(ctx)
        if tree is None:
            return text_output(NOT_ACTIVE)
        action = input.get("action")
        if action not in ("keep", "remove"):
            return tool_error(self.name, "action must be \"keep\" or \"remove\"")
        made = tree if tree.get("created") else tree.get("made")
        back = str(tree.get("back") or ctx.env.cwd)
        if action == "remove" and isinstance(made, dict):
            what, failed = await unsaved_work(ctx.env, made["path"], made["base"])
            if (what or failed) and input.get("discard_changes") is not True:
                if failed:
                    return tool_error(self.name, UNCHECKED.format(path=made["path"],
                                                                  error=failed))
                return tool_error(self.name, CHANGES.format(path=made["path"], what=what))
            err = await remove_worktree(ctx.env, made["root"], made["path"], made["branch"])
            if err:
                return tool_error(self.name, err)
            self._leave(ctx, back)
            return text_output(REMOVED.format(root=back, path=made["path"],
                                              branch=made["branch"]), cwd=back)
        self._leave(ctx, back)
        kept = made if isinstance(made, dict) else tree
        return text_output(KEPT.format(root=back, path=kept["path"],
                                       branch=kept.get("branch") or "unchanged"), cwd=back)

    @staticmethod
    def _leave(ctx: ToolContext, back: str) -> None:
        ctx.record(SHELL, "worktree", {"worktree": None})
        ctx.record(SHELL, "cwd", {"cwd": back})
