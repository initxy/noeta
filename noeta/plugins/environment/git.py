"""The git snapshot: captured through the task's exec env with git's
code-running hooks off, and rendered for the first user turn."""

from __future__ import annotations

import posixpath

from noeta.core.execenv import ExecEnv
from noeta.toolkit.shell import GIT_FILTER_DRIVERS, GIT_READ_FLAGS, harden_git, untrusted_filters

#: Git status longer than this many characters is cut there (Claude Code's
#: cut), and the note below follows.
GIT_STATUS_MAX_CHARS = 2000
GIT_STATUS_CUT_NOTE = ("\n... (cut here: the status runs past 2,000 characters; for the "
                       "full list run `git status` with Bash)")


#: Commits listed in the git snapshot.
RECENT_COMMITS = 5
#: The snapshot's second line: taken at the start, or again for a message.
SNAPSHOT_AT_START = ("A snapshot taken when this conversation started; it does not change as "
                     "you work.")
SNAPSHOT_AGAIN = ("Taken again when the latest message arrived; it replaces the earlier "
                  "snapshot and does not change as you work.")


async def _git(env: ExecEnv, cwd: str, *args: str) -> str:
    return await _git_or_none(env, cwd, *args) or ""


async def _git_or_none(env: ExecEnv, cwd: str, *args: str) -> str | None:
    """git's output, ``None`` when it failed or timed out."""
    argv = ["git", *args]
    if args and args[0] == "status":
        argv = harden_git(argv)
    argv = [argv[0], *GIT_READ_FLAGS, *argv[1:]]
    try:
        out = await env.run(argv, cwd=cwd, timeout=5, output_cap=1 << 20)
    except Exception:  # noqa: BLE001 — capture is best-effort
        return None
    if out.exit_code != 0 or out.timed_out:
        return None
    return out.stdout.decode("utf-8", errors="replace")


async def _repo_defines_filters(env: ExecEnv, cwd: str) -> bool:
    """Whether the repository's own config defines a filter driver (whose
    ``clean`` program ``git status`` would run). Unsure counts as yes."""
    argv = ["git", *GIT_READ_FLAGS, *GIT_FILTER_DRIVERS]
    try:
        out = await env.run(argv, cwd=cwd, timeout=5, output_cap=1 << 20)
    except Exception:  # noqa: BLE001 — capture is best-effort
        return True
    return out.timed_out or untrusted_filters(
        out.exit_code, out.stdout.decode("utf-8", errors="replace"))


async def _git_status(env: ExecEnv, cwd: str) -> str | None:
    """The short status (``""`` when clean), ``None`` when not captured
    (skipped, or git failed or timed out: a huge tree such as a home
    directory kept as a dotfiles repository)."""
    if await _repo_defines_filters(env, cwd):
        return None
    # Not inside submodules: their own config's filters are not checked.
    out = await _git_or_none(env, cwd, "status", "--short", "--ignore-submodules=dirty")
    if out is None:
        return None
    status = out.strip()
    if len(status) <= GIT_STATUS_MAX_CHARS:
        return status
    return status[:GIT_STATUS_MAX_CHARS] + GIT_STATUS_CUT_NOTE


async def _main_branch(env: ExecEnv, cwd: str) -> str:
    """The branch pull requests usually target, as Claude Code finds it:
    the remote's default (``origin/HEAD``, when the branch it names exists),
    else ``origin/main`` or ``origin/master``, else ``main``."""
    remote = (await _git(env, cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")).strip()
    name = remote.split("/", 1)[-1] if remote.startswith("origin/") else ""
    for candidate in ([name] if name else []) + ["main", "master"]:
        if (await _git(env, cwd, "rev-parse", "--verify", "--quiet",
                       f"refs/remotes/origin/{candidate}")).strip():
            return candidate
    return "main"


async def git_root(env: ExecEnv, cwd: str) -> str | None:
    """The top of the git checkout ``cwd`` is in: the nearest of ``cwd`` and
    its parents holding a ``.git`` (a directory, or a linked worktree's
    file), looked up through ``env``; ``None`` outside any checkout."""
    try:
        return await _find_root(env, cwd)
    except Exception:  # noqa: BLE001 — capture is best-effort
        return None


async def is_worktree(env: ExecEnv, cwd: str) -> bool:
    """Whether ``cwd`` is in a linked worktree (its git directory is not the
    repository's common one; a submodule's is)."""
    out = (await _git(env, cwd, "rev-parse", "--git-dir", "--git-common-dir")).splitlines()
    if len(out) != 2:
        return False
    own, common = (posixpath.normpath(posixpath.join(cwd, d.strip())) for d in out)
    return own != common


async def _find_root(env: ExecEnv, cwd: str) -> str | None:
    """:func:`git_root`, raising what the env raised."""
    path = posixpath.normpath(cwd)
    while True:
        if await env.stat(posixpath.join(path, ".git")) is not None:
            return path
        parent = posixpath.dirname(path)
        if parent == path:
            return None
        path = parent


async def capture_git(env: ExecEnv, cwd: str, *, when: str = SNAPSHOT_AT_START) -> str:
    """The git snapshot for the first user turn (``""`` outside a repository)."""
    if await git_root(env, cwd) is None:
        return ""
    branch = (await _git(env, cwd, "rev-parse", "--abbrev-ref", "HEAD")).strip()
    status = await _git_status(env, cwd)
    commits = (await _git(env, cwd, "log", "--oneline", "-n", str(RECENT_COMMITS))).rstrip("\n")
    return render_git(branch=branch, main=await _main_branch(env, cwd),
                      user=(await _git(env, cwd, "config", "user.name")).strip(),
                      status=status, commits=commits, when=when)


async def git_head(env: ExecEnv, cwd: str) -> str:
    """The branch and the commit it is at (``""`` outside a repository): what
    the person changes by switching branch or committing, which the model's
    own edits do not."""
    if await git_root(env, cwd) is None:
        return ""
    return (await _git(env, cwd, "rev-parse", "HEAD", "--abbrev-ref", "HEAD")).strip()


def render_git(*, branch: str, main: str, user: str, status: str | None,
               commits: str, when: str = SNAPSHOT_AT_START) -> str:
    shown = "(not captured)" if status is None else status or "(clean)"
    # Like Claude Code: no user line when none is set, an empty commit list
    # in a repository without commits.
    who = [f"Git user: {user}", ""] if user else []
    return "\n".join([
        "Here is context from the workspace that may help you answer:",
        "# Git status",
        when,
        "",
        f"Current branch: {branch or '(unknown)'}",
        "",
        f"Main branch (where pull requests usually go): {main or '(unknown)'}",
        "",
        *who,
        f"Status:\n{shown}",
        "",
        f"Recent commits:\n{commits}",
        "",
        "noeta attached this on its own; the user did not write it. It is about the "
        "user's own checkout and account, so there is no need to tell it back to them.",
    ])
