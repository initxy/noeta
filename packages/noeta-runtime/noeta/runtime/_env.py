"""Shared subprocess environment scrubbing for tool packs.

Promoted out of ``noeta.tools.fs.shell`` (Phase 4.5 F2) so the MCP stdio
client and the shell tool build a launched subprocess's environment from
the **same** explicit allowlist — no duplicated, drifting copies. Tools
that spawn a child process should use :func:`scrub_env` rather than
passing the parent ``os.environ`` through.
"""

from __future__ import annotations

import os


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


def scrub_env() -> dict[str, str]:
    """Build a minimal env from the parent — explicit allowlist only."""
    parent = os.environ
    return {key: parent[key] for key in ENV_ALLOWLIST if key in parent}
