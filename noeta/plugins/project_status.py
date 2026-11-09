"""The project and git branch of the task's directory, as status items every
front end shows (what Claude Code's status-line plugins show): the project —
the top folder of the git checkout the directory is in (a linked worktree's
own folder), else the directory's name — and, in a checkout, the branch (the
short commit when detached), ``*`` when the work tree has changes and
``↑2 ↓1`` against the branch's upstream.

The directory is where the task's shell is (after a ``cd`` or
``EnterWorktree``), else the task's own. Front ends ask ``ui`` with no exec
env, so git runs as a process of this machine, each command stopped after
:data:`GIT_TIMEOUT`, and an answer is reused for :data:`FRESH_SECONDS` per
directory. A directory this machine lacks (one inside a container) shows
nothing.

Nothing of the repository's own config may start a program: git runs with no
fsmonitor, hooks or signature program (``GIT_READ_FLAGS``), and ``status``
not at all where the repository's config defines a filter driver (whose
``clean`` program it would run): there the dirty mark is left out. The
branch is read from ``HEAD`` itself."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from asyncio.subprocess import DEVNULL, PIPE
from collections.abc import Sequence
from dataclasses import dataclass

from noeta.core.extension import Extension, StatusItem, TaskView
from noeta.toolkit.local_env import child_env
from noeta.toolkit.plugin_records import shell_cwd
from noeta.toolkit.shell import GIT_FILTER_DRIVERS, GIT_READ_FLAGS, harden_git, untrusted_filters

__all__ = ["Checkout", "ProjectStatus", "branch_text", "read_checkout"]

log = logging.getLogger("noeta.plugins.project_status")

#: Seconds one directory's answer is reused for, and after a git command
#: there ran out of time (a huge tree: a home directory kept as a repository).
FRESH_SECONDS = 2.0
SLOW_SECONDS = 30.0
#: Seconds each git command may take before it is stopped.
GIT_TIMEOUT = 1.0
#: Characters of a detached HEAD's commit shown.
SHORT_COMMIT = 7
#: What a reftable repository's ``HEAD`` file names, whatever is checked out.
_REFTABLE_HEAD = "refs/heads/.invalid"
#: Variables that would point git at another repository than the directory's.
_REPOSITORY_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


@dataclass(frozen=True)
class Checkout:
    """What the status items say of one directory."""

    project: str
    #: The branch, or the short commit of a detached HEAD; ``""`` outside git.
    branch: str = ""
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    #: A git command ran out of time.
    slow: bool = False


def branch_text(found: Checkout) -> str:
    """``⎇ main* ↑2 ↓1``: the branch, ``*`` when dirty, ahead and behind."""
    text = f"⎇ {found.branch}{'*' if found.dirty else ''}"
    if found.ahead:
        text += f" ↑{found.ahead}"
    if found.behind:
        text += f" ↓{found.behind}"
    return text


class ProjectStatus(Extension):
    """The project and git branch of the task's directory, under the footer."""

    name = "project_status"

    def __init__(self) -> None:
        #: directory -> (until when its answer holds, the answer)
        self._seen: dict[str, tuple[float, Checkout]] = {}

    async def ui(self, view: TaskView) -> Sequence[StatusItem]:
        directory = shell_cwd(view.state.ext) or view.cwd
        if not os.path.isabs(directory) or not os.path.isdir(directory):
            return ()
        found = await self._checkout(os.path.normpath(directory))
        items = [StatusItem(key="project", text=found.project)]
        if found.branch:
            items.append(StatusItem(key="branch", text=branch_text(found)))
        return items

    async def _checkout(self, directory: str) -> Checkout:
        now = time.monotonic()
        seen = self._seen.get(directory)
        if seen is not None and now < seen[0]:
            return seen[1]
        found = await read_checkout(directory)
        self._seen = {d: s for d, s in self._seen.items() if s[0] > now}
        self._seen[directory] = (now + (SLOW_SECONDS if found.slow else FRESH_SECONDS), found)
        return found


async def read_checkout(directory: str) -> Checkout:
    """What :class:`ProjectStatus` shows for ``directory`` (an existing
    absolute path), read afresh."""
    top = _checkout_top(directory)
    if top is None:
        return Checkout(project=os.path.basename(directory) or directory)
    project = os.path.basename(top) or top
    branch = _head(top)
    try:
        code, filters = await _git(top, *GIT_FILTER_DRIVERS)
        if not untrusted_filters(code, filters):
            code, status = await _git(top, "status", "--porcelain=v2", "--branch",
                                      "--ignore-submodules=dirty")
            if code == 0:
                return _from_status(project, branch, status)
        code, counts = await _git(top, "rev-list", "--count", "--left-right",
                                  "HEAD...@{upstream}")
        ahead, behind = _counts(counts) if code == 0 else (0, 0)
        return Checkout(project=project, branch=branch, ahead=ahead, behind=behind)
    except TimeoutError:
        log.debug("project_status: git ran out of time in %s", top)
        return Checkout(project=project, branch=branch, slow=True)
    except OSError as exc:
        log.debug("project_status: git did not start in %s: %s", top, exc)
        return Checkout(project=project, branch=branch)


def _checkout_top(directory: str) -> str | None:
    """The nearest of ``directory`` and its parents holding a ``.git`` (a
    directory, or a linked worktree's or submodule's file)."""
    path = directory
    while True:
        if os.path.lexists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _head(top: str) -> str:
    """The branch ``HEAD`` names, or its short commit when detached; ``""``
    when unreadable (or a reftable repository's, which names none)."""
    dot_git = os.path.join(top, ".git")
    try:
        if os.path.isfile(dot_git):  # a linked worktree or a submodule
            with open(dot_git, encoding="utf-8") as f:
                pointer = f.read().strip()
            if not pointer.startswith("gitdir:"):
                return ""
            dot_git = os.path.join(top, pointer.removeprefix("gitdir:").strip())
        with open(os.path.join(dot_git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if head.startswith("ref:"):
        ref = head.removeprefix("ref:").strip()
        return "" if ref == _REFTABLE_HEAD else ref.removeprefix("refs/heads/")
    return head[:SHORT_COMMIT]


def _from_status(project: str, branch: str, status: str) -> Checkout:
    """:class:`Checkout` from ``git status --porcelain=v2 --branch``."""
    oid = head = ""
    ahead = behind = 0
    dirty = False
    for line in status.splitlines():
        if line.startswith("# branch.oid "):
            oid = line.removeprefix("# branch.oid ")
        elif line.startswith("# branch.head "):
            head = line.removeprefix("# branch.head ")
        elif line.startswith("# branch.ab "):
            plus, _, minus = line.removeprefix("# branch.ab ").partition(" ")
            ahead, behind = _number(plus.lstrip("+")), _number(minus.lstrip("-"))
        elif line and not line.startswith("#"):
            dirty = True
    if head == "(detached)":
        head = oid[:SHORT_COMMIT] if oid and oid != "(initial)" else ""
    return Checkout(project=project, branch=head or branch, dirty=dirty,
                    ahead=ahead, behind=behind)


def _counts(out: str) -> tuple[int, int]:
    """Ahead and behind from ``git rev-list --count --left-right A...B``."""
    left, _, right = out.strip().partition("\t")
    return _number(left), _number(right)


def _number(text: str) -> int:
    return int(text) if text.isdigit() else 0


async def _git(cwd: str, *args: str) -> tuple[int, str]:
    """``git args`` run in ``cwd`` with :data:`GIT_READ_FLAGS`: its exit code
    and output. Raises ``TimeoutError`` once it took :data:`GIT_TIMEOUT`
    (it is stopped), ``OSError`` when git cannot be started."""
    argv = ["git", *args]
    if args[0] == "status":
        argv = harden_git(argv)
    env = {k: v for k, v in child_env().items() if k not in _REPOSITORY_VARIABLES}
    proc = await asyncio.create_subprocess_exec(
        argv[0], *GIT_READ_FLAGS, *argv[1:], cwd=cwd, env=env,
        stdin=DEVNULL, stdout=PIPE, stderr=DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    finally:
        if proc.returncode is None:  # out of time, or the caller gave up
            proc.kill()
            await proc.wait()
    return proc.returncode or 0, out.decode("utf-8", errors="replace")
