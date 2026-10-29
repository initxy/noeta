"""What ``Edit`` and ``Write`` share: the write fence, the read-first check,
the unified diff and the record of what the model has now seen."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import posixpath
import re
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
    "file_diff",
    "mutation_result",
    "read_precondition_error",
    "record_written",
    "seen_whole",
    "stale_error",
    "unread_write_allowed",
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


#: Claude models that keep the read-first check whatever the permissions
#: say (Claude Code's rule): Opus up to 4.6, Sonnet and Haiku up to 4.5, and
#: every Claude 3 or older model.
_FAMILY_VERSION = re.compile(r"claude-(opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?!\d)")
_LAST_STRICT = {"opus": (4, 6), "sonnet": (4, 5), "haiku": (4, 5)}


def reads_first_always(model: str) -> bool:
    """Whether ``model`` is an older Claude model, for which the read-first
    check is never skipped."""
    name = model.lower().rsplit("/", 1)[-1]
    if re.search(r"claude-(?:[0-3](?:[-.]|$)|instant)", name):
        return True
    found = _FAMILY_VERSION.search(name)
    if found is None:
        return False
    version = (int(found.group(2)), int(found.group(3) or 0))
    return version <= _LAST_STRICT[found.group(1)]


def unread_write_allowed(ctx: ToolContext) -> bool:
    """A write may skip the read-first check: the permission layer allowed
    it outright (a mode or an allow rule, nobody asked), on a model that is
    not an older Claude model. Never without a permission layer."""
    return ctx.allowed_outright and not ctx.approved and not reads_first_always(ctx.model)


def read_precondition_error(
    ctx: ToolContext, resolved: str, raw: bytes, *, tool_name: str, verb: str,
    unread_ok: bool = False, partial_ok: bool = False, check_stale: bool = True,
) -> ToolOutput | None:
    """The file must have been read (an ``fs`` entry for the canonical path)
    and its bytes must still hash to what was read — otherwise the model
    would change content it has not seen.

    Claude Code skips the read-first part for a write its permissions allow
    outright (:func:`unread_write_allowed`), which the caller passes as
    ``unread_ok``; with no permission layer the check always holds.
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
                tool_name,
                f"Read the file first: {verb} a file this conversation has not read is refused."
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
                    in_view: bool = True, every: bool = False) -> str:
    """The one-line result of a successful ``Edit`` or ``Write`` (absolute
    path). The file-state note follows only when the model holds the whole
    file as it now is (``in_view``); an edit over changes made since the
    read says so instead (Claude Code's rules). ``every``: a ``replace_all``
    edit, whose line says every occurrence was replaced."""
    if created:
        head = f"File created successfully at: {path}"
    elif every:
        head = f"The file {path} has been updated: every occurrence was replaced."
    else:
        head = f"The file {path} has been updated successfully."
    if stale:
        return f"{head} {STALE_EDIT_NOTE}"
    if not in_view:
        return head
    return f"{head} {FILE_STATE_NOTE}"


#: Characters (before and after together) past which a change gets no diff:
#: difflib takes seconds a megabyte on files of many lines.
DIFF_MAX_CHARS = 1_000_000


async def file_diff(before: str, after: str, rel_path: str) -> tuple[str, int, int]:
    """The change's unified diff and its ``(added, removed)`` line counts,
    worked out off the event loop; ``("", 0, 0)`` past
    :data:`DIFF_MAX_CHARS` (see :func:`diff_display`)."""
    if len(before) + len(after) > DIFF_MAX_CHARS:
        return "", 0, 0
    diff = await asyncio.to_thread(compute_diff, before, after, rel_path)
    return diff, *diff_stat_counts(diff)


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


def diff_display(rel: str, diff: str, added: int, removed: int, *,
                 skipped: bool = False) -> Display:
    """The front ends' view of a change; ``skipped``: the file was too large
    to diff (:func:`file_diff`)."""
    return Display(
        kind="diff",
        data={"path": rel, "diff": diff, "added": added, "removed": removed},
        fallback=f"{rel} (too large to diff)" if skipped else f"{rel} +{added}/-{removed}",
    )
