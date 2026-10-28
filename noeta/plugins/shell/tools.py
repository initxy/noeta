"""``Bash`` with ``run_in_background``, and the tools around background jobs:
``Monitor`` and ``TaskStop``, both deferred. Limits are Claude Code's."""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.shell.bash import (
    SCHEMA_DIALECT,
    SHELL,
    BashTool,
    confine,
    git_prelude,
    guard_root,
    sandbox_required,
    shell_state,
)
from noeta.plugins.shell.hardening import hardened_argv
from noeta.plugins.shell.jobs import Job, Registry
from noeta.plugins.shell.notices import format_duration
from noeta.plugins.shell.websocket import parse_ws
from noeta.toolkit.sandbox import domain_allowed, resolve_allowed, sandbox_of
from noeta.toolkit.tools import Workspace, require_str, tool_error

#: Claude Code's ``Monitor`` deadline bounds.
DEFAULT_MONITOR_TIMEOUT_MS = 300_000
MIN_MONITOR_TIMEOUT_MS = 1_000
MAX_MONITOR_TIMEOUT_MS = 3_600_000
MONITOR_TIMEOUT_CAP_MS = 1_800_000

#: A ``cd`` among the command's words: a background launch then says the
#: session's directory did not move.
_CD_WORD = re.compile(r"(?:^|[\s;&|(])cd(?:\s|$)")

NOT_AVAILABLE = "background execution is not available on this host"
#: Why a job is gone: its worker held the task tree while the job ran, so a
#: worker that does not have it is not the one that started it.
LOST_REASON = ("the worker process running it stopped, so its further output and exit "
               "status are unknown")


def _description(name: str) -> str:
    text = (resources.files(__package__) / f"{name}.md").read_text("utf-8")
    return text.rstrip("\n")


def _unknown(registry: Registry, ctx: ToolContext, job_id: str) -> str:
    """Why a job id is not in this worker's table."""
    job = shell_state(ctx.state).jobs.get(job_id) or {}
    if job.get("status") in ("running", "lost"):
        return f"background job {job_id!r} was lost: {LOST_REASON}"
    return f"unknown background job {job_id!r}"


def background_started(job_id: str, out_path: str, *, ends_with_turn: bool) -> str:
    """The launch result: the ID, the output file, and how the end is
    reported."""
    text = (f"Started in the background with ID {job_id}; its output is written to "
            f"{out_path}. ")
    if ends_with_turn:
        return text + (
            "You are notified if it ends while you are still working, but it is stopped "
            "when you give your final response and nothing can follow that: if you need "
            "its result, wait for it before you finish.")
    return text + (
        "You will be notified when it finishes, so do not poll it or sleep waiting for it. "
        "Read the file only if the user asks how it is going or you need a line from it "
        "(a server's ready line, a port) before you can continue.")


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
                               description if isinstance(description, str) else "",
                               disable=input.get("dangerouslyDisableSandbox"))
        if isinstance(started, ToolOutput):
            return started
        job, cwd, tracked = started
        text = background_started(job.job_id, job.out_path,
                                  ends_with_turn=self.ends_with_turn)
        if tracked and _CD_WORD.search(command):
            # Claude Code's note for a backgrounded ``cd``.
            text += ("\n" + f"Session cwd remains {cwd}; directory changes made by "
                     "the backgrounded command do not apply to subsequent commands.")
        return text_output(text, job_id=job.job_id)


def _ws_policy(env: Any, url: str) -> Callable[[str, int], Awaitable[str]] | str | None:
    """How a ``Monitor`` WebSocket, which the worker opens itself, keeps to
    the sandbox's network rules while commands run confined: the resolver
    that connects only to an allowed host at a checked address, a refusal,
    or ``None`` (no sandbox in force)."""
    sandbox = sandbox_of(env)
    if sandbox is None:
        return None
    reason = sandbox.unavailable()
    if reason is not None:
        return sandbox_required(reason) if sandbox.settings.fail_if_unavailable else None
    s = sandbox.settings
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "wss" else 80)
    if not domain_allowed(host, port, s.allowed_domains, s.denied_domains):
        return (f"the sandbox does not allow connecting to {host}:{port}; add it to "
                "sandbox.network.allowedDomains to watch this socket")

    async def resolve(name: str, number: int) -> str:
        return await resolve_allowed(name, number, s.allowed_domains, s.denied_domains)

    return resolve


async def launch(shell: BackgroundBash, ctx: ToolContext, command: str, tool: str,
                 description: str, monitor_timeout_ms: float | None = None,
                 disable: Any = False,
                 ws: tuple[str, list[str]] | None = None,
                 ) -> ToolOutput | tuple[Job, str, bool]:
    """Start ``command`` the way ``Bash`` would run it (same directory,
    variables, hardening and sandbox), as a job. Returns the job, its
    directory and whether the command runs as written (so a ``cd`` in it
    matters). A monitor with ``ws`` streams that WebSocket instead
    (``command`` then only names it)."""
    if ctx.post is None:
        return tool_error(tool, NOT_AVAILABLE)
    if ws is not None:
        resolve = _ws_policy(ctx.env, ws[0])
        if isinstance(resolve, str):
            return tool_error(tool, resolve)
        release = await ctx.hold() if ctx.hold is not None else None
        job = await shell.registry.spawn(
            env=ctx.env, command=command, cwd="", env_vars={}, root_id=_root(ctx),
            spawner_id=ctx.task_id, tool_use_id=ctx.call_id, description=description,
            post=ctx.post, monitor_timeout_ms=monitor_timeout_ms, release=release, ws=ws,
            ws_resolve=resolve)
        if isinstance(job, str):
            return tool_error(tool, job)
        ctx.record(SHELL, "started", {
            "job_id": job.job_id, "command": command, "description": job.description,
            "tool_use_id": ctx.call_id, "monitor": True, "worker": shell.registry.worker})
        return job, "", False
    workspace = await Workspace.of(ctx.env)
    cwd, _moved_back = await shell._start_cwd(workspace, ctx)
    prelude = (git_prelude(command, guard_root(workspace, ctx))
               if shell.hardening_rules else "")
    hardened = None if prelude else hardened_argv(command, shell.hardening_rules)
    run_command, tracked = prelude + command, True
    if hardened is not None:
        run_command, tracked = shlex.join(hardened), False
    confined = await confine(ctx.env, command, disable, ["bash", "-c", run_command], cwd)
    if isinstance(confined, str):
        return tool_error(tool, confined)
    # The job lives in this process only: keep the task tree with this
    # worker while it runs, so the next turn can read and stop it.
    held = await ctx.hold() if ctx.hold is not None else None
    release = held
    if confined is not None:
        sandbox_done = confined

        async def release_all() -> None:
            await sandbox_done.close()
            if held is not None:
                await held()

        release = release_all
    job = await shell.registry.spawn(
        env=ctx.env, command=run_command, cwd=cwd,
        env_vars={**shell._command_env(ctx), **(confined.env if confined else {})},
        root_id=_root(ctx), spawner_id=ctx.task_id, tool_use_id=ctx.call_id,
        description=description or command, post=ctx.post,
        monitor_timeout_ms=monitor_timeout_ms, release=release,
        argv=confined.argv if confined else None)
    if isinstance(job, str):
        return tool_error(tool, job)
    ctx.record(SHELL, "started", {
        "job_id": job.job_id, "command": command, "description": job.description,
        "tool_use_id": ctx.call_id, "monitor": monitor_timeout_ms is not None,
        "worker": shell.registry.worker})
    return job, cwd, tracked


class TaskStopTool:
    """``TaskStop``: stop a background job; its end comes as the usual
    notice. Deferred."""

    name = "TaskStop"
    read_only = False
    clearable = False
    deferred = True
    description = (
        "Stops a background task (a Bash command started with `run_in_background`, or a "
        "Monitor) by its ID.\n\n"
        "- `task_id` is the ID the launch result gave\n"
        "- The result says whether the task was stopped\n"
        "- Use it to end a long-running task you no longer need")
    input_schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "task_id": {"type": "string",
                        "description": "ID of the background task to stop."},
            "shell_id": {"type": "string",
                         "description": "Older name for task_id; use task_id."},
        },
        "additionalProperties": False,
    }

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        job_id = require_str(input, "task_id") or require_str(input, "shell_id")
        if job_id is None:
            return tool_error(self.name, "requires a non-empty 'task_id'")
        job = self.registry.find(_root(ctx), job_id)
        if job is None:
            return tool_error(self.name, _unknown(self.registry, ctx, job_id))
        if job.status != "running":
            return tool_error(self.name, f"task {job_id} is not running ({job.status})")
        self.registry.kill(job)
        return text_output(f"Stopped task {job_id} ({job.description})", job_id=job_id)


class MonitorTool:
    """``Monitor``: a job whose stdout lines are events, delivered as
    notifications until it exits or its deadline passes."""

    name = "Monitor"
    read_only = False
    clearable = False
    deferred = True

    def __init__(self, shell: BackgroundBash) -> None:
        self.shell = shell
        self.description = _description("monitor")
        self.input_schema: dict[str, Any] = {
            "$schema": SCHEMA_DIALECT,
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": ("The script to run. Every line it prints on stdout is "
                                    "an event; its exit ends the watch."),
                },
                "ws": {
                    "type": "object",
                    "description": ("A WebSocket to connect to instead of a command. Every "
                                    "text message is an event and a binary one shows as a "
                                    "placeholder line; the socket closing ends the watch. "
                                    "Not together with command."),
                    "properties": {
                        "url": {"type": "string"},
                        "protocols": {"type": "array", "items": {
                            "type": "string",
                            "pattern": "^[!#$%&'*+.^_`|~0-9A-Za-z-]+$"}},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                "description": {
                    "type": "string",
                    "description": ("A short, specific label for what is watched; shown "
                                    "with every notification."),
                },
                "timeout_ms": {
                    "type": "number",
                    "default": DEFAULT_MONITOR_TIMEOUT_MS,
                    "minimum": MIN_MONITOR_TIMEOUT_MS,
                    "maximum": MAX_MONITOR_TIMEOUT_MS,
                    "description": ("When to stop the monitor, in milliseconds (default "
                                    "300000; anything above 1800000 counts as 1800000). "
                                    "You get a notice when it expires and may start it "
                                    "again."),
                },
            },
            "required": ["description", "timeout_ms"],
            "additionalProperties": False,
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        command = input.get("command")
        ws: tuple[str, list[str]] | None = None
        if input.get("ws") is not None:
            if command is not None:
                return tool_error(self.name, "takes 'command' or 'ws', not both")
            parsed = parse_ws(input["ws"])
            if isinstance(parsed, str):
                return tool_error(self.name, parsed)
            ws = parsed
            command = f"ws {ws[0]}"
        if not isinstance(command, str) or not command.strip():
            return tool_error(self.name, "requires non-empty 'command' (or 'ws')")
        description = input.get("description")
        if not isinstance(description, str) or not description.strip():
            return tool_error(self.name, "requires non-empty 'description'")
        timeout_ms = _monitor_timeout_ms(input.get("timeout_ms"))
        started = await launch(self.shell, ctx, command, self.name, description,
                               monitor_timeout_ms=timeout_ms, ws=ws)
        if isinstance(started, ToolOutput):
            return started
        job = started[0]
        output = (
            f"Monitor started with ID {job.job_id}; it expires in "
            f"{format_duration(timeout_ms)} unless the "
            f"{'socket closes' if ws else 'script ends'} first, with one notice "
            "at expiry (start it again if you still need it). Each event reaches you as a "
            "notification: keep working, do not poll or sleep. An event that arrives while "
            "you wait for the user is not their reply."
        )
        if self.shell.ends_with_turn:
            output += (
                " Its events reach you only while you are still working: it is "
                "terminated when you give your final response, and nothing can "
                "follow that."
            )
        return text_output(output, job_id=job.job_id)
