"""What Claude Code's harness tells the model about where it runs, in the
same places: a git snapshot and the commit-attribution note in the first
user turn, the environment (with the model line, the deferred tools, the
token budget and today's date) as a system turn right after it, and a
``<total_tokens>`` budget note after every prompt and tool result (a
sub-agent's after its tool results). As in Claude Code, only a model Claude
Code knows as a Claude model gets the budget (:func:`gets_budget`): for any
other model, and with ``token_budget=0``, no note is sent and the prompt's
budget line is taken out wherever it sits.

Everything is captured through the task's ExecEnv (``view.env``), never by a
host subprocess: on a container the facts describe the container's checkout and platform,
and the shell / OS version lines (facts of this machine) are left out. git
runs with the hardening the Bash tool's ``git status`` rule applies (``-c
core.fsmonitor=``, no hooks), no signature program and no look inside
submodules. A ``filter.<driver>.clean`` program has no off
switch, so when the repository's own config (local or worktree scope)
defines any filter driver the status is skipped. The capture is recorded
when the task first starts, and taken again for a compaction (which starts
the conversation over from its summary, as Claude Code does); in between it
never changes under the prompt cache. noeta's
own trigger for taking it again: Claude Code re-announces a changed session
context when a session resumes (a new process); a durable task has no resume
moment, so at the start of each later turn the branch and HEAD commit are
compared with what they were when the previous turn ended, and when someone
moved them in between (another branch, new commits) the new snapshot is
given in that turn. The model's own commits during a turn do not count.

As in Claude Code, a switched model is told before its next call, a change
of the repository, worktree or additional-directory facts at the start of
the next turn ("# Environment update"), and the ``language`` setting as its
own note; the main prompt gets Fable 5.1's and Haiku 5.5's own paragraphs
(:func:`with_model_paragraphs`).
"""

from __future__ import annotations

import asyncio
import os
import platform
import posixpath
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import msgspec

from noeta.core import models
from noeta.core.events import Recorded, Waiting
from noeta.core.execenv import ExecEnv
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.types import Message, ModelRequest, ToolResult, system_message, user
from noeta.toolkit.commands import is_command_message
from noeta.toolkit.context import task_env
from noeta.toolkit.deltas import AMBIENT_NOTE, changed
from noeta.toolkit.local_env import is_local
from noeta.toolkit.shell import harden_git

#: Git status longer than this many characters is cut there (Claude Code's
#: cut), and the note below follows.
GIT_STATUS_MAX_CHARS = 2000
GIT_STATUS_CUT_NOTE = ("\n... (cut here: the status runs past 2,000 characters; for the "
                       "full list run `git status` with Bash)")
#: A compaction waits this long for a new git snapshot, then gives the
#: stored one again (each git call has 5 seconds; a slow container could
#: otherwise add half a minute to the compaction).
GIT_RECAPTURE_TIMEOUT_S = 10.0
#: Commits listed in the git snapshot.
RECENT_COMMITS = 5
#: The snapshot's second line: taken at the start, or again for a message.
SNAPSHOT_AT_START = ("A snapshot taken when this conversation started; it does not change as "
                     "you work.")
SNAPSHOT_AGAIN = ("Taken again when the latest message arrived; it replaces the earlier "
                  "snapshot and does not change as you work.")
#: Global flags every capture git runs with: no fsmonitor hook, no signature
#: program, no index refresh written back.
_GIT_FLAGS = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
              "-c", "log.showSignature=false", "--no-optional-locks")
#: Config scopes a person set up themselves (a filter there is trusted).
_TRUSTED_SCOPES = ("system", "global")
#: The budget a turn starts with (Claude Code's ``<total_tokens>``).
TOKEN_BUDGET = 15_000_000
#: Where the PR footer points.
PROJECT_URL = "https://github.com/initxy/noeta"

#: Places of this plugin's notes in the joined system turn (``Reminder.order``):
#: the environment first, the budget and the date last.
ORDER_ENVIRONMENT = 0
#: After a compaction the environment comes after the lists (Claude Code's).
ORDER_ENVIRONMENT_AFTER_COMPACTION = 60
ORDER_DEFERRED = 30
#: A sub-agent's environment turn opens with its deferred tools.
ORDER_DEFERRED_SUBAGENT = -10
ORDER_LANGUAGE = 75
ORDER_BUDGET = 80
ORDER_DATE = 90


async def _git(env: ExecEnv, cwd: str, *args: str) -> str:
    return await _git_or_none(env, cwd, *args) or ""


async def _git_or_none(env: ExecEnv, cwd: str, *args: str) -> str | None:
    """git's output, ``None`` when it failed or timed out."""
    argv = ["git", *args]
    if args and args[0] == "status":
        argv = harden_git(argv)
    argv = [argv[0], *_GIT_FLAGS, *argv[1:]]
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
    argv = ["git", *_GIT_FLAGS, "config", "--show-scope", "--get-regexp", r"^filter\."]
    try:
        out = await env.run(argv, cwd=cwd, timeout=5, output_cap=1 << 20)
    except Exception:  # noqa: BLE001 — capture is best-effort
        return True
    if out.timed_out:
        return True
    if out.exit_code == 1 and not out.stdout.strip():
        return False  # no filter configured anywhere
    if out.exit_code != 0:
        return True
    for line in out.stdout.decode("utf-8", errors="replace").splitlines():
        if line.strip() and line.split("\t", 1)[0].strip() not in _TRUSTED_SCOPES:
            return True
    return False


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


def _shell() -> str:
    """The shell noeta's commands run in (``Bash`` runs ``bash -c`` from
    ``PATH``), by name, as Claude Code names the one its commands run in;
    the user's login shell follows when it is another (``bash (login shell:
    zsh)``)."""
    bash = shutil.which("bash") or "bash"
    login = os.environ.get("SHELL")
    if not login or os.path.realpath(login) == os.path.realpath(bash) \
            or posixpath.basename(login) == "bash":
        return "bash"
    return f"bash (login shell: {posixpath.basename(login)})"


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


async def capture_facts(env: ExecEnv, cwd: str, dirs: Sequence[str] = ()) -> dict[str, Any]:
    """The facts the environment message gives that can change while a task
    runs (Claude Code's environment update): whether ``cwd`` is in a
    repository and in a linked worktree, and the additional directories."""
    is_git = await git_root(env, cwd) is not None
    return {"git": is_git, "worktree": is_git and await is_worktree(env, cwd),
            "dirs": list(dirs)}


async def capture_environment(env: ExecEnv, cwd: str, model: str,
                              facts: Mapping[str, Any] | None = None) -> str:
    """The environment message's facts for ``cwd`` as ``env`` sees it."""
    if facts is None:
        facts = await capture_facts(env, cwd)
    local = is_local(env)
    return render_environment(cwd, model, is_git=bool(facts["git"]), local=local,
                              platform_name=sys.platform if local else await _env_platform(env),
                              is_worktree=bool(facts.get("worktree")),
                              additional_dirs=facts.get("dirs") or ())


async def _env_platform(env: ExecEnv) -> str:
    """The exec env's platform in ``sys.platform``'s words (``linux``,
    ``darwin``), from its ``uname -s``; ``""`` when it cannot be told."""
    try:
        out = await env.run(["uname", "-s"], timeout=5, output_cap=1024)
    except Exception:  # noqa: BLE001 — capture is best-effort
        return ""
    if out.exit_code != 0 or out.timed_out:
        return ""
    name = out.stdout.decode("utf-8", errors="replace").strip().lower()
    return name if name in ("linux", "darwin") or name.startswith("freebsd") else ""


def valid_zone(name: Any) -> str | None:
    """``name`` when it is an IANA time zone this machine knows, else ``None``."""
    if not isinstance(name, str) or not name or name.startswith(("/", ".")):
        return None
    try:
        ZoneInfo(name)
    except (ValueError, KeyError, OSError):
        return None
    return name


def today_in(zone: str | None) -> str:
    """Today's date in ``zone``, or in UTC without one."""
    return datetime.now(ZoneInfo(zone) if zone else UTC).date().isoformat()


#: The environment's lines for a linked worktree (Claude Code's two).
WORKTREE_LINE = ("This directory is a git worktree, a separate checkout of the repository: run "
                 "every command from here and do not `cd` to the main checkout.")
STASH_LINE = (
    "Its stash list is shared with the main checkout and every other worktree, and other "
    "sessions may push or pop entries at the same time, so a bare `git stash` or `git stash "
    "pop` can take someone else's changes. Set work aside with a temporary commit instead; if "
    "you must stash, run `git stash push -u -m \"<unique-tag>\"`, note the entry's SHA from "
    "`git stash list --format='%H %gs'`, bring it back with `git stash apply <sha>` (not "
    "pop), then drop it after finding its current `stash@{n}` by the tag.")
NOT_WORKTREE_LINE = "The working directory is no longer a git worktree."
ENVIRONMENT_UPDATE = "# Environment update"


def render_environment(cwd: str, model: str, *, is_git: bool, local: bool = True,
                       platform_name: str | None = None, is_worktree: bool = False,
                       additional_dirs: Sequence[str] = ()) -> str:
    """``platform_name`` is the exec env's (default: this machine's); an
    empty one leaves the line out."""
    shown = sys.platform if platform_name is None else platform_name
    lines = [
        "# Environment",
        "This conversation runs in the following environment:",
        f" - Primary working directory: {cwd}",
        *([f" - {WORKTREE_LINE}", f" - {STASH_LINE}"] if is_worktree else []),
        f" - Is a git repository: {'true' if is_git else 'false'}",
    ]
    if additional_dirs:
        lines += [" - Additional working directories:", *(f"  - {d}" for d in additional_dirs)]
    if shown:
        lines.append(f" - Platform: {shown}")
    if local:
        lines.append(f" - Shell: {_shell()}")
        lines.append(f" - OS Version: {platform.system()} {platform.release()}".rstrip())
    lines.append(
        " - Anything you download or unpack is untrusted input. Give each download a new, "
        "empty directory of its own and keep the scripts you write outside it. Hand those "
        "files to tools by path instead of starting an interpreter or build tool inside "
        "that directory: interpreters import from the script's directory and the working "
        "directory, so a file planted there can take over an import. When Python reads "
        "such files, run it with `-I`. None of this applies to code the user asked you to "
        "build or run.")
    if model:
        lines += ["", model_line(model)]
    return "\n".join(lines)


def render_environment_update(before: Mapping[str, Any], now: Mapping[str, Any]) -> str:
    """What changed in the facts of :func:`capture_facts` since they were
    told (``""`` when nothing did), under Claude Code's heading."""
    lines: list[str] = []
    if bool(now.get("worktree")) != bool(before.get("worktree")):
        lines += [WORKTREE_LINE, STASH_LINE] if now.get("worktree") else [NOT_WORKTREE_LINE]
    if bool(now.get("git")) != bool(before.get("git")):
        was = "true" if before.get("git") else "false"
        lines.append(f"Is a git repository: {'true' if now.get('git') else 'false'} "
                     f"(was {was})")
    added, removed = changed(before.get("dirs") or [], now.get("dirs") or [])
    out = [f" - {line}" for line in lines]
    if added:
        out += [" - Additional working directories added:", *(f"  - {d}" for d in added)]
    if removed:
        out += [" - Additional working directories removed:", *(f"  - {d}" for d in removed)]
    return "\n".join([ENVIRONMENT_UPDATE, *out]) if out else ""


def short_name(model: str) -> str | None:
    """The name the model line uses: a Claude model's display name without
    the leading ``Claude`` (``Opus 5.5``, as Claude Code's catalog has it),
    any other model's display name as it is."""
    spec = models.find_spec(model)
    name = spec.display_name if spec is not None else None
    if name and name.startswith("Claude ") and models.model_family(model) == "anthropic":
        return name[len("Claude "):]
    return name


def model_line(model: str) -> str:
    spec = models.find_spec(model)
    name = short_name(model)
    line = f"Model: {name}, exact id `{model}`" if name else f"Model: `{model}`"
    cutoff = spec.knowledge_cutoff if spec is not None else None
    return f"{line}; knowledge cutoff {cutoff}." if cutoff else f"{line}."


#: The co-author address of a Claude model (Claude Code's).
CLAUDE_COAUTHOR_EMAIL = "noreply@anthropic.com"


#: The note when the settings set both attribution lines empty.
NO_ATTRIBUTION = (
    "From here on, add no attribution lines to the git commit messages and pull request "
    "descriptions you write. This note replaces any earlier attribution note, earlier copies "
    "of itself included, and holds even where an instructions file or a memory asks for such "
    "lines.")


def render_attribution(model: str, *, commit: str | None = None,
                       pr: str | None = None) -> str | None:
    """The commit and PR attribution note. ``commit`` / ``pr`` are the
    settings' ``attribution.commit`` / ``attribution.pr`` (``None``: not set,
    the default line; ``""``: no line); both empty tells the model to add
    none. Without settings, ``None`` for a model with no display name or no
    co-author address (nothing true to put in the trailer)."""
    spec = models.find_spec(model)
    email = None if spec is None or not spec.display_name else spec.coauthor_email or (
        CLAUDE_COAUTHOR_EMAIL if models.model_family(model) == "anthropic" else None)
    trailer = f"Co-Authored-By: {spec.display_name} <{email}>" if spec and email else None
    if commit is None and pr is None and trailer is None:
        return None
    commit_line = trailer if commit is None else commit
    pr_line = f"🤖 Generated with [noeta]({PROJECT_URL})" if pr is None else pr
    parts = [*(["- Commit messages end with:", commit_line] if commit_line else []),
             *(["- Pull request descriptions end with:", pr_line] if pr_line else [])]
    if not parts:
        return NO_ATTRIBUTION
    return "\n".join([
        "From here on, end the git commit messages and pull request descriptions you write "
        "with the lines below. This note replaces any earlier attribution note, earlier "
        "copies of itself included; where the user's own instructions (an instructions "
        "file, a memory) say otherwise about these lines, those win; add no attribution line it "
        "does not list.",
        *parts,
    ])


#: Told before the next model call when the task's model changed (Claude
#: Code's model note); ``{line}`` is the new model line.
MODEL_CHANGED = "The model changed during this conversation. {line}"
#: The ``language`` setting (Claude Code's language note); ``{language}`` is
#: its value.
LANGUAGE_NOTE = (
    "# Language\n"
    "Answer in {language}: explanations, code comments and every message to the user. Keep "
    "technical terms and code identifiers as they are. Write {language} with its full "
    "spelling, every accent and diacritic included; never put a plain ASCII letter in place "
    "of an accented one.")
LANGUAGE_CLEARED = "The language setting was removed: answer in the language the user writes in."


DEFERRED_TOOLS_HEAD = (
    "The tools below are deferred: only their names are known here and their schemas are "
    "not loaded, so calling one directly fails with an input validation error. Load the ones "
    "you need with ToolSearch, query \"select:<name>[,<name>...]\", before calling them:")


def deferred_list(names: Sequence[str]) -> str:
    """The deferred tools, one name per line under the lead-in."""
    return "\n".join([DEFERRED_TOOLS_HEAD, *names])


DEFERRED_BACK = (
    "{count} deferred tool{s} {verb} available again ({why}; the names were given earlier in "
    "this conversation): {names}. Load them with ToolSearch as before.")
DEFERRED_GONE_MCP = (
    "These deferred tools are no longer available (their MCP server disconnected). Do not "
    "search for them; ToolSearch will not find them:")
DEFERRED_GONE = (
    "These deferred tools are no longer available in this conversation. Do not search for "
    "them; ToolSearch will not find them:")


def deferred_delta(before: Sequence[str], ever: Sequence[str], now: Sequence[str]) -> str:
    """The note on a changed deferred-tool set (``""`` when it did not
    change): new names under the list's lead-in, names given earlier as
    available again, and the names gone."""
    joined, left = changed(before, now)
    new = [n for n in joined if n not in ever]
    back = [n for n in joined if n in ever]
    parts: list[str] = []
    if new:
        parts.append(deferred_list(new))
    for group, why in (([n for n in back if n.startswith("mcp__")], "their MCP server "
                        "reconnected"), ([n for n in back if not n.startswith("mcp__")],
                                         "offered again")):
        if group:
            parts.append(DEFERRED_BACK.format(
                count=len(group), s="" if len(group) == 1 else "s",
                verb="is" if len(group) == 1 else "are", why=why, names=", ".join(group)))
    mcp = [n for n in left if n.startswith("mcp__")]
    other = [n for n in left if not n.startswith("mcp__")]
    if mcp:
        parts.append("\n".join([DEFERRED_GONE_MCP, *mcp]))
    if other:
        parts.append("\n".join([DEFERRED_GONE, *other]))
    if left:
        parts.append(AMBIENT_NOTE)
    return "\n\n".join(parts)


def budget_note(left: int) -> str:
    return f"<total_tokens>{left} tokens left</total_tokens>"


#: A budget line of a system prompt (``main.md``'s last line), with the
#: blank lines before it; an appended prompt may follow it.
_PROMPT_BUDGET_LINE = re.compile(r"\n*^<total_tokens>\d+ tokens left</total_tokens>[ \t]*$",
                                 re.MULTILINE)

#: Claude Code 2.1.312's Claude models: every row of its catalog has the
#: ``total_tokens_reminder`` capability. It recognizes a model id that
#: contains one of these (case-insensitive: ``claude-opus-5-5[1m]``,
#: ``us.anthropic.claude-opus-5-5-v1:0``, a dated id), or the dated
#: first-party id of a 4.0 model.
_CLAUDE_CODE_MODELS = (
    "claude-3-5-haiku", "claude-3-5-sonnet", "claude-3-7-sonnet",
    "claude-haiku-4-5", "claude-haiku-5-5",
    "claude-sonnet-4-0", "claude-sonnet-4-5", "claude-sonnet-4-6", "claude-sonnet-5",
    "claude-sonnet-5-5",
    "claude-opus-4-0", "claude-opus-4-1", "claude-opus-4-5", "claude-opus-4-6",
    "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-opus-5-5",
    "claude-fable-5", "claude-fable-5-1", "claude-mythos-5", "claude-mythos-5-1",
    "claude-opus-4-2025", "claude-sonnet-4-2025",
)


def gets_budget(model: str) -> bool:
    """Whether ``model`` is told the ``<total_tokens>`` budget: Claude Code
    tells exactly the Claude models it knows (:data:`_CLAUDE_CODE_MODELS`)
    and nothing else (a gateway id like ``auto_model/x`` gets none). An
    alias resolves first."""
    real = models.resolve_alias(model).lower()
    return any(known in real for known in _CLAUDE_CODE_MODELS)


#: Claude Code's paragraph for Fable 5.1 (its ``fable_identity`` section,
#: after the cautions, before the session guidance).
FABLE_IDENTITY = (
    "You are Claude Fable 5.1, the newest model of Anthropic's Claude 5 family and part of "
    "the Mythos-class tier, which sits above Claude Opus in capability. Claude Fable 5.1 and "
    "Claude Mythos 5.1 are one underlying model: Fable 5.1, the most capable generally "
    "available Claude model, adds safeguards for dual-use capabilities, while Mythos 5.1 comes "
    "without them and only to approved organizations. When the person asks how the two "
    "differ, point them to https://www.anthropic.com/claude/fable.")
#: Claude Code's early-stopping guidance for Haiku 5.5 (the model's
#: ``haiku_5_5_early_stopping_guidance`` capability), at the prompt's end.
EARLY_STOPPING = (
    "The effort setting decides how much you think before you act, not how much of the "
    "request you finish. A turn lasts as long as you keep working, so a large task can be "
    "finished in the turn it was asked in; its size is no reason to check in first.\n"
    "Ending your turn halts all work until the user answers, and they may be away a while. "
    "Stopping before you change anything leaves them the code as it was plus a message to "
    "answer. End the turn when the request is done or nothing is left that you can do "
    "without them.\n"
    "Ask before you start only when you cannot name the most likely reading of the request; "
    "when you can, act on it and say in your final message which reading you took. The other "
    "reason to ask first is a fact, file or access that only the user has and the whole task "
    "depends on; their approval of a choice you could make yourself is not one. Actions that "
    "are hard to undo or reach outside still need their confirmation. When they say they "
    "want to approve something before you go on, such as a plan, stop there; words that only "
    "order the steps (\"plan, then build\") are not a stop, so do each step and go on. When "
    "they ask a question or are still choosing, they want your answer, not a change; when "
    "they also asked for work, answer and then do it.\n"
    "Edits to files in the working tree are neither hard to undo nor outward-facing, since "
    "the user can look at and revert them, unless they would overwrite work the user has in "
    "progress. That leaves the open choices to you: how to build the change, how to split "
    "it, how to handle a case the request left open. Pick what you would recommend, follow "
    "the user's words where they were specific, and list your choices in the final message "
    "so they can steer you. Start editing once you know the first change: a design worked "
    "out in files lasts, while a long stretch of thinking can be cut off and lost.\n"
    "When one part of the task is blocked, unclear or looks wrong, the rest usually is not. "
    "Try a step you expect to fail before you report it. Finish everything that does not "
    "depend on the stuck part and open your final message with what is stuck: finished parts "
    "help the user even when the whole task is not done. Setting the project up so you can "
    "build and test it, such as installing its declared dependencies, is part of the work; "
    "if it still cannot be built or run here, say so and make the changes you can check by "
    "reading. When you cannot find the cause of a problem, report what you ruled out and "
    "what would settle it. A question at the end of finished work costs the user one reply, "
    "the same as one asked before any work.")
#: Where the paragraphs go in the main prompt: before the first of these
#: headings (Fable's), and before the budget line or at the end (Haiku's).
_IDENTITY_ANCHORS = ("\n# Session-specific guidance\n", "\n# Environment\n")


def with_model_paragraphs(text: str, model: str) -> str:
    """The main prompt with Claude Code's paragraphs for ``model``: Fable
    5.1's identity, Haiku 5.5's early-stopping guidance. Only a prompt with
    the main prompt's ``# Environment`` section gets them (not a sub-agent's,
    not one the person replaced)."""
    if "\n# Environment\n" not in text:
        return text
    real = models.resolve_alias(model).lower()
    if "claude-fable-5-1" in real:
        at = min(i for a in _IDENTITY_ANCHORS if (i := text.find(a)) >= 0)
        text = f"{text[:at + 1]}{FABLE_IDENTITY}\n\n{text[at + 1:]}"
    if "claude-haiku-5-5" in real:
        budget = _PROMPT_BUDGET_LINE.search(text)
        if budget is None:
            text = f"{text.rstrip(chr(10))}\n\n{EARLY_STOPPING}"
        else:
            text = f"{text[:budget.start()]}\n\n{EARLY_STOPPING}{text[budget.start():]}"
    return text


def date_line(today: str, *, utc: bool = False) -> str:
    """Today's date for the environment turn; ``utc`` when the user's time
    zone is not known, so the date is UTC's and said to be."""
    if utc:
        return (f"The date today is {today} in UTC; the user's own date may differ. A date or "
                "time in a system reminder that is not marked UTC is the user's local one: "
                "take it as today's date.")
    return f"The date today is {today}."


DATE_CHANGED_REMINDER = (
    "The date moved on during this conversation: today is {date}. The user knows, so do not "
    "point it out."
)
#: The same, when the dates are UTC's (the user's time zone is not known).
DATE_CHANGED_UTC_REMINDER = (
    "The date in UTC moved on during this conversation: it is now {date}; the user's own "
    "date may differ. The earlier date line was in UTC too; a date or time in a system "
    "reminder that is not marked UTC is the user's local one: take it as today's date."
)


def _owes_budget_note(messages: Sequence[Message], *, prompts: bool = True) -> bool:
    """Whether a prompt (with ``prompts``) or a tool result came in since the
    last budget note; a typed slash command is not a prompt here (Claude
    Code sends none after one)."""
    for m in reversed(messages):
        if m.role == "assistant":
            return False
        if m.role == "system" and "<total_tokens>" in m.text:
            return False
        if m.role == "user" and any(isinstance(b, ToolResult) for b in m.content):
            return True
        if m.from_person:
            return prompts and not is_command_message(m.text)
    return False


class Environment(Extension):
    """Constructor arguments:

    * ``model`` — the agent's model, named when the task's config sets none.
    * ``model_name`` — include the model line.
    * ``git_status`` — put the git snapshot in the first user turn.
    * ``git_refresh`` — give the snapshot again at the start of a later turn
      when the branch or HEAD moved since the previous turn ended (noeta's
      stand-in for Claude Code's re-announce on resume).
    * ``attribution`` — put the commit / PR attribution note there.
    * ``attribution_commit`` / ``attribution_pr`` — the settings'
      ``attribution.commit`` / ``attribution.pr``: the line to use instead
      of the default (``""``: none; both ``""``: the model is told to add
      none).
    * ``language`` — the settings' ``language``: the model is told to answer
      in it (and told again when it changes or goes).
    * ``token_budget`` — the ``<total_tokens>`` budget a turn starts with
      (``0``: no budget notes, and the prompt's budget line taken out).
    * ``date_change`` — tell the model, before any model call, when the date
      is no longer the one it was given (the prompt
      ``reminder/date_changed``, or ``reminder/date_changed_utc`` for UTC
      dates; ``{date}`` is today).
    * ``today`` — today's date as ``YYYY-MM-DD``, taken as the user's
      (default: the date in the task config's ``timezone``, the user's IANA
      zone a front end passes; without one, the UTC date, said to be UTC's,
      so a server never gives its own zone's date as the user's).
    """

    name = "environment"

    def __init__(self, *, model: str | None = None, model_name: bool = True,
                 git_status: bool = True, git_refresh: bool = True,
                 attribution: bool = True, attribution_commit: str | None = None,
                 attribution_pr: str | None = None, language: str | None = None,
                 token_budget: int = TOKEN_BUDGET, date_change: bool = True,
                 today: Callable[[], str] | None = None) -> None:
        self.model = model
        self.attribution_commit = attribution_commit
        self.attribution_pr = attribution_pr
        self.language = language or None
        self.model_name = model_name
        self.git_status = git_status
        self.git_refresh = git_refresh
        self.attribution = attribution
        self.token_budget = token_budget
        self.date_change = date_change
        self.today = today

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        if not isinstance(data, dict):
            return state
        if kind == "environment" and "text" in data:
            # A record without ``held`` (an older log) went out as a message.
            return {"text": data["text"], "date": data.get("date"),
                    "utc": bool(data.get("utc")), "held": bool(data.get("held")),
                    "turn_base": 0, "model": data.get("model"), "facts": data.get("facts")}
        if not isinstance(state, dict):
            return state
        if kind == "model":
            return {**state, "model": data.get("model"), "text": data.get("text", state["text"])}
        if kind == "env_update":
            return {**state, "facts": data.get("facts"), "text": data.get("text", state["text"])}
        if kind == "language":
            return {**state, "language": data.get("language")}
        if kind == "git":
            text = data.get("text")
            return {**state, "git": text, "git_head": data.get("head"),
                    "git_turn_end": data.get("head")}
        if kind == "git_turn_end":
            return {**state, "git_turn_end": data.get("head")}
        if kind == "attribution":
            return {**state, kind: data.get("text")}
        if kind == "sent":
            return {**state, "held": False, "skip_date": False}
        if kind == "rehold":
            # A compaction: the environment, the deferred tools, the
            # language note and (unless it already follows the summary) the
            # date go out again.
            return {**state, "held": True, "compacted": True, "language": None,
                    "skip_date": not data.get("date", True)}
        if kind in ("deferred", "deferred_delta"):
            names = list(data.get("names") or [])
            ever = [*(state.get("deferred_ever") or state.get("deferred") or []), *names]
            return {**state, "deferred": names, "deferred_ever": list(dict.fromkeys(ever))}
        if kind == "date":
            return {**state, "date": data.get("date")}
        if kind == "budget":
            return {**state, "turn_base": data.get("base", 0)}
        return state

    def _model(self, view: TaskView) -> str:
        return str(view.state.config.get("model") or self.model or view.model or "")

    def _today(self, view: TaskView) -> tuple[str, bool]:
        """Today's date for this task, and whether it is UTC's (the user's
        zone is not known)."""
        if self.today is not None:
            return self.today(), False
        zone = valid_zone(view.state.config.get("timezone"))
        return today_in(zone), zone is None

    def inherit(self, state: TaskState) -> Mapping[str, Any]:
        zone = valid_zone(state.config.get("timezone"))
        return {"timezone": zone} if zone else {}

    @staticmethod
    def _dirs(view: TaskView) -> list[str]:
        """The additional working directories: those of the plugin that
        keeps them (permissions, ``working_directories``) after the first."""
        for ext in view.extensions:
            dirs = getattr(ext, "working_directories", None)
            if callable(dirs):
                return list(dirs(view))[1:]
        return []

    async def on_start(self, view: TaskView) -> None:
        if view.ext_state(self.name):
            return
        env = task_env(view.env, view.state.config)
        cwd = view.cwd or env.cwd
        model = self._model(view)
        facts = await capture_facts(env, cwd, self._dirs(view))
        text = await capture_environment(env, cwd, model if self.model_name else "", facts)
        today, utc = self._today(view)
        # Held for the first request's system turn, after the prompt.
        data: dict[str, Any] = {"text": text, "date": today, "held": True, "facts": facts}
        if self.model_name and model:
            data["model"] = model
        if utc:
            data["utc"] = True
        view.record(self.name, "environment", data)
        if self.git_status:
            snapshot = await capture_git(env, cwd)
            if snapshot:
                view.record(self.name, "git", {"text": snapshot,
                                               "head": await git_head(env, cwd)},
                            message=user(snapshot, origin="system"))
        note = render_attribution(model, commit=self.attribution_commit,
                                  pr=self.attribution_pr) if self.attribution and model else None
        if note:
            view.record(self.name, "attribution", {"text": note},
                        message=user(note, origin="system"))

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        st = view.ext_state(self.name)
        if not isinstance(st, dict):
            return ()
        out: list[Reminder] = []
        model = self._model(view)
        told = str(st.get("model") or "")
        switched = bool(self.model_name and told and model and model != told)
        if st.get("held"):
            # The first request (or the first after a compaction): the
            # environment follows the prompt, naming the model in use now.
            text = st["text"]
            if switched:
                text = text.replace(model_line(told), model_line(model))
                view.record(self.name, "model", {"model": model, "text": text})
            out.append(Reminder(text=text, kind="sent", data={}, role="system",
                                order=ORDER_ENVIRONMENT_AFTER_COMPACTION
                                if st.get("compacted") else ORDER_ENVIRONMENT))
            # The deferred tools the first request offers, named in full;
            # later changes are told as they happen (below).
            names = view.deferred_tool_names()
            if names:
                order = ORDER_DEFERRED_SUBAGENT if view.state.parent_id else ORDER_DEFERRED
                out.append(Reminder(text=deferred_list(names), kind="deferred",
                                    data={"names": names}, role="system", order=order))
            elif st.get("deferred"):
                view.record(self.name, "deferred", {"names": []})
            if not st.get("skip_date"):
                out.append(Reminder(text=date_line(st["date"], utc=bool(st.get("utc"))),
                                    kind="date", data={"date": st["date"]}, role="system",
                                    order=ORDER_DATE))
        else:
            if switched:
                # Claude Code tells a switched model before its next call.
                line = MODEL_CHANGED.replace("{line}", model_line(model))
                out.append(Reminder(text=line, kind="model", data={
                    "model": model, "text": st["text"].replace(model_line(told),
                                                               model_line(model))},
                    role="system", order=ORDER_ENVIRONMENT))
            out += await self._environment_update(view, st)
            out += self._deferred_delta(view, st)
            out += await self._git_again(view, st)
        if not view.state.parent_id and self.language != st.get("language") \
                and (self.language or st.get("language")):
            text = LANGUAGE_NOTE.replace("{language}", self.language) if self.language \
                else LANGUAGE_CLEARED
            out.append(Reminder(text=text, kind="language", data={"language": self.language},
                                role="system", order=ORDER_LANGUAGE))
        # Checked before every model call, as Claude Code does, so a long
        # turn that runs past midnight is told too. Each call may run on
        # another worker, whose clock can be a little behind: a date before
        # the one last given is never told.
        if not st.get("held") and self.date_change and st.get("date"):
            today, _ = self._today(view)
            if today > str(st["date"]):
                text = view.prompt("reminder/date_changed_utc", DATE_CHANGED_UTC_REMINDER) \
                    if st.get("utc") else view.prompt("reminder/date_changed",
                                                      DATE_CHANGED_REMINDER)
                out.append(Reminder(text=text.replace("{date}", today), kind="date",
                                    data={"date": today}, role="system", order=ORDER_DATE))
        # A sub-agent's task prompt gets none; its tool results do.
        if self.token_budget and gets_budget(self._model(view)) and _owes_budget_note(
                view.state.messages, prompts=not view.state.parent_id):
            spent = view.state.usage.output
            base = spent if not view.state.steps_in_turn else st.get("turn_base", 0)
            left = max(self.token_budget - (spent - base), 0)
            out.append(Reminder(text=budget_note(left), kind="budget", data={"base": base},
                                role="system", order=ORDER_BUDGET))
        return out

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        """The main prompt's paragraphs for this model
        (:func:`with_model_paragraphs`); a model that is told no budget is
        not shown the prompt's budget line either."""
        budget = bool(self.token_budget and gets_budget(request.model))
        system = [msgspec.structs.replace(p, text=t) for p in request.system
                  if (t := self._prompt_text(p.text, request.model, budget)) is not None]
        if all(a.text == b.text for a, b in zip(system, request.system, strict=True)):
            return request
        return msgspec.structs.replace(request, system=system)

    @staticmethod
    def _prompt_text(text: str, model: str, budget: bool) -> str:
        text = with_model_paragraphs(text, model)
        return text if budget else _PROMPT_BUDGET_LINE.sub("", text)

    async def _environment_update(self, view: TaskView,
                                  st: dict[str, Any]) -> list[Reminder]:
        """At the start of a later turn, what changed in the facts the
        environment gave (repository, worktree, additional directories):
        Claude Code's environment update. The environment text is taken
        again with them, for a compaction to send."""
        before = st.get("facts")
        if not isinstance(before, dict) or view.state.steps_in_turn or view.state.parent_id:
            return []
        env = task_env(view.env, view.state.config)
        cwd = view.cwd or env.cwd
        try:
            now = await capture_facts(env, cwd, self._dirs(view))
        except Exception:  # noqa: BLE001 — capture is best-effort
            return []
        text = render_environment_update(before, now)
        if not text:
            return []
        full = await capture_environment(env, cwd, self._model(view) if st.get("model") else "",
                                         now)
        return [Reminder(text=text, kind="env_update", data={"facts": now, "text": full},
                         role="system", order=ORDER_ENVIRONMENT)]

    async def _git_again(self, view: TaskView, st: dict[str, Any]) -> list[Reminder]:
        """At the start of a later turn, the snapshot taken again when the
        branch or its commit moved since the previous turn ended (someone
        switched branch or committed in between; edits do not count, nor do
        the model's own commits, which happened inside a turn)."""
        if not self._refreshes(view, st):
            return []
        env = task_env(view.env, view.state.config)
        cwd = view.cwd or env.cwd
        head = await git_head(env, cwd)
        if not head or head == (st.get("git_turn_end") or st["git_head"]):
            return []
        again = await capture_git(env, cwd, when=SNAPSHOT_AGAIN)
        return [Reminder(text=again, kind="git", data={"text": again, "head": head})]

    def _refreshes(self, view: TaskView, st: dict[str, Any]) -> bool:
        return bool(self.git_status and self.git_refresh and not view.state.steps_in_turn
                    and st.get("git_head") and not view.state.parent_id)

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        """When a turn ends, where the branch and HEAD are (a git call)."""
        st = view.ext_state(self.name)
        if not (isinstance(recorded.event, Waiting) and recorded.event.on == "input"
                and isinstance(st, dict) and self.git_status and self.git_refresh
                and st.get("git_head") and not view.state.parent_id):
            return
        env = task_env(view.env, view.state.config)
        head = await git_head(env, view.cwd or env.cwd)
        if head and head != st.get("git_turn_end"):
            view.record(self.name, "git_turn_end", {"head": head})

    @staticmethod
    def _deferred_delta(view: TaskView, st: dict[str, Any]) -> list[Reminder]:
        """A note when the deferred tools offered changed since they were
        last named (an MCP server connected late, restarted with other
        tools, or dropped): Claude Code's deferred-tools delta."""
        names = view.deferred_tool_names()
        before = st.get("deferred") or []
        text = deferred_delta(before, st.get("deferred_ever") or before, names)
        if not text:
            return []
        return [Reminder(text=text, kind="deferred_delta", data={"names": names},
                         role="system", order=ORDER_DEFERRED)]

    async def lead_resident(self, view: TaskView) -> Sequence[Message]:
        """Ahead of a compaction's summary, as Claude Code rebuilds it: a
        git snapshot taken now (the summary has replaced the earlier one, so
        it shows what the conversation changed; none once the directory is
        no longer a repository), then the attribution note, or, when
        messages stay after the summary, the date (a system turn, which
        follows the summary) and the note after those messages
        (``resident``). The environment and the deferred tools go out again
        with the next request."""
        st = view.ext_state(self.name)
        if view.keeps_tail is None or not isinstance(st, dict):
            return ()
        out: list[Message] = []
        if isinstance(st.get("git"), str):
            snapshot = await self._git_for_compaction(view, st["git"])
            if snapshot:
                out.append(user(snapshot, origin="system"))
        if not view.keeps_tail and isinstance(st.get("attribution"), str):
            out.append(user(st["attribution"], origin="system"))
        date = view.keeps_tail and bool(st.get("date"))
        if date:
            out.append(system_message(date_line(st["date"], utc=bool(st.get("utc")))))
        if isinstance(st.get("text"), str):
            view.record(self.name, "rehold", {"date": not date})
        return out

    async def _git_for_compaction(self, view: TaskView, stored: str) -> str:
        """The snapshot a compaction gives again: taken now (``""`` once the
        directory is no longer a repository), or the stored one when taking
        it fails or runs past :data:`GIT_RECAPTURE_TIMEOUT_S` seconds."""
        env = task_env(view.env, view.state.config)
        cwd = view.cwd or env.cwd

        async def take() -> tuple[str, str]:
            if await _find_root(env, cwd) is None:
                return "", ""
            snapshot = await capture_git(env, cwd)
            return snapshot, (await git_head(env, cwd) if snapshot else "")

        try:
            snapshot, head = await asyncio.wait_for(take(), GIT_RECAPTURE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — the stored snapshot stands in
            return stored
        if snapshot:
            view.record(self.name, "git", {"text": snapshot, "head": head})
        return snapshot

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """After the messages a compaction keeps: the attribution note."""
        st = view.ext_state(self.name)
        if not view.keeps_tail or not isinstance(st, dict) \
                or not isinstance(st.get("attribution"), str):
            return ()
        return [user(st["attribution"], origin="system")]
