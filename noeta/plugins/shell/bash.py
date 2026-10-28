"""``Bash``: run a command through ``bash -c`` in the foreground.

Every command gets a bounded timeout, a scrubbed environment (the env
backend's job), a per-stream capture cap that keeps the tail, and the
directory the previous command left the shell in — tracked with an EXIT trap
and recorded as ``shell`` / ``cwd``. A command that leaves the workspace
sends the next one back to the root. Past 30,000 characters the output is
saved under ``ctx.spill_dir`` with a preview inline, or cut to its start and
end when there is no spill directory. The process is **not** sandboxed:
trusted workspaces only.

Background commands (``run_in_background``) need the ``Shell`` extension's
background jobs; without them this tool refuses them.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from noeta.core.execenv import RunResult
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.toolkit.shell import (
    DEFAULT_SHELL_RULES,
    AllowRule,
    long_leading_sleep,
    matching_rule,
    parse_argv,
)
from noeta.toolkit.tools import (
    Workspace,
    error_output,
    limit_output,
    load_description,
    path_within,
    tool_error,
)

__all__ = [
    "DEFAULT_SHELL_OUTPUT_CAP",
    "DEFAULT_SHELL_TIMEOUT_S",
    "MAX_SHELL_TIMEOUT_MS",
    "SHELL",
    "BashTool",
    "ShellEnv",
    "ShellState",
    "reduce_shell",
    "shell_state",
]

#: The extension name the shell records under (``cwd``, and the background
#: job kinds).
SHELL = "shell"


@dataclass(frozen=True)
class ShellState:
    #: Where the next ``Bash`` command starts; ``None`` = the workspace root.
    cwd: str | None = None
    #: Background job id -> what the log says about it.
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)


def reduce_shell(state: ShellState | None, kind: str, data: Any) -> ShellState:
    """``state`` with one recorded entry applied. Never mutates ``state``."""
    base = state if isinstance(state, ShellState) else ShellState()
    if not isinstance(data, Mapping):
        return base
    if kind == "cwd":
        cwd = data.get("cwd")
        return base if cwd is not None and not isinstance(cwd, str) \
            else replace(base, cwd=cwd or None)
    jobs = dict(base.jobs)
    job_id = data.get("job_id")
    if not isinstance(job_id, str):
        return base
    if kind == "started":
        jobs[job_id] = {**data, "status": "running"}
    elif kind in ("ended", "lost") and job_id in jobs:
        job = dict(jobs[job_id])
        job["status"] = data.get("status") or "lost"
        if data.get("exit_code") is not None:
            job["exit_code"] = data["exit_code"]
        jobs[job_id] = job
    else:
        return base
    return replace(base, jobs=jobs)


def shell_state(state: Mapping[str, Any]) -> ShellState:
    """The ``shell`` entry of a tool context's folded state."""
    raw = state.get(SHELL)
    return raw if isinstance(raw, ShellState) else ShellState()

DEFAULT_SHELL_TIMEOUT_S = 120
#: Ceiling for the per-call ``timeout`` argument (milliseconds): ten minutes.
MAX_SHELL_TIMEOUT_MS = 600_000
#: Captured bytes kept per stream (the tail survives).
DEFAULT_SHELL_OUTPUT_CAP = 256 * 1024
#: Claude Code's ``maxResultSizeChars`` for Bash.
SHELL_OUTPUT_LIMIT = 30_000

#: ``ctx -> variables`` a host adds to this call's command on top of the
#: scrubbed environment. Asked per command; never recorded.
ShellEnv = Callable[[ToolContext], Mapping[str, str]]

#: What a host variable name must look like: a POSIX shell name.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Line a tracked command prints last, carrying ``pwd -P``; stripped before
#: anything is shown.
_CWD_MARK = "___NOETA_SHELL_CWD___"


def resolve_timeout(raw: Any, default_s: int) -> int:
    """Per-call ``timeout`` (milliseconds) -> seconds, clamped to
    :data:`MAX_SHELL_TIMEOUT_MS`. Absent, non-positive, non-numeric or
    ``bool`` falls back to ``default_s``."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return default_s
    ms = min(int(raw), MAX_SHELL_TIMEOUT_MS)
    return max(1, ms // 1000)


def _blocked_sleep(described: str) -> str:
    """Claude Code's refusal of a long leading ``sleep``."""
    return (
        f"Blocked: {described}. To wait for a condition, use Monitor with an "
        "until-loop (e.g. `until <check>; do sleep 2; done`). To wait for a "
        "command you started, use run_in_background: true. Do not chain "
        "shorter sleeps to work around this block."
    )


def _track_cwd(command: str) -> str:
    """``command`` with an EXIT trap printing the shell's final directory, so
    it is reported however the command ends short of a kill (``exit N``
    included) and the exit status is untouched."""
    return f"trap 'printf \"\\n%s%s\\n\" \"{_CWD_MARK}\" \"$(pwd -P)\"' EXIT\n{command}"


def _split_cwd(stream: bytes) -> tuple[bytes, str | None]:
    """``stream`` without the trap's line, and the directory it named."""
    mark = ("\n" + _CWD_MARK).encode("utf-8")
    at = stream.rfind(mark)
    if at < 0:
        return stream, None
    start = at + len(mark)
    end = stream.find(b"\n", start)
    path = stream[start : end if end >= 0 else len(stream)].decode("utf-8", errors="replace")
    rest = stream[:at] + (stream[end + 1 :] if end >= 0 else b"")
    return rest, path or None


def _render_streams(stdout: bytes, stderr: bytes) -> str:
    """stdout, then stderr under a label only when both carry content."""
    out_text = stdout.decode("utf-8", errors="replace").rstrip("\n")
    err_text = stderr.decode("utf-8", errors="replace").rstrip("\n")
    if out_text and err_text:
        return f"{out_text}\nstderr:\n{err_text}"
    return out_text or err_text


def _capture_notice(result: RunResult, output_cap: int) -> str:
    """Name a stream whose HEAD the capture cap dropped, or ``""``."""
    streams = [
        name
        for name, dropped in (
            ("stdout", result.stdout_truncated),
            ("stderr", result.stderr_truncated),
        )
        if dropped
    ]
    if not streams:
        return ""
    size = f"{output_cap // 1024} KB" if output_cap >= 1024 else f"{output_cap} bytes"
    return (
        f"[earlier output dropped: only the last {size} of "
        f"{' and '.join(streams)} was captured]"
    )



def _input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            # per-call wall-clock cap in milliseconds (ceiling 600000)
            "timeout": {"type": "number"},
            # UI hint only; does not affect execution
            "description": {"type": "string"},
            "run_in_background": {"type": "boolean"},
        },
        "required": ["command"],
        "additionalProperties": False,
    }


class BashTool:
    """``Bash`` (foreground). ``shell_env`` adds per-call variables;
    ``hardening_rules`` are the rules whose ``harden`` rewrite applies to a
    matching command (the curated ``git`` flags by default)."""

    name = "Bash"
    description = load_description("noeta.plugins.shell", "shell_run")
    read_only = False
    clearable = True
    input_schema: dict[str, Any]

    def __init__(
        self,
        *,
        timeout_s: int = DEFAULT_SHELL_TIMEOUT_S,
        output_cap: int = DEFAULT_SHELL_OUTPUT_CAP,
        shell_env: ShellEnv | None = None,
        hardening_rules: tuple[AllowRule, ...] = DEFAULT_SHELL_RULES,
    ) -> None:
        self.input_schema = _input_schema()
        self.timeout_s = timeout_s
        self.output_cap = output_cap
        self.shell_env = shell_env
        self.hardening_rules = hardening_rules

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        command = input.get("command")
        if not isinstance(command, str) or not command.strip():
            return tool_error(self.name, "requires non-empty 'command'")
        if input.get("run_in_background"):
            return tool_error(self.name, "background execution is not available on this host")
        described = long_leading_sleep(command)
        if described is not None:
            return tool_error(self.name, _blocked_sleep(described))
        timeout_s = resolve_timeout(input.get("timeout"), self.timeout_s)

        env = ctx.env
        ws = await Workspace.of(env)
        cwd, moved_back = await self._start_cwd(ws, ctx)
        # A command matching a hardening rule runs as that rule's rewritten
        # argv: ``matching_rule`` admits it only when the ``shlex`` argv is
        # exactly what bash would run, so nothing but the injected flags
        # changes. Such a command cannot change directory, so it is not
        # tracked.
        rule = matching_rule(command, self.hardening_rules)
        argv = parse_argv(command) if rule is not None and rule.harden is not None else None
        track = argv is None
        if rule is not None and rule.harden is not None and argv:
            exec_argv = rule.harden(argv)
        else:
            exec_argv = ["bash", "-c", _track_cwd(command)]

        result = await env.run(
            exec_argv,
            cwd=cwd,
            env=self._command_env(ctx),
            timeout=float(timeout_s),
            output_cap=self.output_cap,
        )
        stdout, stderr = result.stdout, result.stderr
        final = cwd
        if track:
            stdout, reported = _split_cwd(stdout)
            if reported is None:  # the command sent its stdout to stderr
                stderr, reported = _split_cwd(stderr)
            if reported is not None:
                if path_within(reported, ws.root):
                    final = reported
                else:
                    final, moved_back = ws.root, True

        body = _render_streams(stdout, stderr)
        notice = _capture_notice(result, self.output_cap)
        if notice:
            body = f"{notice}\n{body}" if body else notice
        meta: dict[str, Any] = {"exit_code": result.exit_code}
        if result.timed_out:
            meta["timed_out"] = True
        if not result.timed_out and result.exit_code == 0 and not body:
            body = "(no output)"
        if moved_back:
            # Claude Code's note when a command left the project.
            note = f"Shell cwd was reset to {ws.root}"
            kept = body.strip() if body != "(no output)" else ""
            body = f"{kept}\n{note}" if kept else note
        if final != cwd or moved_back:
            ctx.record(SHELL, "cwd", {"cwd": final})
            meta["cwd"] = final
        body = await limit_output(body, SHELL_OUTPUT_LIMIT, ctx)
        if result.timed_out:
            return error_output(f"Command timed out after {timeout_s}s", body, **meta)
        if result.exit_code != 0:
            return error_output(f"Exit code {result.exit_code}", body, **meta)
        return text_output(body, **meta)

    async def _start_cwd(self, ws: Workspace, ctx: ToolContext) -> tuple[str, bool]:
        """Where this command starts, and whether the recorded directory had
        to be given up for the root: the last directory a command left the
        shell in, unless it is outside the workspace or no longer a
        directory."""
        raw = shell_state(ctx.state).cwd
        if not raw or raw == ws.root:
            return ws.root, False
        cwd = posixpath.normpath(raw)
        try:
            st = await ws.env.stat(cwd)
            usable = path_within(cwd, ws.root) and st is not None and st.is_dir
        except OSError:
            usable = False
        return (cwd, False) if usable else (ws.root, True)

    def _command_env(self, ctx: ToolContext) -> dict[str, str]:
        """The host's per-call variables. Fails soft — a resolver that raises
        or a malformed entry only costs the command that variable."""
        if self.shell_env is None:
            return {}
        try:
            raw = self.shell_env(ctx)
        except Exception:  # noqa: BLE001 - a broken resolver must not fail the command
            return {}
        if not isinstance(raw, Mapping):
            return {}
        return {
            name: value
            for name, value in raw.items()
            if isinstance(name, str)
            and isinstance(value, str)
            and _ENV_NAME.fullmatch(name)
            and "\x00" not in value
        }
