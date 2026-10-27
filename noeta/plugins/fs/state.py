"""What the file tools remember between calls, as a pure fold.

Tools never keep state of their own. They record facts with
``ctx.record("fs", kind, data)`` and read the folded result back from
``ctx.state.get("fs")``; the runner applies :func:`reduce_fs` to every
recorded entry, in log order, so a fresh process rebuilds the same state from
the task's log.

Kinds:

- ``read`` — ``{"path", "digest", "mtime"}``: the model has seen ``path``
  whose raw bytes hash (sha256 hex) to ``digest``. ``Edit`` and ``Write``
  refuse to change a file without an entry whose digest still matches the
  bytes on disk. A successful write records the new digest, so consecutive
  edits chain without a re-read.
- ``changed`` — ``{"path", "digest"}``: the model was told that ``path``
  changed on disk since it read it (to those bytes); it is told once per
  change.

An unknown kind or malformed data leaves the state unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import msgspec

from noeta.core.types import Struct

__all__ = [
    "FS",
    "KIND_CHANGED",
    "KIND_READ",
    "FileRead",
    "FsState",
    "current_fs_state",
    "reduce_fs",
]

#: The extension name the file tools record under.
FS = "fs"
KIND_READ = "read"
KIND_CHANGED = "changed"


class FileRead(Struct):
    digest: str
    mtime: float = 0.0


class FsState(Struct):
    #: Canonical absolute path -> what was read (or last written) there,
    #: the most recent last.
    reads: dict[str, FileRead] = msgspec.field(default_factory=dict)
    #: Path -> the digest the model was last told the file changed to.
    noticed: dict[str, str] = msgspec.field(default_factory=dict)

    def digest(self, path: str) -> str | None:
        entry = self.reads.get(path)
        return entry.digest if entry is not None else None


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
        reads = dict(base.reads)
        reads.pop(path, None)
        reads[path] = FileRead(digest=digest, mtime=float(mtime))
        return msgspec.structs.replace(base, reads=reads)
    if kind == KIND_CHANGED:
        path, digest = data.get("path"), data.get("digest")
        if not isinstance(path, str) or not isinstance(digest, str):
            return base
        return msgspec.structs.replace(base, noticed={**base.noticed, path: digest})
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
