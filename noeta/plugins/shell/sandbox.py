"""``Bash`` under the env's sandbox (``noeta.toolkit.sandbox``): whether a
command runs confined, the refusal when the sandbox is required but cannot
start, and what the model is told about the sandbox (Claude Code's
``sandbox_instructions``, sent again whenever it changes)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from noeta.toolkit.sandbox import (
    CommandSandbox,
    PreparedCommand,
    belongs_in_sandbox,
    sandbox_of,
)
from noeta.toolkit.tools import load_description


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
    try:
        return await sandbox.prepare(argv, cwd=cwd, writable=writable)
    except PermissionError as exc:
        return f"{exc}. The command was not run."


def _listing(paths: Sequence[str], limit: int = 8) -> str:
    shown = ", ".join(f"`{p}`" for p in paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


#: What the model is told once commands no longer run confined, after it
#: was told they do.
SANDBOX_OFF = ("The Bash sandbox is now off: commands run without its limits, and what you "
               "were told about it earlier no longer applies.")


def sandbox_instructions(sandbox: CommandSandbox | None, root: str) -> str:
    """What the model is told about the sandbox ``Bash`` commands run in
    under ``root``; ``""`` when they run unconfined (no sandbox, or one that
    cannot start)."""
    if sandbox is None or sandbox.unavailable() is not None:
        return ""
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
    return (section.replace("{filesystem}", filesystem).replace("{network}", network)
            .replace("{escape}", escape))
