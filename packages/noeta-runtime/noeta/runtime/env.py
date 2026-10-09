"""Subprocess environment scrubbing shared by every tool that spawns a child.

The MCP stdio client and the shell tools build a launched subprocess's
environment from the **same** explicit allowlist, so a spawned child cannot
inherit the host's credentials by accident. A tool that spawns a process
calls :func:`scrub_env` rather than passing the parent ``os.environ`` through.
"""

from __future__ import annotations

import os
from typing import Callable, Iterable, Mapping, Optional


__all__ = ["ENV_ALLOWLIST", "ShellEnvResolver", "scrub_env"]


#: The only parent-environment keys a launched subprocess inherits.
ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        # Python + uv interpreter discovery
        "PYTHONHASHSEED",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
)


#: ``root task id -> variables`` a host adds to that task's shell commands, on
#: top of the scrubbed environment. Resolved per command, so what it answers
#: may change while the task lives; never recorded, never model-controlled.
ShellEnvResolver = Callable[[str], Mapping[str, str]]


def scrub_env(
    allowlist: Optional[Iterable[str]] = None,
) -> dict[str, str]:
    """Build a minimal env from the parent — explicit allowlist only.

    ``allowlist`` narrows (or replaces) :data:`ENV_ALLOWLIST` for callers whose
    subprocess should inherit even less — e.g. a notify hook command that has
    no business seeing the Python interpreter keys.
    """
    parent = os.environ
    keys = ENV_ALLOWLIST if allowlist is None else allowlist
    return {key: parent[key] for key in keys if key in parent}
