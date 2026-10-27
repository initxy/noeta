"""The workspace environment block (Claude Code's environment section, line
for line), recorded once at the start of a task as a system-origin message so
it never changes under the prompt cache. Later runs keep the first capture;
the block says so, and the model runs git itself for live state.

Everything is captured through the task's ExecEnv (``view.env``), never by a
host subprocess: on a container the facts describe the container's checkout,
and the shell / OS version lines (facts of this machine) are left out, as 0.6
did for sandboxes. git runs with the same hardening the Bash tool's
``git status`` rule applies (``-c core.fsmonitor=``, no hooks), so a
repository's own config cannot make the capture run a program. A
``filter.<driver>.clean`` program has no off switch, so when the
repository's own config (local or worktree scope) defines any filter driver
the status is skipped (the branch is still named).
"""

from __future__ import annotations

import os
import platform
import posixpath
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from noeta.core.execenv import ExecEnv, is_local
from noeta.core.extension import Extension, TaskView
from noeta.core.helpers import task_env
from noeta.core.shell import _harden_git
from noeta.core.types import Message, user

#: Git status beyond this is cut at a line boundary and marked.
GIT_STATUS_MAX_BYTES = 2048
#: Global flags every capture git runs with: no fsmonitor hook, no index
#: refresh written back.
_GIT_FLAGS = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "--no-optional-locks")
#: Config scopes a person set up themselves (a filter there is trusted).
_TRUSTED_SCOPES = ("system", "global")


async def _git(env: ExecEnv, cwd: str, *args: str) -> str:
    argv = ["git", *args]
    if args and args[0] == "status":
        argv = _harden_git(argv)
    argv = [argv[0], *_GIT_FLAGS, *argv[1:]]
    try:
        out = await env.run(argv, cwd=cwd, timeout=5, output_cap=1 << 20)
    except Exception:  # noqa: BLE001 — capture is best-effort
        return ""
    if out.exit_code != 0 or out.timed_out:
        return ""
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


async def _git_status(env: ExecEnv, cwd: str) -> str:
    if await _repo_defines_filters(env, cwd):
        return ""
    status = (await _git(env, cwd, "status", "--short")).rstrip("\n")
    encoded = status.encode("utf-8")
    if len(encoded) <= GIT_STATUS_MAX_BYTES:
        return status
    kept = encoded[:GIT_STATUS_MAX_BYTES].decode("utf-8", errors="ignore")
    newline = kept.rfind("\n")
    kept = kept[:newline] if newline >= 0 else ""
    marker = (
        f"({len(kept.splitlines())} of {len(status.splitlines())} changed "
        "paths shown; the status is truncated — run `git status` for the rest.)"
    )
    return f"{kept}\n{marker}" if kept else marker


def _shell() -> str:
    shell = os.environ.get("SHELL") or "unknown"
    return "zsh" if "zsh" in shell else "bash" if "bash" in shell else shell


async def capture_environment(env: ExecEnv, cwd: str, model: str) -> str:
    """The block for ``cwd`` as ``env`` sees it."""
    try:
        is_git = await env.stat(posixpath.join(cwd, ".git")) is not None
    except Exception:  # noqa: BLE001 — capture is best-effort
        is_git = False
    branch = (await _git(env, cwd, "rev-parse", "--abbrev-ref", "HEAD")).strip() \
        if is_git else ""
    status = await _git_status(env, cwd) if is_git else ""
    return render_environment(cwd, model, is_git=is_git, branch=branch, status=status,
                              local=is_local(env))


def render_environment(cwd: str, model: str, *, is_git: bool, branch: str = "",
                       status: str = "", local: bool = True) -> str:
    lines = [
        "<workspace-environment>",
        "# Environment",
        "You have been invoked in the following environment: ",
        f" - Primary working directory: {cwd}",
        f" - Is a git repository: {'true' if is_git else 'false'}",
        f" - Platform: {sys.platform}",
    ]
    if local:
        lines.append(f" - Shell: {_shell()}")
        lines.append(f" - OS Version: {platform.system()} {platform.release()}".rstrip())
    if model:
        lines.append(f"You are powered by the model {model}.")
    lines.append(f"Today's date is {datetime.now().astimezone().date().isoformat()}.")
    lines.append(
        "File paths for read, edit, glob, and grep resolve relative to the "
        "primary working directory."
    )
    if branch:
        lines.append(f"Git branch: {branch}")
    if status:
        lines.append(f"Git status:\n{status}")
    if branch or status:
        lines.append(
            "The git branch/status above are a snapshot from when this "
            "task started; they do not update as the task runs. Run git "
            "commands for the current state."
        )
    lines.append("</workspace-environment>")
    return "\n".join(lines)


class Environment(Extension):
    """Constructor arguments:

    * ``model`` — the agent's model, named when the task's config sets none.
    * ``model_name`` — include the "You are powered by" line.
    """

    name = "workspace"

    def __init__(self, *, model: str | None = None, model_name: bool = True) -> None:
        self.model = model
        self.model_name = model_name

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        if kind != "environment":
            return state
        return data["text"] if isinstance(data, dict) and "text" in data else True

    async def on_start(self, view: TaskView) -> None:
        if view.ext_state(self.name):
            return
        config = view.state.config
        env = task_env(view.env, config)
        cwd = str(config.get("cwd") or env.cwd)
        model = str(config.get("model") or self.model or "") if self.model_name else ""
        text = await capture_environment(env, cwd, model)
        view.record(self.name, "environment", {"text": text},
                    message=user(text, origin="system"))

    async def resident(self, view: TaskView) -> Sequence[Message]:
        text = view.ext_state(self.name)
        return (user(text, origin="system"),) if isinstance(text, str) else ()
