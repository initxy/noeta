"""``Bash`` with ``run_in_background``, and the tools around background jobs:
``BashOutput``, ``KillShell`` and ``Monitor``. Texts and limits are 0.6's
(Claude Code's), byte for byte."""

from __future__ import annotations

import asyncio
import re
import shlex
from importlib import resources
from typing import Any

from noeta.core.shell import matching_rule, parse_argv
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.tool_helpers import Workspace, require_str, tool_error
from noeta.plugins.shell.bash import SHELL, BashTool, shell_state
from noeta.plugins.shell.jobs import Job, Registry
from noeta.plugins.shell.notices import format_duration

#: Claude Code's inline cap on a poll's new output.
SHELL_INLINE_MAX_CHARS = 30_000
#: The wait bounds of Claude Code's ``TaskOutput`` (default 30 s, max 10 min).
DEFAULT_WAIT_TIMEOUT_MS = 30_000
MAX_WAIT_TIMEOUT_MS = 600_000
#: Claude Code's ``Monitor`` deadline bounds.
DEFAULT_MONITOR_TIMEOUT_MS = 300_000
MIN_MONITOR_TIMEOUT_MS = 1_000
MAX_MONITOR_TIMEOUT_MS = 3_600_000
MONITOR_TIMEOUT_CAP_MS = 1_800_000

#: A ``cd`` among the command's words: a background launch then says the
#: session's directory did not move.
_CD_WORD = re.compile(r"(?:^|[\s;&|(])cd(?:\s|$)")

NOT_AVAILABLE = "background execution is not available on this host"


def _description(name: str) -> str:
    text = (resources.files(__package__) / f"{name}.md").read_text("utf-8")
    return text.rstrip("\n")


def _unknown(registry: Registry, ctx: ToolContext, job_id: str) -> str:
    """Why a job id is not in this worker's table."""
    job = shell_state(ctx.state).jobs.get(job_id) or {}
    owner = job.get("worker")
    if job.get("status") == "running" and owner and owner != registry.worker:
        return (f"background job {job_id!r} is running in another worker process; it cannot "
                "be read or stopped from here")
    return f"unknown background job {job_id!r}"


def background_started(job_id: str, *, ends_with_turn: bool) -> str:
    """The launch result: Claude Code's background-command wording."""
    if ends_with_turn:
        return (
            f"Command running in background with ID: {job_id}. If it exits "
            "while you are still working you will be notified, but it is "
            "terminated when you give your final response and no notification "
            "can follow that — so do not end your turn to wait for it; if you "
            "need its result, wait for it before giving your final response. "
            "To check interim output, use BashOutput on that ID."
        )
    return (
        f"Command running in background with ID: {job_id}. You will be "
        "notified when it completes. To check interim output, use BashOutput "
        "on that ID."
    )


def elide_middle(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    dropped = len(value) - max_chars
    half = max_chars // 2
    return (f"{value[:half]}\n... [{dropped} chars truncated] ...\n"
            f"{value[len(value) - (max_chars - half):]}")


def _js_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


def _wait_timeout_s(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        return DEFAULT_WAIT_TIMEOUT_MS / 1000
    return min(float(raw), MAX_WAIT_TIMEOUT_MS) / 1000


def _monitor_timeout_ms(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_MONITOR_TIMEOUT_MS
    bounded = min(max(float(raw), MIN_MONITOR_TIMEOUT_MS), MAX_MONITOR_TIMEOUT_MS)
    return min(bounded, MONITOR_TIMEOUT_CAP_MS)


def _root(ctx: ToolContext) -> str:
    return ctx.root_id or ctx.task_id


class BackgroundBash(BashTool):
    """``Bash``; with ``run_in_background`` the command starts as a job of
    the process-local registry and the call returns at once."""

    def __init__(self, registry: Registry, *, ends_with_turn: bool = False,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.registry = registry
        self.ends_with_turn = ends_with_turn

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        if not input.get("run_in_background"):
            return await super().run(input, ctx)
        command = input.get("command")
        if not isinstance(command, str) or not command.strip():
            return tool_error(self.name, "requires non-empty 'command'")
        description = input.get("description")
        started = await launch(self, ctx, command, self.name,
                               description if isinstance(description, str) else "")
        if isinstance(started, ToolOutput):
            return started
        job, cwd, tracked = started
        text = background_started(job.job_id, ends_with_turn=self.ends_with_turn)
        if tracked and _CD_WORD.search(command):
            # Claude Code's note for a backgrounded ``cd``.
            text += ("\n" + f"Session cwd remains {cwd}; directory changes made by "
                     "the backgrounded command do not apply to subsequent commands.")
        return text_output(text, job_id=job.job_id)


async def launch(shell: BackgroundBash, ctx: ToolContext, command: str, tool: str,
                 description: str, monitor_timeout_ms: float | None = None,
                 ) -> ToolOutput | tuple[Job, str, bool]:
    """Start ``command`` the way ``Bash`` would run it (same directory,
    variables and hardening), as a job. Returns the job, its directory and
    whether the command runs as written (so a ``cd`` in it matters)."""
    if ctx.post is None:
        return tool_error(tool, NOT_AVAILABLE)
    ws = await Workspace.of(ctx.env)
    cwd, _moved_back = await shell._start_cwd(ws, ctx)
    rule = matching_rule(command, shell.hardening_rules)
    argv = parse_argv(command) if rule is not None and rule.harden is not None else None
    run_command, tracked = command, True
    if rule is not None and rule.harden is not None and argv:
        run_command, tracked = shlex.join(rule.harden(argv)), False
    job = await shell.registry.spawn(
        env=ctx.env, command=run_command, cwd=cwd, env_vars=shell._command_env(ctx),
        root_id=_root(ctx), spawner_id=ctx.task_id, tool_use_id=ctx.call_id,
        description=description or command, post=ctx.post,
        monitor_timeout_ms=monitor_timeout_ms)
    if isinstance(job, str):
        return tool_error(tool, job)
    ctx.record(SHELL, "started", {
        "job_id": job.job_id, "command": command, "description": job.description,
        "tool_use_id": ctx.call_id, "monitor": monitor_timeout_ms is not None,
        "worker": shell.registry.worker})
    return job, cwd, tracked


class BashOutputTool:
    """``BashOutput``: the output a job produced since the last check."""

    name = "BashOutput"
    read_only = False
    clearable = True

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.description = _description("shell_poll")
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "bash_id": {"type": "string"},
                # optional per-line regex filter on the new output
                "filter": {"type": "string"},
                # wait for the job to end before answering
                "block": {"type": "boolean"},
                # how long ``block`` waits, in milliseconds (max 600000)
                "timeout": {"type": "number"},
            },
            "required": ["bash_id"],
            "additionalProperties": False,
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        job_id = require_str(input, "bash_id")
        if job_id is None:
            return tool_error(self.name, "requires a non-empty 'bash_id'")
        line_filter = input.get("filter")
        filter_re: re.Pattern[str] | None = None
        if line_filter is not None:
            if not isinstance(line_filter, str):
                return tool_error(self.name, "'filter' must be a string")
            try:
                filter_re = re.compile(line_filter)
            except re.error as exc:
                return tool_error(self.name, f"invalid 'filter' regex: {exc}")
        job = self.registry.find(_root(ctx), job_id)
        if job is None:
            return tool_error(self.name, _unknown(self.registry, ctx, job_id))
        waited = ""
        if input.get("block"):
            try:
                await asyncio.wait_for(job.done.wait(), _wait_timeout_s(input.get("timeout")))
                waited = "done"
            except TimeoutError:
                waited = "timeout"
        new_output, dropped = await self.registry.poll(job)
        status = job.status
        status_line = f"status: {status}"
        if status in ("exited", "killed") and job.exit_code is not None:
            status_line += f" (exit code {job.exit_code})"
        if filter_re is not None and new_output:
            new_output = "\n".join(
                line for line in new_output.splitlines() if filter_re.search(line))
        if waited == "timeout" and status == "running":
            status_line += (f" (still running after waiting "
                            f"{_js_number(_wait_timeout_s(input.get('timeout')))}s)")
        parts = [status_line]
        if dropped:
            parts.append(f"[{dropped} bytes of older output dropped (buffer overflow)]")
        if new_output:
            parts.append(elide_middle(new_output, SHELL_INLINE_MAX_CHARS))
        else:
            parts.append("(no new output)")
        return text_output("\n".join(parts), status=status)


class KillShellTool:
    """``KillShell``: stop a job; its end comes as the usual notice."""

    name = "KillShell"
    read_only = False
    clearable = False

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.description = _description("shell_kill")
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {"shell_id": {"type": "string"}},
            "required": ["shell_id"],
            "additionalProperties": False,
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        job_id = require_str(input, "shell_id")
        if job_id is None:
            return tool_error(self.name, "requires a non-empty 'shell_id'")
        job = self.registry.find(_root(ctx), job_id)
        if job is None:
            return tool_error(self.name, _unknown(self.registry, ctx, job_id))
        status = self.registry.kill(job)
        return text_output(f"Shell {job_id} is being terminated ({status})")


class MonitorTool:
    """``Monitor``: a job whose stdout lines are events, delivered as
    notifications until it exits or its deadline passes."""

    name = "Monitor"
    read_only = False
    clearable = False

    def __init__(self, shell: BackgroundBash) -> None:
        self.shell = shell
        self.description = _description("monitor")
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command or script. Each stdout line is an "
                        "event; exit ends the watch."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Short human-readable description of what you are "
                        "monitoring (shown in notifications)."
                    ),
                },
                "timeout_ms": {
                    "type": "number",
                    "default": DEFAULT_MONITOR_TIMEOUT_MS,
                    "minimum": MIN_MONITOR_TIMEOUT_MS,
                    "maximum": MAX_MONITOR_TIMEOUT_MS,
                    "description": (
                        "Kill the monitor after this deadline. Default "
                        "300000ms. Deadlines above 1800000ms are capped to "
                        "1800000ms. You are notified at expiry and can re-arm."
                    ),
                },
            },
            "required": ["description", "timeout_ms"],
            "additionalProperties": False,
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        command = input.get("command")
        if not isinstance(command, str) or not command.strip():
            return tool_error(self.name, "requires non-empty 'command'")
        description = input.get("description")
        if not isinstance(description, str) or not description.strip():
            return tool_error(self.name, "requires non-empty 'description'")
        timeout_ms = _monitor_timeout_ms(input.get("timeout_ms"))
        started = await launch(self.shell, ctx, command, self.name, description,
                               monitor_timeout_ms=timeout_ms)
        if isinstance(started, ToolOutput):
            return started
        job = started[0]
        output = (
            f"Monitor started (task {job.job_id}, expires in "
            f"{format_duration(timeout_ms)} unless the source ends first; you "
            "get one notice at expiry — re-arm if you still need the watch). "
            "You will be notified on each event. Keep working — do not poll or "
            "sleep. Events may arrive while you are waiting for the user — an "
            "event is not their reply."
        )
        if self.shell.ends_with_turn:
            output += (
                " Its events reach you only while you are still working: it is "
                "terminated when you give your final response, and nothing can "
                "follow that."
            )
        return text_output(output, job_id=job.job_id)
