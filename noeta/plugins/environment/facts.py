"""The environment block's facts (directory, repository, worktree, platform,
shell, model) and the commit-attribution note."""

from __future__ import annotations

import os
import platform
import posixpath
import shutil
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core import models
from noeta.core.execenv import ExecEnv
from noeta.plugins.environment.git import git_root, is_worktree
from noeta.toolkit.deltas import changed
from noeta.toolkit.local_env import env_platform, is_local

#: Where the PR footer points.
PROJECT_URL = "https://github.com/initxy/noeta"


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
                              platform_name=await env_platform(env),
                              is_worktree=bool(facts.get("worktree")),
                              additional_dirs=facts.get("dirs") or ())


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
