"""What Claude Code's harness tells the model about where it runs, in the
same places: a git snapshot and the commit-attribution note in the first
user turn, the environment (with the model line, the token budget and
today's date) as a system turn right after it, and a ``<total_tokens>``
budget note after every prompt and tool result.

Everything is captured through the task's ExecEnv (``view.env``), never by a
host subprocess: on a container the facts describe the container's checkout,
and the shell / OS version lines (facts of this machine) are left out. git
runs with the same hardening the Bash tool's ``git status`` rule applies
(``-c core.fsmonitor=``, no hooks), so a repository's own config cannot make
the capture run a program. A ``filter.<driver>.clean`` program has no off
switch, so when the repository's own config (local or worktree scope)
defines any filter driver the status is skipped. The capture is recorded
once, when the task first starts, and never changes under the prompt cache.
"""

from __future__ import annotations

import os
import platform
import posixpath
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from noeta.core import models
from noeta.core.execenv import ExecEnv, is_local
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.types import Message, ToolResult, system_message, user
from noeta.toolkit.context import task_env
from noeta.toolkit.shell import _harden_git

#: Git status beyond this is cut at a line boundary and marked.
GIT_STATUS_MAX_BYTES = 2048
#: Commits listed in the git snapshot.
RECENT_COMMITS = 5
#: Global flags every capture git runs with: no fsmonitor hook, no index
#: refresh written back.
_GIT_FLAGS = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "--no-optional-locks")
#: Config scopes a person set up themselves (a filter there is trusted).
_TRUSTED_SCOPES = ("system", "global")
#: The budget a turn starts with (Claude Code's ``<total_tokens>``).
TOKEN_BUDGET = 15_000_000
#: Where the PR footer points.
PROJECT_URL = "https://github.com/initxy/noeta"

#: Places of this plugin's notes in the joined system turn (``Reminder.order``):
#: the environment first, the budget and the date last.
ORDER_ENVIRONMENT = 0
ORDER_BUDGET = 80
ORDER_DATE = 90


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


async def _git_status(env: ExecEnv, cwd: str) -> str | None:
    """The short status (``""`` when clean), ``None`` when not captured."""
    if await _repo_defines_filters(env, cwd):
        return None
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


async def _main_branch(env: ExecEnv, cwd: str) -> str:
    """The branch pull requests usually target: the remote's default, else a
    local ``main`` or ``master``."""
    remote = (await _git(env, cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")).strip()
    if remote:
        return remote.split("/", 1)[-1]
    for name in ("main", "master"):
        if (await _git(env, cwd, "rev-parse", "--verify", "--quiet",
                       f"refs/heads/{name}")).strip():
            return name
    return ""


def _shell() -> str:
    shell = os.environ.get("SHELL") or "unknown"
    return "zsh" if "zsh" in shell else "bash" if "bash" in shell else shell


async def capture_git(env: ExecEnv, cwd: str) -> str:
    """The git snapshot for the first user turn (``""`` outside a repository)."""
    try:
        if await env.stat(posixpath.join(cwd, ".git")) is None:
            return ""
    except Exception:  # noqa: BLE001 — capture is best-effort
        return ""
    branch = (await _git(env, cwd, "rev-parse", "--abbrev-ref", "HEAD")).strip()
    status = await _git_status(env, cwd)
    commits = (await _git(env, cwd, "log", "--oneline", "-n", str(RECENT_COMMITS))).rstrip("\n")
    return render_git(branch=branch, main=await _main_branch(env, cwd),
                      user=(await _git(env, cwd, "config", "user.name")).strip(),
                      status=status, commits=commits)


def render_git(*, branch: str, main: str, user: str, status: str | None,
               commits: str) -> str:
    shown = "(not captured)" if status is None else status or "(clean)"
    return "\n".join([
        "Here is context from the workspace that may help you answer:",
        "# Git status",
        "A snapshot taken when this conversation started; it does not change as you work.",
        "",
        f"Current branch: {branch or '(unknown)'}",
        "",
        f"Main branch (where pull requests usually go): {main or '(unknown)'}",
        "",
        f"Git user: {user or '(unset)'}",
        "",
        f"Status:\n{shown}",
        "",
        f"Recent commits:\n{commits or '(none)'}",
        "",
        "noeta attached this on its own; the user did not write it. It is about the "
        "user's own checkout and account, so there is no need to tell it back to them.",
    ])


async def capture_environment(env: ExecEnv, cwd: str, model: str) -> str:
    """The environment message's facts for ``cwd`` as ``env`` sees it."""
    try:
        is_git = await env.stat(posixpath.join(cwd, ".git")) is not None
    except Exception:  # noqa: BLE001 — capture is best-effort
        is_git = False
    return render_environment(cwd, model, is_git=is_git, local=is_local(env))


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def render_environment(cwd: str, model: str, *, is_git: bool, local: bool = True) -> str:
    lines = [
        "# Environment",
        "This conversation runs in the following environment:",
        f" - Primary working directory: {cwd}",
        f" - Is a git repository: {'true' if is_git else 'false'}",
        f" - Platform: {sys.platform}",
    ]
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


def model_line(model: str) -> str:
    spec = models.find_spec(model)
    name = spec.display_name if spec is not None else None
    line = f"Model: {name}, exact id `{model}`" if name else f"Model: `{model}`"
    cutoff = spec.knowledge_cutoff if spec is not None else None
    return f"{line}; knowledge cutoff {cutoff}." if cutoff else f"{line}."


def render_attribution(model: str) -> str | None:
    """The commit and PR attribution note; ``None`` for a model with no
    display name (nothing to put in the trailer)."""
    spec = models.find_spec(model)
    if spec is None or not spec.display_name:
        return None
    return "\n".join([
        "From here on, end the git commit messages and pull request descriptions you write "
        "with the lines below. This note replaces any earlier attribution note, earlier "
        "copies of itself included; the user's own instructions about these lines (an "
        "instructions file, a memory) take precedence over it; add no attribution line it "
        "does not list.",
        "- Commit messages end with:",
        f"Co-Authored-By: {spec.display_name} <noreply@anthropic.com>",
        "- Pull request descriptions end with:",
        f"🤖 Generated with [noeta]({PROJECT_URL})",
    ])


def budget_note(left: int) -> str:
    return f"<total_tokens>{left} tokens left</total_tokens>"


def date_line(today: str) -> str:
    return f"The date today is {today}."


DATE_CHANGED_REMINDER = (
    "The date moved on during this conversation: today is {date}. The user knows, so do not "
    "point it out."
)


def _owes_budget_note(messages: Sequence[Message]) -> bool:
    """Whether a prompt or a tool result came in since the last budget note."""
    for m in reversed(messages):
        if m.role == "assistant":
            return False
        if m.role == "system" and "<total_tokens>" in m.text:
            return False
        if m.role == "user" and (m.origin in (None, "human")
                                 or any(isinstance(b, ToolResult) for b in m.content)):
            return True
    return False


class Environment(Extension):
    """Constructor arguments:

    * ``model`` — the agent's model, named when the task's config sets none.
    * ``model_name`` — include the model line.
    * ``git_status`` — put the git snapshot in the first user turn.
    * ``attribution`` — put the commit / PR attribution note there.
    * ``token_budget`` — the ``<total_tokens>`` budget a turn starts with
      (``0``: no budget notes).
    * ``date_change`` — tell the model, at the start of a turn, when the date
      is no longer the one it was given (the prompt
      ``reminder/date_changed``; ``{date}`` is today).
    * ``today`` — today's date as ``YYYY-MM-DD`` (default: this machine's).
    """

    name = "environment"

    def __init__(self, *, model: str | None = None, model_name: bool = True,
                 git_status: bool = True, attribution: bool = True,
                 token_budget: int = TOKEN_BUDGET, date_change: bool = True,
                 today: Callable[[], str] = _today) -> None:
        self.model = model
        self.model_name = model_name
        self.git_status = git_status
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
                    "held": bool(data.get("held")), "turn_base": 0}
        if not isinstance(state, dict):
            return state
        if kind in ("git", "attribution"):
            return {**state, kind: data.get("text")}
        if kind == "sent":
            return {**state, "held": False}
        if kind == "date":
            return {**state, "date": data.get("date")}
        if kind == "budget":
            return {**state, "turn_base": data.get("base", 0)}
        return state

    def _model(self, view: TaskView) -> str:
        return str(view.state.config.get("model") or self.model or view.model or "")

    async def on_start(self, view: TaskView) -> None:
        if view.ext_state(self.name):
            return
        env = task_env(view.env, view.state.config)
        cwd = str(view.state.config.get("cwd") or env.cwd)
        model = self._model(view)
        text = await capture_environment(env, cwd, model if self.model_name else "")
        # Held for the first request's system turn, after the prompt.
        view.record(self.name, "environment", {"text": text, "date": self.today(),
                                               "held": True})
        if self.git_status:
            snapshot = await capture_git(env, cwd)
            if snapshot:
                view.record(self.name, "git", {"text": snapshot},
                            message=user(snapshot, origin="system"))
        note = render_attribution(model) if self.attribution and model else None
        if note:
            view.record(self.name, "attribution", {"text": note},
                        message=user(note, origin="system"))

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        st = view.ext_state(self.name)
        if not isinstance(st, dict):
            return ()
        out: list[Reminder] = []
        if st.get("held"):
            # The first request: the environment follows the first prompt.
            out.append(Reminder(text=st["text"], kind="sent", data={}, role="system",
                                order=ORDER_ENVIRONMENT))
            out.append(Reminder(text=date_line(st["date"]), kind="date",
                                data={"date": st["date"]}, role="system", order=ORDER_DATE))
        elif self.date_change and not view.state.steps_in_turn and st.get("date"):
            today = self.today()
            if st["date"] != today:
                text = view.prompt("reminder/date_changed", DATE_CHANGED_REMINDER)
                out.append(Reminder(text=text.replace("{date}", today), kind="date",
                                    data={"date": today}, role="system", order=ORDER_DATE))
        if self.token_budget and _owes_budget_note(view.state.messages):
            spent = view.state.usage.output
            base = spent if not view.state.steps_in_turn else st.get("turn_base", 0)
            left = max(self.token_budget - (spent - base), 0)
            out.append(Reminder(text=budget_note(left), kind="budget", data={"base": base},
                                role="system", order=ORDER_BUDGET))
        return out

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """After a compaction: the git snapshot and attribution note taken at
        the start, then the environment with the date it last gave."""
        st = view.ext_state(self.name)
        if not isinstance(st, dict):
            return ()
        out = [user(st[k], origin="system") for k in ("git", "attribution")
               if isinstance(st.get(k), str)]
        text = st.get("text")
        if isinstance(text, str):
            date = st.get("date")
            out.append(system_message(f"{text}\n\n{date_line(date)}" if date else text))
        return out
