"""``Bash`` plus the two tools that manage the background jobs it starts.

The tier is fixed at construction: ``ALLOWLIST`` rejects shell metacharacters
before tokenisation and runs the structurally matched argv directly with no
shell, so nothing is ever executed as a substring of the model's string, while
``ARBITRARY`` is a real ``bash -c`` whose safety boundary is the host's
PermissionGuard plus approval predicate rather than an argv wall. Either way a
command gets ``cwd = workspace.root``, a bounded timeout, a scrubbed
environment, and a capped inline rendering (the full streams still go to the
ContentStore as audit artifacts) — and nothing beyond that: the spawned process
is **not** sandboxed and can write files anywhere on the host, so ``Bash``
suits a trusted workspace only.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.env import scrub_env
from noeta.runtime.exec_env import ExecEnv, LocalExecEnv
from noeta.builtins.fs.impl.shell_rules import DEFAULT_SHELL_RULES
from noeta.runtime.shell_policy import (
    DEFAULT_SHELL_OUTPUT_CAP,
    DEFAULT_SHELL_TIMEOUT_S,
    ShellMode,
    AllowRule,
    _has_shell_meta,
    _matches_allowlist,
    _parse_argv,
    _resolve_timeout,
)
from noeta.runtime.subproc import RunOutcome, runner_with_spawn_hook
from noeta.runtime.workspace import WorkspaceRoot
from noeta.tools.invocation import require_str
from noeta.protocols.resources import load_markdown
from noeta.tools.limits import (
    SHELL_INLINE_MAX_CHARS,
    SUMMARY_EMBED_MAX_BYTES,
    elide_middle,
    truncate_bytes,
)


__all__ = [
    "ShellKillTool",
    "ShellPollTool",
    "ShellRunTool",
]


def _err(name: str, message: str) -> ToolResult:
    return ToolResult(success=False, summary=f"{name}: {message}")


def _render_streams(stdout: bytes, stderr: bytes) -> str:
    """The model-facing body: stdout, then stderr under a label only when both
    streams carry content (matching the familiar single-stream view when only
    one does)."""
    out_text = stdout.decode("utf-8", errors="replace").rstrip("\n")
    err_text = stderr.decode("utf-8", errors="replace").rstrip("\n")
    if out_text and err_text:
        body = f"{out_text}\nstderr:\n{err_text}"
    else:
        body = out_text or err_text
    return elide_middle(body, SHELL_INLINE_MAX_CHARS)


@dataclass
class ShellRunTool:
    """Shell runner with a strict (allowlist) and a full-bash (arbitrary) tier
    (tool name ``Bash``).

    Honest boundary: Noeta guarantees cwd = workspace, scrubbed env, bounded
    timeout, and a capped inline rendering. It does **not** sandbox the spawned
    process — commands execute workspace code, which can do arbitrary local IO.
    Trusted-workspace use only.
    """

    workspace: WorkspaceRoot
    mode: ShellMode = ShellMode.ALLOWLIST
    timeout_s: int = DEFAULT_SHELL_TIMEOUT_S
    output_cap: int = DEFAULT_SHELL_OUTPUT_CAP
    rules: tuple[AllowRule, ...] = field(
        default_factory=lambda: DEFAULT_SHELL_RULES
    )
    runner: Optional[Callable[..., subprocess.CompletedProcess[bytes]]] = None
    #: Backend the FOREGROUND command runs through — the local host or a
    #: sandbox container. Background spawns always go through the host's
    #: ``background_runner`` instead.
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "Bash"
    description: str = field(default=load_markdown(__package__, "shell_run"))
    # High risk so PermissionGuard treats this as privileged.
    risk_level: str = "high"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                # per-call wall-clock cap in milliseconds (ceiling 600000)
                "timeout": {"type": "number"},
                # UI hint only; does not affect execution
                "description": {"type": "string"},
                # launch detached instead of blocking the engine main loop on a
                # possibly long-running process
                "run_in_background": {"type": "boolean"},
            },
            "required": ["command"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if self.mode is ShellMode.OFF:
            return _err(self.name, "Bash is disabled")
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return _err(self.name, "requires non-empty 'command'")
        if self.mode is ShellMode.ALLOWLIST:
            if _has_shell_meta(command):
                return _err(
                    self.name,
                    "shell metacharacters ('; & | < > $ ` ( ) \\n') are not allowed",
                )
            argv = _parse_argv(command)
            if argv is None or not argv:
                return _err(
                    self.name, "could not parse 'command' (unbalanced quotes?)"
                )
            if not _matches_allowlist(argv, self.rules):
                return _err(
                    self.name,
                    f"command {argv[0]!r} not in allowlist; "
                    "use --allow-shell to run arbitrary commands",
                )
            exec_argv = argv
        else:  # ShellMode.ARBITRARY — full bash
            exec_argv = ["bash", "-c", command]
        timeout_s = _resolve_timeout(arguments.get("timeout"), self.timeout_s)
        # The tier gate above still applies; the sync timeout does NOT — a
        # backgrounded process outlives this call.
        if bool(arguments.get("run_in_background")):
            # A sandbox backend cannot run host-side background jobs: the runner
            # spawns HOST subprocesses. Refuse cleanly rather than silently
            # running on the wrong machine. ``getattr`` defaults to True so a
            # backend that does not declare the attribute keeps working.
            if not getattr(self.exec_env, "supports_background", True):
                return _err(
                    self.name,
                    "run_in_background is not supported in sandbox mode (v1); "
                    "run the command in the foreground instead",
                )
            return self._spawn_background(exec_argv, command, ctx)
        outcome, interrupted = self._run_foreground(exec_argv, timeout_s, ctx)
        return _build_shell_result(
            self.name,
            command=command,
            outcome=outcome,
            ctx=ctx,
            timeout_s=timeout_s,
            interrupted=interrupted,
        )

    def _run_foreground(
        self, exec_argv: list[str], timeout_s: int, ctx: ToolContext
    ) -> tuple[RunOutcome, bool]:
        """Run the blocking command, registered in the session kill table.

        The registration (via the default runner's ``on_spawn`` hook) is what
        lets the human-stop family — interrupt / cancel / close — reap the
        group while this thread is blocked in ``communicate()``: the wait
        returns immediately and the second value reports *interrupted*, so the
        result never masquerades as a plain exit or a timeout. Registration
        engages only where a real HOST group exists to kill: the default local
        runner (an injected test runner spawns nothing; a sandbox backend
        ignores ``runner`` and runs remotely) and a host whose runner exposes
        the foreground surface (``getattr`` so a plain spawn/poll/kill double
        skips cleanly). Unregistration always lands, in the ``finally``."""
        runner = self.runner
        registry = ctx.background_runner
        register = getattr(registry, "register_foreground", None)
        unregister = getattr(registry, "unregister_foreground", None)
        task_id = str(ctx.metadata.get("task_id", ""))
        handle_box: list[Any] = []
        if runner is None and callable(register) and callable(unregister) and task_id:

            def _on_spawn(proc: subprocess.Popen[bytes]) -> None:
                handle_box.append(
                    register(popen=proc, spawned_by_task_id=task_id)
                )

            runner = runner_with_spawn_hook(_on_spawn)
        interrupted = False
        try:
            outcome = self.exec_env.run_argv(
                exec_argv,
                cwd=self.workspace.root,
                timeout_s=timeout_s,
                output_cap=self.output_cap,
                runner=runner,
            )
        finally:
            if handle_box:
                assert unregister is not None  # guarded with ``register`` above
                interrupted = bool(unregister(handle_box[0]))
        return outcome, interrupted

    def _spawn_background(
        self, argv: list[str], command: str, ctx: ToolContext
    ) -> ToolResult:
        """Hand the validated argv to the host's background runner.

        The runner spawns detached and records ``BackgroundShellStarted`` on the
        launching task's stream, so the job handle can be returned immediately.
        A ``None`` runner means the host did not enable background execution,
        which must refuse rather than fall back to a blocking run."""
        runner = ctx.background_runner
        if runner is None:
            return _err(self.name, "background execution is not available on this host")
        spawned_by_task_id = str(ctx.metadata.get("task_id", ""))
        trace_id = str(ctx.metadata.get("trace_id", ""))
        spawned = runner.spawn(
            argv=argv,
            cwd=self.workspace.root,
            env=scrub_env(),
            command=command,
            spawned_by_task_id=spawned_by_task_id,
            trace_id=trace_id,
        )
        # A rejected spawn (per-session concurrency cap) is NOT queued, so the
        # reason must reach the model as a clean tool failure it can act on
        # ("kill one first") rather than a crash.
        if spawned.get("rejected"):
            return _err(self.name, str(spawned["reason"]))
        job_id = spawned["job_id"]
        summary_cmd = truncate_bytes(command, SUMMARY_EMBED_MAX_BYTES)
        return ToolResult(
            success=True,
            output=(
                f"Command running in background with ID: {job_id}\n"
                "Read its output with BashOutput; stop it with KillShell."
            ),
            summary=f"{self.name} {summary_cmd} → background ({job_id})",
        )


def _build_shell_result(
    tool_name: str,
    *,
    command: str,
    outcome: RunOutcome,
    ctx: ToolContext,
    timeout_s: int,
    interrupted: bool = False,
) -> ToolResult:
    # The FULL streams are the audit artifacts; only the capped rendering
    # rides inline, and no ref ever enters the model-facing text.
    artifacts = []
    if outcome.stdout:
        artifacts.append(
            ctx.artifact_store.put(outcome.stdout, media_type="text/plain")
        )
    if outcome.stderr:
        artifacts.append(
            ctx.artifact_store.put(outcome.stderr, media_type="text/plain")
        )
    body = _render_streams(outcome.stdout, outcome.stderr)
    summary_cmd = truncate_bytes(command, SUMMARY_EMBED_MAX_BYTES)
    if interrupted:
        # The session kill cascade reaped the group mid-run (user stop) —
        # checked BEFORE the timeout branch so a kill racing the deadline
        # still reads as the interrupt it was, never a generic timeout.
        return ToolResult(
            success=False,
            output=body,
            artifacts=artifacts,
            summary="Command interrupted (stop requested)",
        )
    if outcome.timed_out:
        return ToolResult(
            success=False,
            output=body,
            artifacts=artifacts,
            summary=(
                f"Command timed out after {timeout_s}s"
            ),
        )
    if outcome.returncode != 0:
        return ToolResult(
            success=False,
            output=body,
            artifacts=artifacts,
            summary=f"Exit code {outcome.returncode}",
        )
    return ToolResult(
        success=True,
        output=body if body else "(no output)",
        artifacts=artifacts,
        summary=f"{tool_name} {summary_cmd} → OK ({outcome.duration_ms}ms)",
    )


@dataclass
class ShellPollTool:
    """Pull the output a background job produced since the last check (tool
    name ``BashOutput``).

    Each call returns ONLY the new output (plus the job status), so repeated
    polling reads like tailing a log. The host runner still mints a
    content-addressed snapshot per poll and records ``BackgroundShellPolled``,
    so a resume reproduces exactly the prefix the model had seen.
    """

    name: str = "BashOutput"
    description: str = field(default=load_markdown(__package__, "shell_poll"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "bash_id": {"type": "string"},
                # optional per-line regex filter on the new output
                "filter": {"type": "string"},
            },
            "required": ["bash_id"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        runner = ctx.background_runner
        if runner is None:
            return _err(self.name, "background execution is not available on this host")
        job_id = require_str(
            arguments, "bash_id", lambda m: _err(self.name, m),
            message="requires a non-empty 'bash_id'",
        )
        if isinstance(job_id, ToolResult):
            return job_id
        line_filter = arguments.get("filter")
        if line_filter is not None:
            if not isinstance(line_filter, str):
                return _err(self.name, "'filter' must be a string")
            try:
                filter_re = re.compile(line_filter)
            except re.error as exc:
                return _err(self.name, f"invalid 'filter' regex: {exc}")
        else:
            filter_re = None
        state = runner.poll(job_id)
        if state.get("status") == "unknown":
            return _err(self.name, f"unknown background job {job_id!r}")

        status = state["status"]
        status_line = f"status: {status}"
        if status in ("exited", "killed") and state.get("exit_code") is not None:
            status_line += f" (exit code {state['exit_code']})"
        new_output = str(state.get("new_output") or "")
        if filter_re is not None and new_output:
            new_output = "\n".join(
                line for line in new_output.splitlines() if filter_re.search(line)
            )
        parts = [status_line]
        dropped = int(state.get("new_output_dropped") or 0)
        if dropped:
            parts.append(
                f"[{dropped} bytes of older output dropped (buffer overflow)]"
            )
        if new_output:
            parts.append(elide_middle(new_output, SHELL_INLINE_MAX_CHARS))
        else:
            parts.append("(no new output)")
        return ToolResult(
            success=True,
            output="\n".join(parts),
            summary=f"{self.name} {job_id} → {status}",
        )


@dataclass
class ShellKillTool:
    """Terminate a background shell job the model started (tool name
    ``KillShell``).

    Lets the agent stop a job it launched wrong — a server on the wrong port, a
    build it must restart — so it is never stuck waiting on a human. The call
    returns immediately; the host's watcher sends SIGTERM then SIGKILL after a
    grace, reaps the process, records ``BackgroundShellKilled`` and fires the
    same completion notice a background ``Bash`` exit does, so the model
    learns the job ended.
    """

    name: str = "KillShell"
    description: str = field(default=load_markdown(__package__, "shell_kill"))
    # High risk so PermissionGuard gates it exactly like ``Bash``: an
    # operator policy can deny it or require approval.
    risk_level: str = "high"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"shell_id": {"type": "string"}},
            "required": ["shell_id"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        runner = ctx.background_runner
        if runner is None:
            return _err(self.name, "background execution is not available on this host")
        job_id = require_str(
            arguments, "shell_id", lambda m: _err(self.name, m),
            message="requires a non-empty 'shell_id'",
        )
        if isinstance(job_id, ToolResult):
            return job_id
        result = runner.kill(job_id)
        if result.get("status") == "unknown":
            return _err(self.name, f"unknown background job {job_id!r}")
        return ToolResult(
            success=True,
            output=f"Shell {job_id} is being terminated ({result['status']})",
            summary=f"{self.name} {job_id} → {result['status']}",
        )
