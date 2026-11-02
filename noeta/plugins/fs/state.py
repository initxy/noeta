"""What the file tools remember between calls, as a pure fold.

Tools never keep state of their own. They record facts with
``ctx.record("fs", kind, data)`` and read the folded result back from
``ctx.state.get("fs")``; the runner applies :func:`reduce_fs` to every
recorded entry, in log order, so a fresh process rebuilds the same state from
the task's log.

Kinds:

- ``read`` — ``{"path", "digest", "mtime", "range"?}``: the model has seen
  ``path`` whose raw bytes hash (sha256 hex) to ``digest``; ``range``
  (``[offset, limit]``) is set by ``Read`` and is what an unchanged re-read
  of the same range is answered from; ``partial`` marks a read cut to fit
  the token ceiling, which counts as not read for ``Edit`` / ``Write`` and is
  never answered as unchanged; ``whole: false`` marks a view that was not
  the whole file (a window, or an edit made without the file in view), which
  takes the file-state note off ``Edit``'s result; ``window: true`` marks a
  view of some lines without a ``range`` to answer from (a command's output
  cut to fit). A ``Bash`` command that showed a file records one too,
  with ``"by": "Bash"``, the command's ``call_id`` and, for a file a deny
  rule keeps from ``Read``, ``"read_denied": true``
  (:mod:`noeta.plugins.fs.shell_reads`). ``Edit`` and ``Write``
  refuse to change a file without an entry whose digest still matches the
  bytes on disk. A successful write records the new digest, so consecutive
  edits chain without a re-read.
- ``changed`` — ``{"path", "digest", "mtime"?}``: the model was told that
  ``path`` changed on disk since it read it (to those bytes, found at that
  modification time); it is told once per change.
- ``checked`` — ``{"path", "mtime"}``: the changed-file check found ``path``
  under a new modification time holding bytes the model already has or was
  told of; it is not read again until the time moves on.
- ``forget_ranges`` — a compaction dropped the earlier results: no re-read is
  answered from them.
- ``mentions`` — ``{"through", "path"?}``: the person's messages up to log
  seq ``through`` had their ``@`` mentions attached (an attachment is
  recorded with its message and names its ``path``).
- ``images`` — ``{"through", "paths"}``: the person's message at log seq
  ``through`` has its pasted images saved as ``paths`` (recorded with the
  note naming them; again when they had to be written elsewhere, and with
  no paths once they are gone).
- ``notice`` — ``{"text"}``: a note for the model to get after this call's
  result (a cut read's paging hint); ``notice_sent`` (the reminder that
  carried it) clears it.

Only the :data:`MAX_FILES` most recently read or written files are
remembered (and told about); an older one counts as never read. An unknown
kind or malformed data leaves the state unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import msgspec

from noeta.core.types import Struct

__all__ = [
    "FS",
    "KIND_CHANGED",
    "KIND_CHECKED",
    "KIND_FORGET_RANGES",
    "KIND_IMAGES",
    "KIND_MENTIONS",
    "KIND_NOTICE",
    "KIND_NOTICE_SENT",
    "KIND_READ",
    "MAX_FILES",
    "FileRead",
    "FsState",
    "current_fs_state",
    "reduce_fs",
]

#: The extension name the file tools record under.
FS = "fs"
KIND_READ = "read"
KIND_CHANGED = "changed"
KIND_CHECKED = "checked"
KIND_FORGET_RANGES = "forget_ranges"
KIND_NOTICE = "notice"
KIND_NOTICE_SENT = "notice_sent"
KIND_MENTIONS = "mentions"
KIND_IMAGES = "images"
#: Files remembered as read at most (Claude Code's read-file state holds as
#: many): the oldest are forgotten past it.
MAX_FILES = 5000
#: Attachments remembered for placing ahead of the person's text.
MAX_ATTACHMENTS = 200


class FileRead(Struct):
    digest: str
    mtime: float = 0.0
    #: ``[offset, limit]`` of the ``Read`` that recorded this, if one did.
    range: list[int] | None = None
    #: The read was cut to fit the token ceiling: the model saw only part.
    partial: bool = False
    #: The model had the whole file in view (a read from the top to the end,
    #: or what it wrote itself over such a view).
    whole: bool = True
    #: Recorded by a ``Read`` given an offset past the first line or a limit
    #: (or cut to fit the token ceiling), or by a command that showed some
    #: lines: no changed-file note follows it, as Claude Code sends none for
    #: such a read. Kept when a compaction drops ``range``.
    window: bool = False
    #: The ``Bash`` call whose output showed the file, for a command's read.
    call_id: str | None = None


class FsState(Struct):
    #: Canonical absolute path -> what was read (or last written) there,
    #: the most recent last.
    reads: dict[str, FileRead] = msgspec.field(default_factory=dict)
    #: Path -> the digest the model was last told the file changed to.
    noticed: dict[str, str] = msgspec.field(default_factory=dict)
    #: Path -> the modification time the changed-file check last found the
    #: file at with nothing new to tell (``changed`` / ``checked``).
    checked: dict[str, float] = msgspec.field(default_factory=dict)
    #: Notes waiting to reach the model after the results they belong to.
    notices: list[str] = msgspec.field(default_factory=list)
    #: Log seq of the newest message of the person's whose mentions were
    #: attached.
    mentions_through: int = 0
    #: Log seq of the newest message of the person's whose pasted images
    #: were saved as files.
    images_through: int = 0
    #: Log seq of a person's message (as text) -> where its images are saved.
    images: dict[str, list[str]] = msgspec.field(default_factory=dict)
    #: Content digests of the attachments' messages (the newest
    #: :data:`MAX_ATTACHMENTS`), which the request moves ahead of the
    #: person's text.
    attached: list[str] = msgspec.field(default_factory=list)


def reduce_fs(state: FsState | None, kind: str, data: Any) -> FsState:
    """``state`` with one recorded entry applied. Never mutates ``state``."""
    base = state if state is not None else FsState()
    if not isinstance(data, Mapping):
        return base
    if kind == KIND_READ:
        path, digest = data.get("path"), data.get("digest")
        if not isinstance(path, str) or not path or not isinstance(digest, str):
            return base
        mtime = data.get("mtime", 0.0)
        if isinstance(mtime, bool) or not isinstance(mtime, (int, float)):
            mtime = 0.0
        lines = data.get("range")
        if not (isinstance(lines, list) and len(lines) == 2
                and all(isinstance(n, int) and not isinstance(n, bool) for n in lines)):
            lines = None
        call_id = data.get("call_id")
        # A copy of the map (the fold stays pure: a simulated fold shares
        # this state) but no walk over it or ``checked`` in Python.
        reads = dict(base.reads)
        reads.pop(path, None)
        dropped = []
        while len(reads) >= MAX_FILES:
            oldest = next(iter(reads))
            del reads[oldest]
            dropped.append(oldest)
        reads[path] = FileRead(digest=digest, mtime=float(mtime), range=lines,
                               partial=data.get("partial") is True,
                               whole=data.get("whole") is not False,
                               window=data.get("window") is True or (
                                   lines is not None and (lines[0] > 1 or lines[1] > 0)),
                               call_id=call_id if isinstance(call_id, str) else None)
        return msgspec.structs.replace(base, reads=reads,
                                       checked=_without(base.checked, [path, *dropped]),
                                       noticed=_without(base.noticed, dropped))
    if kind in (KIND_NOTICE, KIND_NOTICE_SENT):
        text = data.get("text")
        if not isinstance(text, str):
            return base
        notices = list(base.notices)
        if kind == KIND_NOTICE:
            notices.append(text)
        elif text in notices:
            notices.remove(text)
        return msgspec.structs.replace(base, notices=notices)
    if kind == KIND_MENTIONS:
        through = data.get("through")
        if isinstance(through, bool) or not isinstance(through, int):
            return base
        digest = data.get("digest")
        attached = base.attached
        if isinstance(digest, str) and digest:
            attached = [*attached, digest][-MAX_ATTACHMENTS:]
        return msgspec.structs.replace(
            base, mentions_through=max(base.mentions_through, through), attached=attached)
    if kind == KIND_IMAGES:
        through = data.get("through")
        if isinstance(through, bool) or not isinstance(through, int):
            return base
        paths = [p for p in data.get("paths") or () if isinstance(p, str)]
        images = {k: v for k, v in base.images.items() if k != str(through)}
        if paths:
            images[str(through)] = paths
        return msgspec.structs.replace(base, images_through=max(base.images_through, through),
                                       images=images)
    if kind == KIND_FORGET_RANGES:
        return msgspec.structs.replace(base, reads={
            p: msgspec.structs.replace(r, range=None) for p, r in base.reads.items()})
    if kind == KIND_CHANGED:
        path, digest = data.get("path"), data.get("digest")
        if not isinstance(path, str) or not isinstance(digest, str):
            return base
        noticed = dict(base.noticed)
        noticed.pop(path, None)
        noticed[path] = digest
        return _checked(msgspec.structs.replace(base, noticed=noticed), path, data.get("mtime"))
    if kind == KIND_CHECKED:
        path = data.get("path")
        if not isinstance(path, str) or path not in base.reads:
            return base
        return _checked(base, path, data.get("mtime"))
    return base


def _checked(state: FsState, path: str, mtime: Any) -> FsState:
    """``state`` with ``path`` found at ``mtime`` with nothing new to tell."""
    if isinstance(mtime, bool) or not isinstance(mtime, (int, float)):
        return state
    checked = dict(state.checked)
    checked.pop(path, None)
    checked[path] = float(mtime)
    return msgspec.structs.replace(state, checked=checked)


def _without(entries: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """``entries`` without ``paths`` (``entries`` itself when it holds none
    of them, so nothing is copied)."""
    if not any(p in entries for p in paths):
        return entries
    out = dict(entries)
    for p in paths:
        out.pop(p, None)
    return out


def current_fs_state(state: Mapping[str, Any]) -> FsState:
    """The ``fs`` entry of a tool context's folded state, as an
    :class:`FsState` (empty when nothing was recorded yet)."""
    raw = state.get(FS)
    if isinstance(raw, FsState):
        return raw
    if isinstance(raw, Mapping):
        try:
            return msgspec.convert(raw, FsState)
        except msgspec.ValidationError:
            return FsState()
    return FsState()
