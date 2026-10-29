"""``Bash`` under the env's sandbox (``noeta.toolkit.sandbox``): whether a
command runs confined, the refusal when the sandbox is required but cannot
start, and the sandbox section of the tool's description."""

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
    except PermissionError as exc:  # a tenant's working directory gone elsewhere
        return f"{exc}. The command was not run."


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
