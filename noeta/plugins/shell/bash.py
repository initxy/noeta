"""``Bash``: run a command through ``bash -c`` in the foreground.

Every command gets a bounded timeout, the environment its exec env gives
it, stdout and stderr merged in the order printed, a capture cap
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

import asyncio
import re
from collections.abc import Callable, Mapping
from typing import Any

from noeta.core.execenv import RunResult
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Text
from noeta.plugins.shell.cwd import split_cwd, start_cwd, track_cwd
from noeta.plugins.shell.hardening import DEFAULT_SHELL_RULES, HardeningRule, command_script
from noeta.plugins.shell.output import capture_notice, exit_fails, output_image, render_streams
from noeta.plugins.shell.sandbox import confine
from noeta.plugins.shell.state import SHELL
from noeta.toolkit.sandbox import violations_block
from noeta.toolkit.shell import long_leading_sleep
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
    "BashTool",
    "ShellEnv",
]

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
#: exec env's environment. Asked per command; never recorded.
ShellEnv = Callable[[ToolContext], Mapping[str, str]]

#: What a host variable name must look like: a POSIX shell name.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def resolve_timeout(raw: Any, default_s: int) -> int:
    """Per-call ``timeout`` (milliseconds) -> seconds, clamped to
    :data:`MAX_SHELL_TIMEOUT_MS`. Absent, non-positive, non-numeric or
    ``bool`` falls back to ``default_s``."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return default_s
    ms = min(int(raw), MAX_SHELL_TIMEOUT_MS)
    return max(1, ms // 1000)


def _blocked_sleep(described: str) -> str:
    """The refusal of a long leading ``sleep`` (Claude Code's meaning)."""
    return (
        f"Blocked: {described}. To wait until something is true, run an "
        "until-loop under Monitor (`until <check>; do sleep 2; done`); to wait "
        "for a command of yours, start it with run_in_background: true. Do not "
        "get around this with a chain of shorter sleeps."
    )


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
    #: Claude Code's Bash result limit (its default ``bashOutputMaxChars``).
    max_result_chars = SHELL_OUTPUT_LIMIT
    input_schema: dict[str, Any]
    #: Refuse a long leading ``sleep`` (its refusal points at ``Monitor`` and
    #: ``run_in_background``, so only where background jobs are offered).
    refuses_long_sleep = False

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
        described = long_leading_sleep(command) if self.refuses_long_sleep else None
        if described is not None:
            return tool_error(self.name, _blocked_sleep(described))
        timeout_s = resolve_timeout(input.get("timeout"), self.timeout_s)

        env = ctx.env
        ws = await Workspace.of(env)
        cwd, moved_back = await start_cwd(ws, ctx)
        # A command matching a hardening rule runs as that rule's rewritten
        # argv: ``matching_rule`` admits it only when the ``shlex`` argv is
        # exactly what bash would run, so nothing but the injected flags
        # changes. Such a command cannot change directory, so it is not
        # tracked.
        # A command that only reads with git runs under the hardening git
        # function instead, wherever it reads.
        script, hardened = command_script(command, ws, ctx, self.hardening_rules)
        track = hardened is None
        description = input.get("description")
        ran = await self._execute(
            ctx, command=command, script=script, hardened=hardened, cwd=cwd,
            timeout_s=timeout_s, disable=input.get("dangerouslyDisableSandbox"),
            description=description if isinstance(description, str) else "")
        if isinstance(ran, ToolOutput):
            return ran
        result, refused = ran
        return await self._result(ctx, ws, command, result, refused, cwd=cwd,
                                  moved_back=moved_back, track=track, timeout_s=timeout_s)

    async def _result(self, ctx: ToolContext, ws: Workspace, command: str,
                      result: RunResult, refused: list[str], *, cwd: str, moved_back: bool,
                      track: bool, timeout_s: int) -> ToolOutput:
        """The call's result from what the command printed: the merged
        output with its notices, the directory the shell is left in
        (recorded), the sandbox's refusals, or an image it printed."""
        stdout, stderr = result.stdout, result.stderr
        final, left = cwd, False
        if track:
            stdout, reported = split_cwd(stdout)
            if reported is None:  # the command sent its stdout to stderr
                stderr, reported = split_cwd(stderr)
            if reported is not None:
                if path_within(reported, ws.root):
                    final = reported
                else:
                    final, left = ws.root, True

        body = render_streams(stdout, stderr)
        notice = capture_notice(result, self.output_cap)
        if notice:
            body = f"{notice}\n{body}" if body else notice
        meta: dict[str, Any] = {"exit_code": result.exit_code}
        if result.timed_out:
            meta["timed_out"] = True
        ran_ok = not result.timed_out and result.exit_code == 0
        failed = result.timed_out or exit_fails(command, result.exit_code)
        image = None if failed or notice else await asyncio.to_thread(output_image, body)
        if image is not None:
            body = ""
        if not failed and not body and image is None:
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
            body = block if body in (NO_OUTPUT, "") else f"{body}\n{block}"
        if image is not None:
            return ToolOutput(content=[image, *([Text(text=body)] if body else [])],
                              meta=meta, block_list=True)
        body = await limit_output(body, SHELL_OUTPUT_LIMIT, ctx)
        if result.timed_out:
            return error_output(f"Command timed out after {timeout_s}s", body, **meta)
        if failed:
            return error_output(f"Exit code {result.exit_code}", body, **meta)
        return text_output(body, **meta)

    async def _execute(self, ctx: ToolContext, *, command: str, script: str,
                       hardened: list[str] | None, cwd: str, timeout_s: int, disable: Any,
                       description: str) -> tuple[RunResult, list[str]] | ToolOutput:
        """Run the command (``script`` through ``bash -c``, tracked; or the
        ``hardened`` argv) and return what it printed with the hosts the
        sandbox refused, or the call's result when it ends some other way.
        A tracked command's final directory follows its output on a marker
        line (:func:`split_cwd`)."""
        if hardened is not None:
            exec_argv = ["bash", "-c", 'exec 2>&1; exec "$@"', "bash", *hardened]
        else:
            exec_argv = ["bash", "-c", track_cwd(script)]
        confined = await confine(ctx.env, command, disable, exec_argv, cwd)
        if isinstance(confined, str):
            return tool_error(self.name, confined)
        try:
            result = await ctx.env.run(
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
        return result, refused

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
