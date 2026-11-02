"""``Bash`` with ``run_in_background``, and the tools around background jobs:
``Monitor`` and ``TaskStop``, both deferred. Limits are Claude Code's."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from noeta.core.errors import HeldElsewhere
from noeta.core.execenv import RunResult
from noeta.core.holds import Release
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.shell.bash import SCHEMA_DIALECT, BashTool
from noeta.plugins.shell.cwd import CWD_MARK, start_cwd
from noeta.plugins.shell.hardening import command_script
from noeta.plugins.shell.jobs import Job, Registry
from noeta.plugins.shell.notices import format_duration
from noeta.plugins.shell.sandbox import confine, sandbox_required
from noeta.plugins.shell.state import SHELL, shell_state
from noeta.plugins.shell.websocket import parse_ws
from noeta.toolkit.sandbox import domain_allowed, resolve_allowed, sandbox_of
from noeta.toolkit.shell_syntax import parse
from noeta.toolkit.tools import Workspace, load_description, require_str, tool_error

_log = logging.getLogger(__name__)

#: Claude Code's ``Monitor`` deadline bounds.
DEFAULT_MONITOR_TIMEOUT_MS = 300_000
MIN_MONITOR_TIMEOUT_MS = 1_000
MAX_MONITOR_TIMEOUT_MS = 3_600_000
MONITOR_TIMEOUT_CAP_MS = 1_800_000
#: A foreground command's output is shown live (``ToolContext.progress``)
#: once it has run this long, then every ``PROGRESS_EVERY`` seconds: its
#: last ``PROGRESS_BYTES``.
PROGRESS_AFTER, PROGRESS_EVERY, PROGRESS_BYTES = 2.0, 1.0, 4096

#: A ``cd`` among the command's words: a background launch then says the
#: session's directory did not move.
_CD_WORD = re.compile(r"(?:^|[\s;&|(])cd(?:\s|$)")

NOT_AVAILABLE = "background execution is not available on this host"
#: Why a job cannot start: a tree's jobs all run on one worker, and another
#: worker holds the tree (for a job of its own) or runs one of its tasks.
HELD_ELSEWHERE = ("cannot start a background command now: another worker process is running "
                  "part of this task tree (a background command or another task), and a "
                  "background command needs the whole tree on one worker; run the command "
                  "in the foreground, or try again later")
#: Why a job is gone: its worker held the task tree while the job ran, so a
#: worker that does not have it is not the one that started it.
LOST_REASON = ("the worker process running it stopped, so its further output and exit "
               "status are unknown")


def _unknown(registry: Registry, ctx: ToolContext, job_id: str) -> str:
    """Why a job id is not in this worker's table."""
    job = shell_state(ctx.state).jobs.get(job_id) or {}
    if job.get("status") in ("running", "lost"):
        return f"background job {job_id!r} was lost: {LOST_REASON}"
    return f"No task found with ID: {job_id}"


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


def _first_word(command: str) -> str:
    parsed = parse(command)
    if parsed.opaque or not parsed.commands or not parsed.commands[0].argv:
        return command.split()[0] if command.split() else ""
    return parsed.commands[0].argv[0]


#: The descriptor the EXIT trap writes the final directory to: fd 3 moved
#: out of the way first, so a command that opens fd 3 itself keeps its file
#: (a path would have to be writable inside the sandbox; a descriptor
#: passes through it).
_CWD_FD = 254


def _track_cwd_fd(script: str) -> str:
    """``script`` with an EXIT trap writing the shell's final directory to
    the job's cwd file, which the job opened on fd 3 (quietly, if fd 3 did
    not reach it)."""
    return (f"exec 2>&1\n{{ exec {_CWD_FD}>&3 3>&-; }} 2>/dev/null\n"
            f"trap '{{ pwd -P >&{_CWD_FD}; }} 2>/dev/null' EXIT\n{script}")


def _output_so_far(data: bytes, truncated: bool) -> str:
    """The tail of a moved command's output: a line cut by the window is
    dropped."""
    text = data.decode("utf-8", errors="replace")
    if truncated and "\n" in text:
        text = text[text.index("\n") + 1:]
    return text.strip("\n")


def moved_to_background(job_id: str, out_path: str, timeout_s: int, *,
                        printed: bool, ends_with_turn: bool) -> str:
    """The result of a foreground command its timeout moved to the
    background."""
    text = (f"The command was still running when its {timeout_s}s timeout ran out, so it "
            f"was moved to the background with ID {job_id}; its output goes on into "
            f"{out_path}. ")
    if ends_with_turn:
        text += ("You are notified if it ends while you are still working, but it is "
                 "stopped when you give your final response: if you need its result, wait "
                 "for it before you finish. ")
    else:
        text += "You will be notified when it ends. "
    text += ("It is still running, so do not start it again; stop it with TaskStop if you no "
             "longer need it. Read that file for its output so far.")
    if not printed:
        text += (" It had printed nothing yet: a program that buffers its output, or a pipe "
                 "into one such as tail, can leave the file empty until it ends.")
    return text


#: How much of a moved command's output its result shows (Claude Code's).
MOVED_OUTPUT_BYTES = 2_000


class BackgroundBash(BashTool):
    """``Bash``; with ``run_in_background`` the command starts as a job of
    the process-local registry and the call returns at once. A foreground
    command runs as a job too (unless it starts with ``sleep``): it is
    waited for, and if its timeout runs out it is moved to the background
    instead of killed, as Claude Code does."""

    refuses_long_sleep = True
    progress_after, progress_every = PROGRESS_AFTER, PROGRESS_EVERY

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
            text += ("\n" + f"The shell's directory stays {cwd}: a cd inside a "
                     "background command does not carry over to later commands.")
        return text_output(text, job_id=job.job_id)

    async def _execute(self, ctx: ToolContext, *, command: str, script: str,
                       hardened: list[str] | None, cwd: str, timeout_s: int, disable: Any,
                       description: str) -> tuple[RunResult, list[str]] | ToolOutput:
        if ctx.post is None or _first_word(command) == "sleep":
            return await super()._execute(
                ctx, command=command, script=script, hardened=hardened, cwd=cwd,
                timeout_s=timeout_s, disable=disable, description=description)
        if hardened is not None:
            argv = ["bash", "-c", 'exec "$@"', "bash", *hardened]
        else:
            argv = ["bash", "-c", _track_cwd_fd(script)]
        confined = await confine(ctx.env, command, disable, argv, cwd)
        if isinstance(confined, str):
            return tool_error(self.name, confined)
        job = await self.registry.spawn(
            env=ctx.env, command=script if hardened is None else shlex.join(hardened),
            cwd=cwd, env_vars={**self._command_env(ctx), **(confined.env if confined else {})},
            root_id=_root(ctx), spawner_id=ctx.task_id, tool_use_id=ctx.call_id,
            description=description or command, post=ctx.post,
            argv=confined.argv if confined else argv, foreground=True)
        if isinstance(job, str):  # no job slot: run it the plain way
            if confined is not None:
                await confined.close()
            return await super()._execute(
                ctx, command=command, script=script, hardened=hardened, cwd=cwd,
                timeout_s=timeout_s, disable=disable, description=description)
        assert job.proc is not None
        held: Release | None = None
        refused = False
        try:
            await self._wait_showing(ctx, job, float(timeout_s))
            if ctx.hold is not None and not job.proc.done():
                try:
                    held = await ctx.hold()
                except HeldElsewhere:
                    refused = True
                except Exception:  # noqa: BLE001 — no hold means no background job
                    # A hold that could not be taken (a store fault) keeps
                    # nothing in the background: it times out as refused.
                    _log.warning("Bash: no hold for a timed-out command", exc_info=True)
                    refused = True
        except asyncio.CancelledError:
            self.registry.kill(job, quiet=True)
            self.registry.discard_later(job)
            if confined is not None:
                await asyncio.shield(confined.close())
            raise

        async def release() -> None:
            if confined is not None:
                await confined.close()
            if held is not None:
                await held()

        if not refused and self.registry.to_background(job, release):
            return await self._moved(ctx, job, command, cwd, timeout_s, held,
                                     tracked=hardened is None)
        if held is not None:
            await held()  # it ended meanwhile: nothing to keep
        if refused:
            # Another worker holds the tree: a job here could not be read or
            # stopped later, so it times out as a plain command does.
            self.registry.kill(job, quiet=True)
        try:
            return await self._foreground_result(job), (
                confined.violations() if confined else [])
        finally:
            if confined is not None:
                await confined.close()
            await self.registry.discard(job)

    async def _wait_showing(self, ctx: ToolContext, job: Job, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for ``job``, showing its latest
        output meanwhile (as Claude Code shows a running command's)."""
        assert job.proc is not None
        if ctx.progress is None:
            await asyncio.wait({job.proc}, timeout=timeout)
            return
        loop = asyncio.get_running_loop()
        deadline, step = loop.time() + timeout, self.progress_after
        while not job.proc.done() and (left := deadline - loop.time()) > 0:
            await asyncio.wait({job.proc}, timeout=min(step, left))
            step = self.progress_every
            if not job.proc.done() and loop.time() < deadline:
                try:
                    st = await job.env.stat(job.out_path)
                    size = st.size if st is not None else 0
                    take = min(size, PROGRESS_BYTES)
                    data = await job.env.read_range(job.out_path, size - take, take) \
                        if take else b""
                    ctx.progress(data.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001 — showing progress never fails the call
                    _log.debug("Bash: showing progress failed", exc_info=True)

    async def _foreground_result(self, job: Job) -> RunResult:
        """What a job waited for in the foreground printed (its last
        ``output_cap`` bytes), with its final directory on the marker
        line."""
        await job.done.wait()
        env = job.env
        st = await env.stat(job.out_path)
        size = st.size if st is not None else 0
        take = min(size, self.output_cap)
        data = await env.read_range(job.out_path, size - take, take) if take else b""
        where = b""
        if job.cwd_path:
            cst = await env.stat(job.cwd_path)
            if cst is not None and cst.size:
                where = (await env.read_range(job.cwd_path, 0, cst.size)).strip()
        if where:
            data += b"\n" + CWD_MARK.encode() + where + b"\n"
        killed = job.status == "killed"
        return RunResult(exit_code=job.exit_code if job.exit_code is not None else -1,
                         stdout=data, stdout_truncated=size > take, timed_out=killed)

    async def _moved(self, ctx: ToolContext, job: Job, command: str, cwd: str,
                     timeout_s: int, held: Release | None, *,
                     tracked: bool) -> ToolOutput:
        if held is not None:
            self.registry.hold_with(job, held.tree)
        ctx.record(SHELL, "started", {
            "job_id": job.job_id, "command": command, "description": job.description,
            "tool_use_id": ctx.call_id, "monitor": False, "output_file": job.out_path,
            **_holder(job)})
        try:
            st = await job.env.stat(job.out_path)
            size = st.size if st is not None else 0
            take = min(size, MOVED_OUTPUT_BYTES)
            data = await job.env.read_range(job.out_path, size - take, take) if take else b""
        except OSError:
            size, data = 0, b""
        so_far = _output_so_far(data, size > len(data))
        text = moved_to_background(job.job_id, job.out_path, timeout_s, printed=size > 0,
                                   ends_with_turn=self.ends_with_turn)
        if tracked and _CD_WORD.search(command):
            text += ("\n" + f"The shell's directory stays {cwd}: a cd inside a "
                     "background command does not carry over to later commands.")
        return text_output(f"{so_far}\n{text}" if so_far else text, job_id=job.job_id,
                           timed_out_after_ms=timeout_s * 1000)


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
        try:
            release = await ctx.hold() if ctx.hold is not None else None
        except HeldElsewhere:
            return tool_error(tool, HELD_ELSEWHERE)
        job = await shell.registry.spawn(
            env=ctx.env, command=command, cwd="", env_vars={}, root_id=_root(ctx),
            spawner_id=ctx.task_id, tool_use_id=ctx.call_id, description=description,
            post=ctx.post, monitor_timeout_ms=monitor_timeout_ms, release=release,
            tree=release.tree if release is not None else None, ws=ws, ws_resolve=resolve)
        if isinstance(job, str):
            return tool_error(tool, job)
        ctx.record(SHELL, "started", {
            "job_id": job.job_id, "command": command, "description": job.description,
            "tool_use_id": ctx.call_id, "monitor": True, **_holder(job)})
        return job, "", False
    workspace = await Workspace.of(ctx.env)
    cwd, _moved_back = await start_cwd(workspace, ctx)
    run_command, hardened = command_script(command, workspace, ctx, shell.hardening_rules)
    tracked = True
    if hardened is not None:
        run_command, tracked = shlex.join(hardened), False
    confined = await confine(ctx.env, command, disable, ["bash", "-c", run_command], cwd)
    if isinstance(confined, str):
        return tool_error(tool, confined)
    # The job lives in this process only: keep the task tree with this
    # worker while it runs, so the next turn can read and stop it.
    try:
        held = await ctx.hold() if ctx.hold is not None else None
    except HeldElsewhere:
        if confined is not None:
            await confined.close()
        return tool_error(tool, HELD_ELSEWHERE)
    release_job: Callable[[], Awaitable[None]] | None = held
    if confined is not None:
        sandbox_done = confined

        async def release_all() -> None:
            await sandbox_done.close()
            if held is not None:
                await held()

        release_job = release_all
    job = await shell.registry.spawn(
        env=ctx.env, command=run_command, cwd=cwd,
        env_vars={**shell._command_env(ctx), **(confined.env if confined else {})},
        root_id=_root(ctx), spawner_id=ctx.task_id, tool_use_id=ctx.call_id,
        description=description or command, post=ctx.post,
        monitor_timeout_ms=monitor_timeout_ms, release=release_job,
        tree=held.tree if held is not None else None,
        argv=confined.argv if confined else None)
    if isinstance(job, str):
        return tool_error(tool, job)
    ctx.record(SHELL, "started", {
        "job_id": job.job_id, "command": command, "description": job.description,
        "tool_use_id": ctx.call_id, "monitor": monitor_timeout_ms is not None,
        "output_file": job.out_path, **_holder(job)})
    return job, cwd, tracked


def _holder(job: Job) -> dict[str, Any]:
    """Who runs the job: its worker and the epoch of the hold it lives by
    (what ``hold_lost`` names when that worker is gone)."""
    out: dict[str, Any] = {"worker": job.worker}
    if job.tree is not None:
        out["hold"] = job.tree.epoch
    return out


class TaskStopTool:
    """``TaskStop``: stop a background job; its end comes as the usual
    notice. Deferred."""

    name = "TaskStop"
    read_only = False
    clearable = False
    deferred = True
    search_hint = "stop or kill a running background task"
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
        if job is None or job.foreground:
            return tool_error(self.name, _unknown(self.registry, ctx, job_id))
        if job.status != "running":
            return tool_error(self.name, f"Task {job_id} is not running (status: "
                                         f"{Registry.status_of(job)})")
        self.registry.kill(job, quiet=True)  # Claude Code tells no end after a stop
        # Claude Code's result: the stop as a JSON object.
        return text_output(json.dumps({
            "message": f"Successfully stopped task: {job_id} ({job.description})",
            "task_id": job_id, "task_type": "monitor_ws" if job.ws else "local_bash",
            "command": job.description}, ensure_ascii=False), job_id=job_id)


class MonitorTool:
    """``Monitor``: a job whose stdout lines are events, delivered as
    notifications until it exits or its deadline passes."""

    name = "Monitor"
    read_only = False
    clearable = False
    deferred = True
    search_hint = ("watch or monitor a process, log or command and get each "
                   "output line as a notification")
    #: Claude Code's Monitor result limit.
    max_result_chars = 10_000

    def __init__(self, shell: BackgroundBash) -> None:
        self.shell = shell
        self.description = load_description("noeta.plugins.shell", "monitor")
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
