"""What ``Edit`` and ``Write`` share: the write fence, the read-first check,
the unified diff and the record of what the model has now seen."""

from __future__ import annotations

import difflib
import hashlib
import posixpath
from collections.abc import Callable, Sequence

from noeta.core.tool import ToolContext, ToolOutput
from noeta.core.types import Display
from noeta.plugins.fs.state import FS, KIND_READ, current_fs_state
from noeta.toolkit.tools import Workspace, tool_error

__all__ = [
    "WriteRoots",
    "authorized_roots",
    "compute_diff",
    "diff_display",
    "diff_stat_counts",
    "read_precondition_error",
    "record_written",
]

#: ``ctx -> directories this call may write outside the workspace``. Asked
#: per call, so an authorization granted while the task waits takes effect on
#: the resumed call.
WriteRoots = Callable[[ToolContext], Sequence[str]]


async def authorized_roots(
    ws: Workspace, resolver: WriteRoots | None, ctx: ToolContext
) -> tuple[str, ...]:
    """The extra write roots for this call, canonicalised. Fails closed: no
    resolver, a resolver that raises, or a non-absolute entry grants
    nothing."""
    if resolver is None:
        return ()
    try:
        raw = resolver(ctx)
    except Exception:  # noqa: BLE001 - an authorization fault denies, never grants
        return ()
    roots: list[str] = []
    for entry in raw or ():
        if isinstance(entry, str) and entry and posixpath.isabs(entry):
            roots.append(await ws.env.realpath(entry))
    return tuple(roots)


def read_precondition_error(
    ctx: ToolContext, resolved: str, raw: bytes, *, tool_name: str, verb: str
) -> ToolOutput | None:
    """The file must have been read (an ``fs`` entry for the canonical path)
    and its bytes must still hash to what was read — otherwise the model
    would change content it has not seen."""
    recorded = current_fs_state(ctx.state).digest(resolved)
    if recorded is None:
        return tool_error(
            tool_name, f"File has not been read yet. Read it first before {verb} it."
        )
    if recorded != hashlib.sha256(raw).hexdigest():
        return tool_error(
            tool_name,
            "File has been modified since it was read. Read it again before "
            f"{verb} it.",
        )
    return None


async def record_written(ctx: ToolContext, resolved: str, body: bytes) -> None:
    """Record the new digest after a successful write, so consecutive edits
    chain without a re-read (the model has seen exactly what it wrote)."""
    try:
        st = await ctx.env.stat(resolved)
    except OSError:
        st = None
    ctx.record(
        FS,
        KIND_READ,
        {
            "path": resolved,
            "digest": hashlib.sha256(body).hexdigest(),
            "mtime": st.mtime if st is not None else 0.0,
        },
    )


def compute_diff(before: str, after: str, rel_path: str) -> str:
    """``difflib`` unified diff with ``a/<rel>`` / ``b/<rel>`` framing."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )


def diff_stat_counts(diff: str) -> tuple[int, int]:
    """``(added, removed)`` line counts, excluding the ``+++``/``---`` headers."""
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def diff_display(rel: str, diff: str, added: int, removed: int) -> Display:
    return Display(
        kind="diff",
        data={"path": rel, "diff": diff, "added": added, "removed": removed},
        fallback=f"{rel} +{added}/-{removed}",
    )
