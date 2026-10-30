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
  takes the file-state note off ``Edit``'s result. ``Edit`` and ``Write``
  refuse to change a file without an entry whose digest still matches the
  bytes on disk. A successful write records the new digest, so consecutive
  edits chain without a re-read.
- ``changed`` — ``{"path", "digest"}``: the model was told that ``path``
  changed on disk since it read it (to those bytes); it is told once per
  change.
- ``forget_ranges`` — a compaction dropped the earlier results: no re-read is
  answered from them.
- ``mentions`` — ``{"through", "path"?}``: the person's messages up to log
  seq ``through`` had their ``@`` mentions attached (an attachment is
  recorded with its message and names its ``path``).
- ``images`` — ``{"through", "paths"}``: the person's message at log seq
  ``through`` had its pasted images saved as ``paths`` (recorded with the
  note naming them).
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
KIND_FORGET_RANGES = "forget_ranges"
KIND_NOTICE = "notice"
KIND_NOTICE_SENT = "notice_sent"
KIND_MENTIONS = "mentions"
KIND_IMAGES = "images"
#: Files remembered as read at most: the oldest are forgotten past it.
MAX_FILES = 1000


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


class FsState(Struct):
    #: Canonical absolute path -> what was read (or last written) there,
    #: the most recent last.
    reads: dict[str, FileRead] = msgspec.field(default_factory=dict)
    #: Path -> the digest the model was last told the file changed to.
    noticed: dict[str, str] = msgspec.field(default_factory=dict)
    #: Notes waiting to reach the model after the results they belong to.
    notices: list[str] = msgspec.field(default_factory=list)
    #: Log seq of the newest message of the person's whose mentions were
    #: attached.
    mentions_through: int = 0
    #: Log seq of the newest message of the person's whose pasted images
    #: were saved as files.
    images_through: int = 0


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
        reads = dict(base.reads)
        reads.pop(path, None)
        while len(reads) >= MAX_FILES:
            del reads[next(iter(reads))]
        reads[path] = FileRead(digest=digest, mtime=float(mtime), range=lines,
                               partial=data.get("partial") is True,
                               whole=data.get("whole") is not False)
        return msgspec.structs.replace(base, reads=reads)
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
        return msgspec.structs.replace(
            base, mentions_through=max(base.mentions_through, through))
    if kind == KIND_IMAGES:
        through = data.get("through")
        if isinstance(through, bool) or not isinstance(through, int):
            return base
        return msgspec.structs.replace(base, images_through=max(base.images_through, through))
    if kind == KIND_FORGET_RANGES:
        return msgspec.structs.replace(base, reads={
            p: msgspec.structs.replace(r, range=None) for p, r in base.reads.items()})
    if kind == KIND_CHANGED:
        path, digest = data.get("path"), data.get("digest")
        if not isinstance(path, str) or not isinstance(digest, str):
            return base
        noticed = {p: d for p, d in base.noticed.items() if p != path and p in base.reads}
        noticed[path] = digest
        return msgspec.structs.replace(base, noticed=noticed)
    return base


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
