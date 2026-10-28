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
    "changed_since_read",
    "compute_diff",
    "diff_display",
    "diff_stat_counts",
    "mutation_result",
    "read_precondition_error",
    "record_written",
    "seen_whole",
    "stale_error",
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
    ctx: ToolContext, resolved: str, raw: bytes, *, tool_name: str, verb: str,
    unread_ok: bool = False, partial_ok: bool = False, check_stale: bool = True,
) -> ToolOutput | None:
    """The file must have been read (an ``fs`` entry for the canonical path)
    and its bytes must still hash to what was read — otherwise the model
    would change content it has not seen.

    Claude Code skips the read-first part for a write its permissions allow
    outright; here that is a call no person was asked about
    (``not ctx.approved``), which the caller passes as ``unread_ok``.
    ``partial_ok`` extends that to a read cut to fit the token ceiling
    (``Edit``; a whole-file ``Write`` over a partial view is always
    refused). ``check_stale=False`` leaves a change since the read to the
    caller (``Edit``'s recovery, see :func:`changed_since_read`)."""
    entry = current_fs_state(ctx.state).reads.get(resolved)
    # A read cut to fit the token ceiling showed only part of the file.
    seen = entry is not None and not entry.partial
    if not seen:
        if not unread_ok or (entry is not None and not partial_ok):
            return tool_error(
                tool_name, f"File has not been read yet. Read it first before {verb} it."
            )
        if entry is None:
            return None
    assert entry is not None
    if check_stale and entry.digest != hashlib.sha256(raw).hexdigest():
        return stale_error(tool_name, verb)
    return None


def stale_error(tool_name: str, verb: str) -> ToolOutput:
    return tool_error(
        tool_name,
        f"File has been modified since it was read. Read it again before {verb} it.",
    )


def changed_since_read(ctx: ToolContext, resolved: str, raw: bytes) -> bool:
    """The file's bytes differ from what the model last read or wrote."""
    entry = current_fs_state(ctx.state).reads.get(resolved)
    return entry is not None and entry.digest != hashlib.sha256(raw).hexdigest()


def seen_whole(ctx: ToolContext, resolved: str, raw: bytes) -> bool:
    """The model has these exact bytes in view as a whole file: a read from
    the top to the end (or its own write over one) that nothing has changed
    since. Only then does an ``Edit`` result carry the file-state note."""
    entry = current_fs_state(ctx.state).reads.get(resolved)
    return (entry is not None and entry.whole and not entry.partial
            and entry.digest == hashlib.sha256(raw).hexdigest())


async def record_written(ctx: ToolContext, resolved: str, body: bytes, *,
                         whole: bool = True) -> None:
    """Record the new digest after a successful write, so consecutive edits
    chain without a re-read (the model has seen exactly what it wrote).
    ``whole=False``: the model did not have the rest of the file in view."""
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
            **({} if whole else {"whole": False}),
        },
    )


#: Follows a successful write: the model already holds the file as written.
FILE_STATE_NOTE = "(what you wrote is the file's current state; no need to Read it back)"
#: Follows an edit applied over changes made on disk since the model's read.
STALE_EDIT_NOTE = (
    "(the file had changed on disk since your last Read; the edit applied cleanly, but the "
    "file holds other changes you have not seen, so Read it before an edit that depends on "
    "the text around it)")


def mutation_result(path: str, *, created: bool, stale: bool = False,
                    in_view: bool = True) -> str:
    """The one-line result of a successful ``Edit`` or ``Write`` (absolute
    path). The file-state note follows only when the model holds the whole
    file as it now is (``in_view``); an edit over changes made since the
    read says so instead (Claude Code's rules)."""
    head = (f"File created successfully at: {path}" if created
            else f"The file {path} has been updated successfully.")
    if stale:
        return f"{head} {STALE_EDIT_NOTE}"
    if not in_view:
        return head
    return f"{head} {FILE_STATE_NOTE}"


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
