"""Shared subprocess environment scrubbing for tool packs.

Promoted out of ``noeta.tools.fs.shell`` (Phase 4.5 F2) so the MCP stdio
client and the shell tool build a launched subprocess's environment from
the **same** explicit allowlist — no duplicated, drifting copies. Tools
that spawn a child process should use :func:`scrub_env` rather than
passing the parent ``os.environ`` through.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional


__all__ = ["ENV_ALLOWLIST", "scrub_env"]


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


def scrub_env(
    allowlist: Optional[Iterable[str]] = None,
) -> dict[str, str]:
    """Build a minimal env from the parent — explicit allowlist only.

    ``allowlist`` narrows (or replaces) the default :data:`ENV_ALLOWLIST` for
    callers whose subprocess should inherit even less — e.g. a notify hook
    command that has no business seeing the Python interpreter keys. ``None``
    keeps the default set, byte-identical for every existing caller.
    """
    parent = os.environ
    keys = ENV_ALLOWLIST if allowlist is None else allowlist
    return {key: parent[key] for key in keys if key in parent}
