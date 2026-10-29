"""Where ``Bash`` commands start: the directory a command left the shell
in, reported by an EXIT trap on a marker line and checked against the
workspace before the next command starts there."""

from __future__ import annotations

import posixpath

from noeta.core.tool import ToolContext
from noeta.plugins.shell.state import shell_state
from noeta.toolkit.tools import Workspace, path_within

#: Line a tracked command prints last, carrying ``pwd -P``; stripped before
#: anything is shown.
CWD_MARK = "___NOETA_SHELL_CWD___"


def track_cwd(command: str) -> str:
    """``command`` with an EXIT trap printing the shell's final directory, so
    it is reported however the command ends short of a kill (``exit N``
    included) and the exit status is untouched."""
    return (f"exec 2>&1\ntrap 'printf \"\\n%s%s\\n\" \"{CWD_MARK}\" \"$(pwd -P)\"' EXIT\n"
            f"{command}")


def split_cwd(stream: bytes) -> tuple[bytes, str | None]:
    """``stream`` without the trap's line, and the directory it named."""
    mark = ("\n" + CWD_MARK).encode("utf-8")
    at = stream.rfind(mark)
    if at < 0:
        return stream, None
    start = at + len(mark)
    end = stream.find(b"\n", start)
    path = stream[start : end if end >= 0 else len(stream)].decode("utf-8", errors="replace")
    rest = stream[:at] + (stream[end + 1 :] if end >= 0 else b"")
    return rest, path or None


async def start_cwd(ws: Workspace, ctx: ToolContext) -> tuple[str, bool]:
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
