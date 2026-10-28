"""``Bash``: run a command through ``bash -c`` in the foreground.

Every command gets a bounded timeout, a scrubbed environment (the env
backend's job), stdout and stderr merged in the order printed, a capture cap
that keeps the tail, and — in a root task, after a command that succeeds —
the directory it left the shell in, tracked with an EXIT trap and recorded as
``shell`` / ``cwd``. A command that ends outside the workspace sends the next
one back to the root. Past 30,000 characters the output is
saved under ``ctx.spill_dir`` with a preview inline, or cut to its start and
end when there is no spill directory. When the env carries a sandbox
(``noeta.toolkit.sandbox``) a command runs confined unless the settings
exclude it or the call leaves it (``dangerouslyDisableSandbox``, which the
permissions plugin asks about); the hosts the sandbox refused follow the
output in a ``<sandbox_violations>`` block. Without one the process can do
anything the host user can: trusted workspaces only.

Background commands (``run_in_background``) need the ``Shell`` extension's
background jobs; without them this tool refuses them.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from noeta.core.execenv import RunResult
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.shell.hardening import DEFAULT_SHELL_RULES, HardeningRule, hardened_argv
from noeta.toolkit.sandbox import (
    CommandSandbox,
    PreparedCommand,
    belongs_in_sandbox,
    sandbox_of,
    violations_block,
)
from noeta.toolkit.shell import long_leading_sleep
from noeta.toolkit.shell_syntax import parse
from noeta.toolkit.tools import SCHEMA_DIALECT as SCHEMA_DIALECT
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
    "NO_OUTPUT",
    "SCHEMA_DIALECT",
    "SHELL",
    "BashTool",
    "ShellEnv",
    "ShellState",
    "git_prelude",
    "guard_root",
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
    #: The worktree ``EnterWorktree`` moved the shell into (``worktree.py``).
    worktree: dict[str, Any] | None = None


def reduce_shell(state: ShellState | None, kind: str, data: Any) -> ShellState:
    """``state`` with one recorded entry applied. Never mutates ``state``."""
    base = state if isinstance(state, ShellState) else ShellState()
    if not isinstance(data, Mapping):
        return base
    if kind == "cwd":
        cwd = data.get("cwd")
        return base if cwd is not None and not isinstance(cwd, str) \
            else replace(base, cwd=cwd or None)
    if kind == "worktree":
        tree = data.get("worktree")
        return replace(base, worktree=dict(tree) if isinstance(tree, Mapping) else None)
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
#: The result of a command that succeeds and prints nothing.
NO_OUTPUT = "(Bash completed with no output)"

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


#: Commands whose exit status 1 is an answer, not a failure (no match, a
#: difference, a false condition): Claude Code's table. Status 2 and up
#: still fail.
_EXIT_ONE_ANSWERS = frozenset({"grep", "rg", "egrep", "fgrep", "find", "diff", "test", "["})
#: ``git`` subcommands read the same way.
_GIT_EXIT_ONE_ANSWERS = frozenset({"diff", "grep"})
#: Where a command line is split into its last command when it cannot be
#: parsed.
_COMMAND_SEPARATOR = re.compile(r"&&|\|\||[;|\n]")


def _last_command(command: str) -> list[str]:
    """The words of the command line's last command (what decides its exit
    status), by the parser when it can read the line, else by splitting on
    the control operators."""
    parsed = parse(command)
    if not parsed.opaque:
        return parsed.commands[-1].argv if parsed.commands else []
    return _COMMAND_SEPARATOR.split(command)[-1].split()


def exit_fails(command: str, exit_code: int) -> bool:
    """Whether ``exit_code`` makes ``command`` an error result."""
    if exit_code == 0:
        return False
    if exit_code != 1:
        return True
    argv = _last_command(command)
    name = argv[0] if argv else ""
    if name == "git":
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in ("-C", "-c") else 1
        return i >= len(argv) or argv[i] not in _GIT_EXIT_ONE_ANSWERS
    return name not in _EXIT_ONE_ANSWERS


def _blocked_sleep(described: str) -> str:
    """Claude Code's refusal of a long leading ``sleep``."""
    return (
        f"Blocked: {described}. To wait for a condition, use Monitor with an "
        "until-loop (e.g. `until <check>; do sleep 2; done`). To wait for a "
        "command you started, use run_in_background: true. Do not chain "
        "shorter sleeps to work around this block."
    )


#: ``git`` subcommands that only read (those the permissions plugin can let
#: through without asking).
_GIT_READ_SUBCOMMANDS = frozenset(
    "log show diff whatchanged shortlog status ls-files ls-tree blame rev-parse cat-file "
    "describe grep reflog merge-base name-rev count-objects branch tag remote config "
    "stash".split())
#: Global ``git`` options whose value is the next word.
_GIT_GLOBAL_VALUED = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
                      "--super-prefix")
#: Settings a reading ``git`` runs with over any repository's own: no
#: fsmonitor hook, hooks, pager, signature program or transport (a lazy
#: fetch), and (git 2.38+) no repository found by walking into a bare one.
_GIT_SAFE_CONFIG = ("core.fsmonitor=", "core.hooksPath=/dev/null", "core.pager=cat",
                    "safe.bareRepository=explicit", "protocol.allow=never",
                    "log.showSignature=false", "gpg.program=false",
                    "gpg.openpgp.program=false", "gpg.x509.program=false",
                    "gpg.ssh.program=false")
#: A ``git`` function every reading command runs under: it runs ``git``
#: with :data:`_GIT_SAFE_CONFIG` and without external diff programs or
#: ``textconv`` filters, and with a guard (:data:`_GIT_GUARD`) refuses a
#: repository planted in the workspace.
_GIT_FUNCTION = """git() {
  local _noeta_pre=() _noeta_sub=() _noeta_dir _noeta_top _noeta_named
  while [ "$#" -gt 0 ]; do
    case "$1" in
      @VALUED@) [ "$#" -ge 2 ] || break; _noeta_pre+=("$1" "$2"); shift 2 ;;
      -*) _noeta_pre+=("$1"); shift ;;
      *) break ;;
    esac
  done
@GUARD@  case "${1-}" in
    diff|log|show|whatchanged) _noeta_sub=("$1" --no-ext-diff --no-textconv); shift ;;
    blame) _noeta_sub=(blame --no-textconv); shift ;;
    stash) if [ "${2-}" = show ]; then
        _noeta_sub=(stash show --no-ext-diff --no-textconv); shift 2; fi ;;
    reflog) case "${2-}" in
        expire|delete|drop|exists|list) ;;
        show) _noeta_sub=(reflog show --no-ext-diff --no-textconv); shift 2 ;;
        *) _noeta_sub=(reflog show --no-ext-diff --no-textconv); shift ;;
      esac ;;
  esac
  command git "${_noeta_pre[@]+"${_noeta_pre[@]}"}" @CONFIG@ \\
    "${_noeta_sub[@]+"${_noeta_sub[@]}"}" "$@"
}
""".replace("@VALUED@", "|".join(_GIT_GLOBAL_VALUED)).replace(
    "@CONFIG@", " ".join(f"-c {c}" for c in _GIT_SAFE_CONFIG))
#: The refusal of a repository whose git directory lies under the guard
#: root (``@ROOT@``) without being a ``.git`` directory or the one a
#: ``.git`` file at its top names: a bare repository committed into the
#: workspace, whose config its author wrote. A clone cannot carry a
#: ``.git`` file, so a worktree's or a separate git directory passes.
_GIT_GUARD = """  if _noeta_dir=$(command git "${_noeta_pre[@]+"${_noeta_pre[@]}"}" \\
      -c core.fsmonitor= rev-parse --absolute-git-dir 2>/dev/null) \\
      && _noeta_dir=$(cd "$_noeta_dir" 2>/dev/null && pwd -P); then
    case "$_noeta_dir/" in
      @ROOT@/*) case "$_noeta_dir" in
        */.git|*/.git/*) ;;
        *) _noeta_top=$(command git "${_noeta_pre[@]+"${_noeta_pre[@]}"}" -c core.fsmonitor= \\
             rev-parse --show-toplevel 2>/dev/null) || _noeta_top=
          _noeta_named=
          if [ -n "$_noeta_top" ] && [ -f "$_noeta_top/.git" ]; then
            _noeta_named=$(cd "$(command git -C "$_noeta_top" -c core.fsmonitor= \\
              rev-parse --absolute-git-dir 2>/dev/null)" 2>/dev/null && pwd -P)
          fi
          if [ "$_noeta_named" != "$_noeta_dir" ]; then
            printf 'git: refused the repository at %s: not a .git directory\\n' \\
              "$_noeta_dir" >&2; return 128
          fi ;;
      esac ;;
    esac
  fi
"""


def git_prelude(command: str, guard_root: str = "") -> str:
    """:data:`_GIT_FUNCTION` when ``command`` runs ``git`` only to read
    (every ``git`` in it has a :data:`_GIT_READ_SUBCOMMANDS` subcommand),
    so whatever directory it reads in, a repository's config runs nothing;
    ``""`` otherwise (a ``git commit`` keeps its hooks). With
    ``guard_root`` (a canonical directory) it also carries
    :data:`_GIT_GUARD` for that root."""
    parsed = parse(command)
    if parsed.opaque:
        return ""
    found = False
    for cmd in parsed.commands:
        argv = cmd.argv
        if not argv or argv[0] != "git":
            continue
        found = True
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in _GIT_GLOBAL_VALUED else 1
        if i >= len(argv) or argv[i] not in _GIT_READ_SUBCOMMANDS:
            return ""
    if not found:
        return ""
    guard = _GIT_GUARD.replace("@ROOT@", shlex.quote(guard_root)) if guard_root else ""
    return _GIT_FUNCTION.replace("@GUARD@", guard)


def guard_root(ws: Workspace, ctx: ToolContext) -> str:
    """The root :func:`git_prelude` refuses planted repositories under: the
    workspace, unless a person approved this command (it then runs
    hardened, as approved)."""
    return "" if ctx.approved else ws.root


def _track_cwd(command: str) -> str:
    """``command`` with an EXIT trap printing the shell's final directory, so
    it is reported however the command ends short of a kill (``exit N``
    included) and the exit status is untouched."""
    return (f"exec 2>&1\ntrap 'printf \"\\n%s%s\\n\" \"{_CWD_MARK}\" \"$(pwd -P)\"' EXIT\n"
            f"{command}")


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
    """The merged output (stderr is redirected into stdout; anything left on
    stderr follows), trailing newlines trimmed."""
    text = (stdout + stderr).decode("utf-8", errors="replace")
    return text.rstrip("\n")


def _capture_notice(result: RunResult, output_cap: int) -> str:
    """Name a stream whose HEAD the capture cap dropped, or ``""``."""
    if not (result.stdout_truncated or result.stderr_truncated):
        return ""
    size = f"{output_cap // 1024} KB" if output_cap >= 1024 else f"{output_cap} bytes"
    return f"[earlier output dropped: only the last {size} was captured]"



_DESCRIPTION_FIELD = (
    "What the command does, in plain words and the active voice, for a user who reads "
    "this without seeing the command: do not repeat the command, its flags or its paths, "
    "and do not use the words \"complex\" or \"risk\".\n\n"
    "A simple command gets about 5-10 words:\n"
    "- pytest -q -> \"Run the test suite\"\n"
    "- git log -5 -> \"Show the five latest commits\"\n"
    "- make build -> \"Build the package\"\n\n"
    "A pipeline or an unusual flag gets enough context to be understood:\n"
    "- du -sh * | sort -h | tail -3 -> \"Show which three folders here use the most disk\"\n"
    "- docker ps -q | xargs docker stop -> \"Stop every running container\"\n"
    "- tar -xzf release.tgz -C out -> \"Unpack the release archive into the out folder\"")


def sandbox_required(reason: str) -> str:
    return (f"the sandbox is required (sandbox.failIfUnavailable) but cannot start: {reason}. "
            "The command was not run, and running it outside the sandbox "
            "(dangerouslyDisableSandbox) is refused too.")


async def confine(env: Any, command: str, disable: Any, argv: list[str], cwd: str,
                  writable: Sequence[str] = ()) -> PreparedCommand | str | None:
    """``argv`` made ready to run in the env's sandbox when ``command``
    belongs there; ``None`` to run it as it is; a refusal when it belongs
    there, the sandbox cannot start and the settings require it."""
    sandbox = sandbox_of(env)
    if sandbox is None or not belongs_in_sandbox(sandbox, command):  # excluded
        return None
    reason = sandbox.unavailable()
    if reason is not None:
        return sandbox_required(reason) if sandbox.settings.fail_if_unavailable else None
    if not belongs_in_sandbox(sandbox, command, disable):
        return None
    return await sandbox.prepare(argv, cwd=cwd, writable=writable)


def _listing(paths: Sequence[str], limit: int = 8) -> str:
    shown = ", ".join(f"`{p}`" for p in paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def sandbox_description(base: str, sandbox: CommandSandbox, root: str) -> str:
    """``Bash``'s description with the sandbox section for ``sandbox``."""
    s = sandbox.settings
    if s.filesystem_disabled:
        filesystem = "not confined: commands read and write as you do."
    else:
        writable = [root, *(s.resolve(p, root) for p in s.allow_write)]
        filesystem = (f"writes are allowed only in {_listing(writable)} and `$TMPDIR`; "
                      "everything else is read-only. Credential stores (SSH and GPG keys, "
                      "cloud sign-ins)" + (f", {_listing(list(s.deny_read))}" if s.deny_read
                                           else "") + " cannot be read.")
    if s.http_proxy_port or s.socks_proxy_port:
        network = "connections go through the user's proxy."
    elif s.allowed_domains:
        network = (f"only these hosts can be reached: {_listing(list(s.allowed_domains), 20)}."
                   )
    else:
        network = "no host can be reached."
    escape = load_description("noeta.plugins.shell", "shell_sandbox_escape"
                              if s.allow_unsandboxed else "shell_sandbox_no_escape")
    section = load_description("noeta.plugins.shell", "shell_sandbox")
    section = (section.replace("{filesystem}", filesystem).replace("{network}", network)
               .replace("{escape}", escape))
    return f"{base}\n\n{section}"


def _input_schema() -> dict[str, Any]:
    return {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line to run."},
            "timeout": {"type": "number",
                        "description": ("Time limit in milliseconds; at most 600000 for a "
                                        "command run in the foreground.")},
            "description": {"type": "string", "description": _DESCRIPTION_FIELD},
            "run_in_background": {"type": "boolean",
                                  "description": ("Run it detached and get its ID at once; "
                                                  "you are told when it ends.")},
            "dangerouslyDisableSandbox": {
                "type": "boolean",
                "description": ("Ask to run this command outside the sandbox; only when the "
                                "sandbox is what makes it fail.")},
        },
        "required": ["command"],
        "additionalProperties": False,
    }


class BashTool:
    """``Bash`` (foreground). ``shell_env`` adds per-call variables;
    ``hardening_rules`` are the rules whose ``harden`` rewrite applies to a
    matching command (the curated ``git`` flags by default); a command that
    only reads with ``git`` runs under :func:`git_prelude` instead, unless
    the rules are empty (hardening off)."""

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
        hardening_rules: tuple[HardeningRule, ...] = DEFAULT_SHELL_RULES,
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
        # A command that only reads with git runs under the hardening git
        # function instead, wherever it reads.
        prelude = git_prelude(command, guard_root(ws, ctx)) if self.hardening_rules else ""
        hardened = None if prelude else hardened_argv(command, self.hardening_rules)
        track = hardened is None
        if hardened is not None:
            exec_argv = ["bash", "-c", 'exec 2>&1; exec "$@"', "bash", *hardened]
        else:
            exec_argv = ["bash", "-c", _track_cwd(prelude + command)]

        confined = await confine(env, command, input.get("dangerouslyDisableSandbox"),
                                 exec_argv, cwd)
        if isinstance(confined, str):
            return tool_error(self.name, confined)
        try:
            result = await env.run(
                confined.argv if confined else exec_argv,
                cwd=cwd,
                env={**self._command_env(ctx), **(confined.env if confined else {})},
                timeout=float(timeout_s),
                output_cap=self.output_cap,
            )
        finally:
            refused = confined.violations() if confined else []
            if confined:
                await confined.close()
        stdout, stderr = result.stdout, result.stderr
        final, left = cwd, False
        if track:
            stdout, reported = _split_cwd(stdout)
            if reported is None:  # the command sent its stdout to stderr
                stderr, reported = _split_cwd(stderr)
            if reported is not None:
                if path_within(reported, ws.root):
                    final = reported
                else:
                    final, left = ws.root, True

        body = _render_streams(stdout, stderr)
        notice = _capture_notice(result, self.output_cap)
        if notice:
            body = f"{notice}\n{body}" if body else notice
        meta: dict[str, Any] = {"exit_code": result.exit_code}
        if result.timed_out:
            meta["timed_out"] = True
        ran_ok = not result.timed_out and result.exit_code == 0
        failed = result.timed_out or exit_fails(command, result.exit_code)
        if not failed and not body:
            body = NO_OUTPUT
        # Only the root task's shell keeps a directory, and only one a
        # command ended in successfully.
        if not ran_ok or (ctx.root_id and ctx.root_id != ctx.task_id):
            final, left = cwd, False
        moved_back = moved_back or left
        if moved_back:
            # Claude Code's note when a command left the project.
            note = f"Shell cwd was reset to {ws.root}"
            kept = body.strip() if body != NO_OUTPUT else ""
            body = f"{kept}\n{note}" if kept else note
        if final != cwd or moved_back:
            ctx.record(SHELL, "cwd", {"cwd": final})
            meta["cwd"] = final
        if refused:
            block = violations_block(refused)
            body = block if body == NO_OUTPUT else f"{body}\n{block}"
        body = await limit_output(body, SHELL_OUTPUT_LIMIT, ctx)
        if result.timed_out:
            return error_output(f"Command timed out after {timeout_s}s", body, **meta)
        if failed:
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
