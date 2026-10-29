"""What ``Read`` and ``Edit`` say about a path that names no file: the working
directory, and one likely intended path when there is one (Claude Code's two
guesses: the same path re-rooted under the working directory, or a file of
the same name with another extension next to it)."""

from __future__ import annotations

import posixpath
import re

from noeta.toolkit.context import fence_refuses
from noeta.toolkit.tools import Workspace, path_within

__all__ = ["missing_file_message"]

#: An extension a sibling may have instead: none, or one short suffix.
_EXTENSION = re.compile(r"^(\.[\w+~#-]{1,32})?$")


async def missing_file_message(ws: Workspace, resolved: str) -> str:
    message = f"No file at that path (the working directory is {ws.display})."
    guess = await _rerooted(ws, resolved) or await _other_extension(ws, resolved)
    if guess:
        message += f" Did you mean {guess}?"
    return message


async def _rerooted(ws: Workspace, resolved: str) -> str | None:
    """A path beside the working directory, tried again inside it: the
    commonest slip is one path component too few."""
    parent = posixpath.dirname(ws.root)
    if parent == ws.root or not path_within(resolved, parent) or path_within(resolved, ws.root):
        return None
    candidate = posixpath.join(ws.root, posixpath.relpath(resolved, parent))
    st = await ws.env.stat(candidate)
    return candidate if st is not None and not st.is_dir else None


async def _other_extension(ws: Workspace, resolved: str) -> str | None:
    directory, name = posixpath.split(resolved)
    stem = posixpath.splitext(name)[0]
    if fence_refuses(ws.env, directory):
        return None  # the listing would name files the task may not read
    try:
        listing = await ws.env.run(["ls", "-1A", directory], cwd=ws.root, timeout=10.0,
                                   output_cap=1024 * 1024)
    except OSError:
        return None
    if listing.exit_code != 0:
        return None
    for entry in sorted(listing.stdout.decode("utf-8", "replace").splitlines()):
        root, ext = posixpath.splitext(entry)
        if entry != name and root == stem and _EXTENSION.match(ext):
            return entry
    return None
