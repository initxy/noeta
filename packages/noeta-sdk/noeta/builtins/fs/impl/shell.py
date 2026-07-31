"""``shell_run`` plus the two tools that manage the background jobs it starts.

The tier is fixed at construction: ``ALLOWLIST`` rejects shell metacharacters
before tokenisation and runs the structurally matched argv directly with no
shell, so nothing is ever executed as a substring of the model's string, while
``ARBITRARY`` is a real ``bash -c`` whose safety boundary is the host's
PermissionGuard plus approval predicate rather than an argv wall. Either way a
command gets ``cwd = workspace.root``, a bounded timeout, a scrubbed
environment, and an output cap that offloads the full streams to artifacts —
and nothing beyond that: the spawned process is **not** sandboxed and can write
files anywhere on the host, so ``shell_run`` suits a trusted workspace only.
"""

from __future__ import annotations

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
    _STDERR_TAIL_BYTES,
    _STDOUT_TAIL_BYTES,
)
from noeta.runtime.subproc import RunOutcome, tail_bytes
from noeta.runtime.workspace import WorkspaceRoot
from noeta.tools.invocation import require_str
from noeta.protocols.resources import load_markdown
from noeta.tools.limits import (
    INLINE_CONTENT_MAX_BYTES,
    SUMMARY_EMBED_MAX_BYTES,
    fit_output_fields,
    truncate_bytes,
)
from noeta.tools.refs import ref_json


__all__ = [
    "ShellKillTool",
    "ShellPollTool",
    "ShellRunTool",
]


def _err(name: str, message: str) -> ToolResult:
    return ToolResult(success=False, summary=f"{name}: {message}")


@dataclass
class ShellRunTool:
    """Shell runner with a strict (allowlist) and a full-bash (arbitrary) tier.

    Honest boundary: Noeta guarantees cwd = workspace, scrubbed env, bounded
    timeout, and output cap. It does **not** sandbox the spawned process —
    commands execute workspace code, which can do arbitrary local IO.
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
    name: str = "shell_run"
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
            return _err(self.name, "shell_run is disabled")
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
        outcome = self.exec_env.run_argv(
            exec_argv,
            cwd=self.workspace.root,
            timeout_s=timeout_s,
            output_cap=self.output_cap,
            runner=self.runner,
        )
        return _build_shell_result(
            self.name,
            command=command,
            outcome=outcome,
            ctx=ctx,
        )

    def _spawn_background(
        self, argv: list[str], command: str, ctx: ToolContext
    ) -> ToolResult:
        """Hand the validated argv to the host's background runner.

        The runner spawns detached and records ``BackgroundShellStarted`` on the
        launching task's stream, so the ``{job_id, ref}`` handle can be returned
        immediately. A ``None`` runner means the host did not enable background
        execution, which must refuse rather than fall back to a blocking run."""
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
        summary_cmd = truncate_bytes(command, SUMMARY_EMBED_MAX_BYTES)
        return ToolResult(
            success=True,
            output={
                "job_id": spawned["job_id"],
                "status": "running",
                "ref": spawned["ref"],
            },
            summary=f"{self.name} {summary_cmd} → background ({spawned['job_id']})",
        )


def _build_shell_result(
    tool_name: str,
    *,
    command: str,
    outcome: RunOutcome,
    ctx: ToolContext,
) -> ToolResult:
    stdout_tail, _ = tail_bytes(outcome.stdout, _STDOUT_TAIL_BYTES)
    stderr_tail, _ = tail_bytes(outcome.stderr, _STDERR_TAIL_BYTES)
    # ContentStore.put dedups on hash, so calling once + reusing the ref
    # keeps the artifact list and the `output.*_ref` JSON form in sync.
    stdout_ref_obj = (
        ctx.artifact_store.put(outcome.stdout, media_type="text/plain")
        if outcome.stdout
        else None
    )
    stderr_ref_obj = (
        ctx.artifact_store.put(outcome.stderr, media_type="text/plain")
        if outcome.stderr
        else None
    )
    output: dict[str, Any] = {
        "command": truncate_bytes(command, 512),
        "returncode": outcome.returncode,
        "duration_ms": outcome.duration_ms,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "stdout_truncated": outcome.stdout_truncated,
        "stderr_truncated": outcome.stderr_truncated,
        "timed_out": outcome.timed_out,
    }
    if stdout_ref_obj is not None:
        output["stdout_ref"] = ref_json(stdout_ref_obj)
    if stderr_ref_obj is not None:
        output["stderr_ref"] = ref_json(stderr_ref_obj)
    output = fit_output_fields(
        output,
        shrink_order=["stderr_tail", "stdout_tail", "command"],
        max_bytes=INLINE_CONTENT_MAX_BYTES,
    )
    summary_cmd = truncate_bytes(command, SUMMARY_EMBED_MAX_BYTES)
    status = "OK" if outcome.returncode == 0 else f"exit={outcome.returncode}"
    if outcome.timed_out:
        status = "timeout"
    artifacts = [
        ref for ref in (stdout_ref_obj, stderr_ref_obj) if ref is not None
    ]
    return ToolResult(
        success=True,
        output=output,
        artifacts=artifacts,
        summary=f"{tool_name} {summary_cmd} → {status} ({outcome.duration_ms}ms)",
    )


@dataclass
class ShellPollTool:
    """Pull the latest snapshot + status of a background job.

    Thin by design: it returns ``{status, ref, offset}``, never the bytes, so
    the model dereferences ``ref`` through the ordinary deref path instead of
    this becoming a second, fat cursor-read tool. The host runner mints a fresh
    content-addressed snapshot and records ``BackgroundShellPolled(ref,
    offset)``, so the model reads exactly the prefix it was shown.
    """

    name: str = "shell_poll"
    description: str = field(default=load_markdown(__package__, "shell_poll"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        runner = ctx.background_runner
        if runner is None:
            return _err(self.name, "background execution is not available on this host")
        job_id = require_str(
            arguments, "job_id", lambda m: _err(self.name, m),
            message="requires a non-empty 'job_id'",
        )
        if isinstance(job_id, ToolResult):
            return job_id
        state = runner.poll(job_id)
        if state.get("status") == "unknown":
            return _err(self.name, f"unknown background job {job_id!r}")
        output: dict[str, Any] = {
            "status": state["status"],
            "ref": state["ref"],
            "offset": state["offset"],
            # Tells the model the snapshot is only the tail: the buffer
            # overflowed ``output_cap`` and the oldest output was dropped.
            "truncated": bool(state.get("truncated")),
        }
        if "exit_code" in state:
            output["exit_code"] = state["exit_code"]
        return ToolResult(
            success=True,
            output=output,
            summary=f"{self.name} {job_id} → {state['status']}",
        )


@dataclass
class ShellKillTool:
    """Terminate a background shell job the model started.

    Lets the agent stop a job it launched wrong — a server on the wrong port, a
    build it must restart — so it is never stuck waiting on a human. The call
    returns immediately; the host's watcher sends SIGTERM then SIGKILL after a
    grace, reaps the process, records ``BackgroundShellKilled`` and fires the
    same completion notice a background ``shell_run`` exit does, so the model
    learns the job ended.
    """

    name: str = "shell_kill"
    description: str = field(default=load_markdown(__package__, "shell_kill"))
    # High risk so PermissionGuard gates it exactly like ``shell_run``: an
    # operator policy can deny it or require approval.
    risk_level: str = "high"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        runner = ctx.background_runner
        if runner is None:
            return _err(self.name, "background execution is not available on this host")
        job_id = require_str(
            arguments, "job_id", lambda m: _err(self.name, m),
            message="requires a non-empty 'job_id'",
        )
        if isinstance(job_id, ToolResult):
            return job_id
        result = runner.kill(job_id)
        if result.get("status") == "unknown":
            return _err(self.name, f"unknown background job {job_id!r}")
        return ToolResult(
            success=True,
            output={"job_id": job_id, "status": result["status"]},
            summary=f"{self.name} {job_id} → {result['status']}",
        )


