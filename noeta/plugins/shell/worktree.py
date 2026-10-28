"""``EnterWorktree`` / ``ExitWorktree``: move the shell into a git worktree
under ``.noeta/worktrees/`` and back.

The shell's working directory is what moves (``shell`` / ``cwd``, which
``Bash`` starts in): the task's root — its environment note, the file tools'
relative paths and the permission scope — stays the launch directory, so a
worktree lives inside it. The worktree the tool made is recorded
(``shell`` / ``worktree``) so ``ExitWorktree`` touches only that one.
"""

from __future__ import annotations

import posixpath
import re
import secrets
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.toolkit.tools import SCHEMA_DIALECT, load_description, tool_error

__all__ = ["EnterWorktreeTool", "ExitWorktreeTool"]

SHELL = "shell"
#: Where new worktrees go, under the repository's top directory.
WORKTREES = ".noeta/worktrees"
_NAME = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
MAX_NAME = 64

ENTERED = ("Now working in the worktree {path} on branch {branch}: Bash commands start there. "
           "Give file tools absolute paths inside it. Leave it with ExitWorktree.")
SWITCHED = ("Now working in the worktree {path}: Bash commands start there. Give file tools "
            "absolute paths inside it. ExitWorktree will not remove a worktree entered this "
            "way; use action \"keep\" to go back.")
NOT_ACTIVE = "No worktree session is active: nothing was changed."
KEPT = "Back in {root}. The worktree {path} (branch {branch}) is kept."
REMOVED = "Back in {root}. The worktree {path} and its branch {branch} were removed."
CHANGES = ("The worktree {path} was not removed: it has {what}. Confirm with the user, then "
           "call again with discard_changes: true to remove it anyway, or use action \"keep\".")


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


async def _git(ctx: ToolContext, args: list[str], cwd: str) -> tuple[int, str]:
    out = await ctx.env.run(["git", *args], cwd=cwd)
    text = (out.stdout if out.exit_code == 0 else out.stderr or out.stdout)
    return out.exit_code, text.decode(errors="replace").strip()


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
        code, top = await _git(ctx, ["rev-parse", "--show-toplevel"], ctx.env.cwd)
        if code != 0:
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
        target = posixpath.join(top, WORKTREES, name)
        branch = f"worktree-{name.replace('/', '-')}"
        code, _ = await _git(ctx, ["rev-parse", "--verify", "--quiet", "origin/HEAD"], top)
        base = "origin/HEAD" if code == 0 else "HEAD"
        code, err = await _git(ctx, ["worktree", "add", "-b", branch, target, base], top)
        if code != 0:
            return tool_error(self.name, err or "git worktree add failed")
        _, base_sha = await _git(ctx, ["rev-parse", "HEAD"], target)
        ctx.record(SHELL, "worktree", {"worktree": {
            "path": target, "branch": branch, "root": top, "base": base_sha,
            "created": True, "back": start}})
        ctx.record(SHELL, "cwd", {"cwd": target})
        return text_output(ENTERED.format(path=target, branch=branch), cwd=target)

    async def _enter_existing(self, ctx: ToolContext, path: str, top: str,
                              current: dict[str, Any] | None) -> ToolOutput:
        target = posixpath.normpath(path if posixpath.isabs(path)
                                    else posixpath.join(ctx.env.cwd, path))
        code, listing = await _git(ctx, ["worktree", "list", "--porcelain"], top)
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
                posixpath.join(current["root"], WORKTREES) + "/"):
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
                "Needed (true) to remove a worktree with uncommitted files or commits not on "
                "the original branch; without it the tool refuses and lists them.")},
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
            what = await self._changes(ctx, made)
            if what and input.get("discard_changes") is not True:
                return tool_error(self.name, CHANGES.format(path=made["path"], what=what))
            code, err = await _git(ctx, ["worktree", "remove", "--force", made["path"]],
                                   made["root"])
            if code != 0:
                return tool_error(self.name, err or "git worktree remove failed")
            await _git(ctx, ["branch", "-D", made["branch"]], made["root"])
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

    @staticmethod
    async def _changes(ctx: ToolContext, tree: dict[str, Any]) -> str:
        """What removing ``tree`` would lose, in words; empty when nothing."""
        lost = []
        code, status = await _git(ctx, ["status", "--porcelain"], tree["path"])
        if code == 0 and status:
            lost.append(f"{len(status.splitlines())} uncommitted file(s)")
        code, ahead = await _git(ctx, ["rev-list", "--count", f"{tree['base']}..HEAD"],
                                 tree["path"])
        if code == 0 and ahead.isdigit() and int(ahead):
            lost.append(f"{ahead} commit(s) not on the original branch")
        return " and ".join(lost)
