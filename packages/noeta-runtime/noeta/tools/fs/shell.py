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

import json
import shlex
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.tools._invocation import require_str
from noeta.tools._env import scrub_env
from noeta.tools.descriptions import load_tool_description
from noeta.tools._limits import (
    INLINE_CONTENT_MAX_BYTES,
    SUMMARY_EMBED_MAX_BYTES,
    fit_output_fields,
    truncate_bytes,
)
from noeta.tools._refs import ref_json
from noeta.tools.fs._subprocess import _RunOutcome, tail_bytes
from noeta.tools.fs._workspace import WorkspaceRoot
from noeta.tools.fs.exec_env import ExecEnv, LocalExecEnv


__all__ = [
    "ShellKillTool",
    "ShellMode",
    "ShellPollTool",
    "ShellRunTool",
    "DEFAULT_SHELL_TIMEOUT_S",
    "MAX_SHELL_TIMEOUT_MS",
    "DEFAULT_SHELL_OUTPUT_CAP",
]


#: Per-command wall-clock cap (seconds) when the call passes no ``timeout``.
DEFAULT_SHELL_TIMEOUT_S = 120

#: ceiling for the per-call ``timeout`` argument (milliseconds),
#: mirroring Claude Code's Bash (max 600000ms / 10 min).
MAX_SHELL_TIMEOUT_MS = 600_000

#: Cap on captured stdout/stderr **bytes per stream**. Output past this
#: is dropped from the artifact too — the tool returns ``truncated=True``
#: so the agent knows to narrow its command (e.g. ``pytest -q``).
DEFAULT_SHELL_OUTPUT_CAP = 256 * 1024  # 256 KB

#: Bytes of stdout / stderr to embed inline in the ``ToolResult.output``.
#: The agent gets the **tail** (test failures land at the end of
#: pytest output); the full stream is the artifact.
_STDOUT_TAIL_BYTES = 2048
_STDERR_TAIL_BYTES = 1024

_SHELL_META_CHARS = frozenset(";&|<>`$()\n\r")


class ShellMode(str, Enum):
    """Pre-run shell policy bound at ``FsToolPack`` construction (I4 maps
    CLI flags to this — ``--allow-shell`` ⇒ :attr:`ARBITRARY`).

    * :attr:`OFF` — ``shell_run`` is not in the pack at all.
    * :attr:`ALLOWLIST` — only the structural allowlist is permitted.
    * :attr:`ARBITRARY` — any non-shell-metachar command runs (high-risk,
      not for the daemon default Agent).
    """

    OFF = "off"
    ALLOWLIST = "allowlist"
    ARBITRARY = "arbitrary"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


_ArgValidator = Callable[[list[str]], bool]


@dataclass(frozen=True)
class _AllowRule:
    """One structural allowlist entry.

    ``program`` matches ``argv[0]`` exactly; ``subcommand`` (if set)
    matches ``argv[1]`` exactly. ``validate`` then inspects the tail
    args. Matching is on the parsed argv only — never on the raw string.
    """

    program: str
    subcommand: Optional[str]
    validate: _ArgValidator
    label: str

    def matches(self, argv: list[str]) -> bool:
        if not argv or argv[0] != self.program:
            return False
        tail = argv[1:]
        if self.subcommand is not None:
            if not tail or tail[0] != self.subcommand:
                return False
            tail = tail[1:]
        return self.validate(tail)


def _is_safe_path_arg(arg: str) -> bool:
    """A path-shaped arg has no shell metas and does not start with `-`.

    (Top-level metachar scan already caught most cases; this is a second
    line of defense for paths that might contain spaces or quotes.)
    """
    if not arg:
        return False
    if arg.startswith("-"):
        return False
    return not any(c in _SHELL_META_CHARS for c in arg)


def _git_status_validate(tail: list[str]) -> bool:
    return tail in ([], ["--short"], ["-s"], ["--porcelain"])


def _git_diff_validate(tail: list[str]) -> bool:
    # Allowed shapes: `git diff`, `git diff <path>`, `git diff -- <path>`,
    # `git diff --stat`, `git diff <path1> <path2>` (still all paths /
    # the path separator).
    allowed_flags = {"--stat", "--name-only", "--"}
    for arg in tail:
        if arg in allowed_flags:
            continue
        if arg.startswith("-"):
            return False
        if not _is_safe_path_arg(arg):
            return False
    return True


def _pytest_validate(tail: list[str]) -> bool:
    # pytest takes arbitrary args; the shell-meta scan already
    # disallowed the dangerous tokens. Reject only obvious red flags
    # (``--pdb`` lands you in an interactive prompt, which would hang).
    forbidden = {"--pdb", "--pdb-trace"}
    return all(a not in forbidden for a in tail)


def _uv_run_pytest_validate(tail: list[str]) -> bool:
    # tail starts AFTER ["uv", "run"]. First element must be `pytest`,
    # rest is pytest-tail-shaped.
    if not tail or tail[0] != "pytest":
        return False
    return _pytest_validate(tail[1:])


def _trivial_validate(_: list[str]) -> bool:
    return True


def _grep_validate(_: list[str]) -> bool:
    # grep cannot execute a command or write a file; the top-level
    # metachar scan already blocks `; & | < > $` injection. Any flag /
    # pattern / path shape is safe to search with.
    return True


def _rg_validate(tail: list[str]) -> bool:
    # ripgrep is read-only EXCEPT a few flags that shell out to an
    # external program per file. Reject those so `rg` stays a pure search.
    for arg in tail:
        if arg == "--hostname-bin":
            return False
        if arg == "--pre" or arg.startswith("--pre="):
            return False
        if arg == "--pre-glob" or arg.startswith("--pre-glob="):
            return False
    return True


def _find_validate(tail: list[str]) -> bool:
    # find can run commands (-exec/-execdir/-ok/-okdir), delete files
    # (-delete), or write files (-fprint*/-fls). Reject all of those so
    # find stays pure traversal/matching.
    forbidden = {
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        "-fprint",
        "-fprintf",
        "-fprint0",
        "-fls",
    }
    return all(a not in forbidden for a in tail)


_DEFAULT_RULES: tuple[_AllowRule, ...] = (
    _AllowRule("git", "status", _git_status_validate, "git_status"),
    _AllowRule("git", "diff", _git_diff_validate, "git_diff"),
    _AllowRule("pytest", None, _pytest_validate, "pytest"),
    _AllowRule("uv", "run", _uv_run_pytest_validate, "uv_run_pytest"),
    _AllowRule("npm", "test", _trivial_validate, "npm_test"),
    _AllowRule("pnpm", "test", _trivial_validate, "pnpm_test"),
    # read-only search / listing so an ALLOWLIST-mode agent —
    # notably general-purpose, which has no grep/glob tool of its own —
    # can still search the workspace via shell. All four are read-only;
    # the validators reject the handful of flags that shell out to an
    # external program or mutate the filesystem.
    _AllowRule("grep", None, _grep_validate, "grep"),
    _AllowRule("rg", None, _rg_validate, "rg"),
    _AllowRule("find", None, _find_validate, "find"),
    _AllowRule("ls", None, _trivial_validate, "ls"),
)


def _rule_from_spec(spec: Mapping[str, Any]) -> _AllowRule:
    """Convert one JSON-serializable allowlist spec → an :class:`_AllowRule`.

    Spec shape (JSON-serializable, config-friendly)::

        {"program": "npm", "subcommand": "start"}   # subcommand optional

    Operator-configured rules use :func:`_trivial_validate`: any tail args are
    accepted *provided* the top-level shell-metachar scan passed (``; & | < > $``
    etc. are rejected before validation runs). This is deliberately looser than
    the curated built-in validators (which pin exact safe flag shapes) — a config
    rule means "this program/subcommand may run", not "with exactly these args".
    """
    program = spec.get("program")
    if not isinstance(program, str) or not program:
        raise ValueError(
            f"shell allowlist rule needs a non-empty string 'program': {spec!r}"
        )
    subcommand = spec.get("subcommand")
    if subcommand is not None and not isinstance(subcommand, str):
        raise ValueError(
            f"shell allowlist rule 'subcommand' must be a string or absent: {spec!r}"
        )
    label = spec.get("label")
    if not isinstance(label, str) or not label:
        label = program if subcommand is None else f"{program}_{subcommand}"
    return _AllowRule(program, subcommand, _trivial_validate, label)


def build_allowlist(
    extra_specs: Sequence[Mapping[str, Any]] = (),
) -> tuple[_AllowRule, ...]:
    """Curated safe defaults + operator-configured extra rules (extend).

    :data:`_DEFAULT_RULES` (git status/diff, pytest, uv run pytest, npm/pnpm
    test, and the read-only search/listing commands grep/rg/find/ls) are
    always kept; ``extra_specs`` from host config are appended. An empty
    ``extra_specs`` returns exactly the defaults.
    """
    return _DEFAULT_RULES + tuple(_rule_from_spec(s) for s in extra_specs)


def command_in_allowlist(command: str, rules: Sequence["_AllowRule"]) -> bool:
    """True iff ``command`` is a well-formed, metachar-free argv matching a rule.

    Shared by the tool's own ALLOWLIST gate and the SDK-host approval predicate
    (which asks "does this need human sign-off?" = ``not command_in_allowlist``).
    A command with shell metacharacters or unbalanced quotes is never considered
    allowlisted (the tool rejects metas regardless of mode).
    """
    if _has_shell_meta(command):
        return False
    argv = _parse_argv(command)
    if not argv:
        return False
    return _matches_allowlist(argv, tuple(rules))


def rule_spec_from_command(command: str) -> Optional[dict[str, str]]:
    """Derive a persist-able allowlist spec from an approved command.

    Granularity is program + first arg: ``npm start`` -> ``{"program": "npm",
    "subcommand": "start"}``; a bare ``ls`` -> ``{"program": "ls"}``. Returns
    ``None`` for a command with metachars / unbalanced quotes (nothing safe to
    remember). The resulting rule accepts any tail args (``_trivial_validate``).
    """
    if _has_shell_meta(command):
        return None
    argv = _parse_argv(command)
    if not argv:
        return None
    spec: dict[str, str] = {"program": argv[0]}
    if len(argv) >= 2:
        spec["subcommand"] = argv[1]
    return spec


def project_shell_allowlist_path(workspace_root: Path) -> Path:
    """Per-project remembered-rules file: ``<workspace>/.noeta/shell-allowlist.json``."""
    return Path(workspace_root) / ".noeta" / "shell-allowlist.json"


def load_project_shell_allowlist(
    workspace_root: Path, *, exec_env: Optional[ExecEnv] = None
) -> tuple[dict[str, Any], ...]:
    """Load the project's remembered allowlist specs (empty if absent/malformed).

    Plain external config read - it never enters the LLM context or the event
    log; it just feeds the effective allowlist when the tools are built for a turn.

    ``exec_env`` (sandbox mode) reads the allowlist file THROUGH the container —
    ``workspace_root`` is then the container workdir, so the rules come from the
    file INSIDE the sandbox (this fixes the v1 bug where the loader read a
    container path against the host filesystem). ``None`` keeps the host read
    byte-identical.
    """
    path = project_shell_allowlist_path(workspace_root)
    try:
        if exec_env is not None:
            raw = json.loads(exec_env.read_text(path, encoding="utf-8"))
        else:
            raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ()
    if not isinstance(raw, list):
        return ()
    out: list[dict[str, Any]] = []
    for item in raw:
        if (
            isinstance(item, Mapping)
            and isinstance(item.get("program"), str)
            and item["program"]
        ):
            out.append(dict(item))
    return tuple(out)


def append_project_shell_rule(
    workspace_root: Path, spec: Mapping[str, Any]
) -> bool:
    """Append ``spec`` to the project allowlist file, deduped by (program, subcommand).

    Returns True if newly added, False if already present. Creates ``.noeta/`` as
    needed. Pure external side-effect (see :func:`load_project_shell_allowlist`).
    """
    path = project_shell_allowlist_path(workspace_root)
    existing = list(load_project_shell_allowlist(workspace_root))
    key = (spec.get("program"), spec.get("subcommand"))
    for entry in existing:
        if (entry.get("program"), entry.get("subcommand")) == key:
            return False
    existing.append(dict(spec))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return True


def _has_shell_meta(command: str) -> bool:
    return any(c in _SHELL_META_CHARS for c in command)


def _resolve_timeout(raw: Any, default_s: int) -> int:
    """Per-call ``timeout`` (milliseconds) → seconds, clamped to
    :data:`MAX_SHELL_TIMEOUT_MS`. Absent / non-positive / non-numeric falls
    back to the construction default. ``bool`` is excluded (it is an ``int``
    subclass but never a meaningful timeout)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return default_s
    ms = min(int(raw), MAX_SHELL_TIMEOUT_MS)
    return max(1, ms // 1000)


def _parse_argv(command: str) -> Optional[list[str]]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def _matches_allowlist(argv: list[str], rules: tuple[_AllowRule, ...]) -> bool:
    return any(rule.matches(argv) for rule in rules)


# ---------------------------------------------------------------------------
# Process execution + output handling
# ---------------------------------------------------------------------------
#
# The run/capture/cap primitives (`run_argv` / `tail_bytes` / `cap_stream`
# / ``_RunOutcome``) live in ``noeta.tools.fs._subprocess`` so ``shell_run``
# and ``run_skill_script`` share the exact timeout / truncation boundary.


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
            argv=exec_argv,
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
    argv: list[str],
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


