"""Phase 4 I5 — shell runner + git convenience tools.

The PRD-D2 / B4 / B19 contract, as amended (Claude-Code Bash
alignment):

* ``shell_run`` has **two tiers**, picked by construction ``mode``:
  - :attr:`ShellMode.ALLOWLIST` — the strict, untrusted-default tier
    (daemon, CLI default). Shell metacharacters are rejected before
    tokenisation; the parsed argv is matched *structurally* against a
    small allowlist and run **directly, with no shell** — never as a
    substring of the original string.
  - :attr:`ShellMode.ARBITRARY` — a **real bash**. The raw
    ``command`` runs through ``bash -c`` so pipes, redirection, and
    chaining work, exactly like Claude Code's Bash tool. Safety here is
    *not* an argv wall — it is the host's PermissionGuard + the
    approval predicate (allowlisted commands run silently, anything else
    asks for a one-time human sign-off). The SDK-host product path forces
    this tier, so product agents get full bash gated by approval.
* Every command runs with ``cwd = workspace.root``, a bounded timeout
  (per-call ``timeout`` ms, ceiling 600000), a scrubbed environment (no
  secrets), and an output cap that offloads the full streams to
  ContentStore artifacts. These guards are the only things Noeta itself
  promises (B19); the spawned process is **not** sandboxed and can write
  files anywhere on the host, so ``shell_run`` is **only appropriate for a
  trusted workspace**.
* ``risk_level = "high"`` so ``PermissionGuard`` treats it as
  privileged. The CLI ``--allow-shell`` flag (I4) flips the closure
  into arbitrary mode by setting ``allow_arbitrary=True`` at
  construction; the daemon-default Agent does **not** enable it (I6).

``git_status`` / ``git_diff`` are thin convenience tools that funnel
through the same allowlist + guards, so an agent can inspect its own
changes with a structured output (and the SPA can render the diff
artifact via the I6 endpoint).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime._env import scrub_env
from noeta.runtime.exec_env import ExecEnv, LocalExecEnv
from noeta.runtime.shell_policy import (
    DEFAULT_SHELL_OUTPUT_CAP,
    DEFAULT_SHELL_TIMEOUT_S,
    ShellMode,
    _AllowRule,
    _DEFAULT_RULES,
    _has_shell_meta,
    _matches_allowlist,
    _parse_argv,
    _resolve_timeout,
    _SHELL_META_CHARS,
    _STDERR_TAIL_BYTES,
    _STDOUT_TAIL_BYTES,
)
from noeta.runtime.subproc import _RunOutcome, tail_bytes
from noeta.runtime.workspace import WorkspaceRoot
from noeta.tools._invocation import require_str
from noeta.tools.descriptions import load_tool_description
from noeta.tools._limits import (
    INLINE_CONTENT_MAX_BYTES,
    SUMMARY_EMBED_MAX_BYTES,
    fit_output_fields,
    truncate_bytes,
)
from noeta.tools._refs import ref_json


__all__ = [
    "ShellKillTool",
    "ShellPollTool",
    "ShellRunTool",
]


def _err(name: str, message: str) -> ToolResult:
    return ToolResult(success=False, summary=f"{name}: {message}")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@dataclass
class ShellRunTool:
    """Shell runner with a strict (allowlist) and a full-bash (arbitrary) tier.

    Construction-time ``mode`` decides the tier: :attr:`ALLOWLIST`
    rejects shell metacharacters and runs the parsed argv directly with no
    shell; :attr:`ARBITRARY` runs the raw command through ``bash -c`` (pipes,
    redirection, chaining), with the host's PermissionGuard + approval
    predicate as the safety boundary. The daemon default Agent uses
    :attr:`ShellMode.OFF` (the tool is simply absent from the pack) — see
    :func:`build_fs_tools`.

    Honest boundary (B19): Noeta guarantees cwd = workspace, scrubbed env,
    bounded timeout, and output cap. It does **not** sandbox the spawned
    process — commands execute workspace code, which can do arbitrary local IO.
    Trusted-workspace use only.
    """

    workspace: WorkspaceRoot
    mode: ShellMode = ShellMode.ALLOWLIST
    timeout_s: int = DEFAULT_SHELL_TIMEOUT_S
    output_cap: int = DEFAULT_SHELL_OUTPUT_CAP
    rules: tuple[_AllowRule, ...] = field(default_factory=lambda: _DEFAULT_RULES)
    runner: Optional[Callable[..., subprocess.CompletedProcess[bytes]]] = None
    #: execution backend the foreground command runs through — the local
    #: host (default) or a sandbox container. Background spawns still go
    #: through the host ``background_runner`` (sandbox background is v2).
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "shell_run"
    # description lives in an independent text resource
    # (descriptions/shell_run.md, four-section shape), not a Python string.
    description: str = field(default=load_tool_description("shell_run"))
    # PRD D2: high-risk so PermissionGuard treats this as privileged.
    risk_level: str = "high"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                # per-call wall-clock cap in milliseconds (ceiling
                # 600000); aligns the schema with Claude Code's Bash.
                "timeout": {"type": "number"},
                # optional human-readable description of the command
                # (UI hint; does not affect execution).
                "description": {"type": "string"},
                # launch detached instead of blocking the engine
                # main loop on the (possibly long-running) process. Renamed from
                # ``background`` to match Claude Code's ``run_in_background``.
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
        # ALLOWLIST stays the strict argv-only tier (reject meta-
        # characters, match the parsed argv against the allowlist, run argv with
        # no shell). ARBITRARY is a real bash — the raw command runs through
        # ``bash -c`` so pipes / redirection / chaining work; safety is the
        # PermissionGuard + approval predicate, not an argv wall.
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
        # background launch — REUSE the mode gate above, then hand off
        # to the host's runner and return immediately. The sync timeout does NOT
        # apply to a backgrounded process.
        if bool(arguments.get("run_in_background")):
            # A sandbox backend cannot run host-side background jobs (the runner
            # spawns HOST subprocesses; AIO has no durable job handle, v1) — so
            # refuse cleanly instead of silently running on the wrong machine.
            # ``getattr`` default True keeps every local / pre-seam backend on
            # the existing path. (D5)
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

        The runner spawns detached and records ``BackgroundShellStarted`` on
        the launching task's stream; we return the ``{job_id, ref}`` handle
        immediately. ``None`` runner ⇒ the host did not enable background
        execution → refuse cleanly (no spawn)."""
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
        # the host rejected the spawn over the per-session
        # concurrency cap (it did NOT queue): surface the reason as a clean tool
        # failure the model can act on ("kill one first"), not a crash.
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
    outcome: _RunOutcome,
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
    # Inline budget — drop stderr_tail first, then stdout_tail, then
    # command echo, until under the canonical-encoded ceiling.
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

    Thin by design: it returns ``{status, ref, offset}`` (plus
    ``exit_code`` once exited), NOT the bytes — the model reads the output by
    dereferencing ``ref`` with the existing deref path, so there is no fat
    cursor-read tool. The host runner mints a fresh content-addressed snapshot
    and records ``BackgroundShellPolled(ref, offset)`` so the model reads
    exactly the prefix it saw. ``risk_level="low"`` — reading status is
    harmless.
    """

    name: str = "shell_poll"
    # description lives in an independent text resource
    # (descriptions/shell_poll.md, four-section shape), not a Python string.
    description: str = field(default=load_tool_description("shell_poll"))
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
            # tell the model the snapshot is the tail when
            # the buffer overflowed output_cap (oldest output dropped).
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

    The agent self-kills a job it launched wrong / no longer needs (a server it
    started on the wrong port, a build it must restart) so it is never stuck
    waiting on the human. Sends SIGTERM, then SIGKILL after a grace, via the
    host's background runner; the call returns immediately (the watcher reaps
    the process and records ``BackgroundShellKilled`` + fires the same
    completion notice ``shell_run(background)`` exits use — issue 02's push, so
    the model is told the job ended).
    ``risk_level="high"`` so :class:`PermissionGuard` gates it exactly like
    ``shell_run`` (an operator policy can deny / require approval for it).
    """

    name: str = "shell_kill"
    # description lives in an independent text resource
    # (descriptions/shell_kill.md, four-section shape), not a Python string.
    description: str = field(default=load_tool_description("shell_kill"))
    # high-risk so PermissionGuard treats it as privileged (an
    # operator policy can deny / gate it, same as shell_run).
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


