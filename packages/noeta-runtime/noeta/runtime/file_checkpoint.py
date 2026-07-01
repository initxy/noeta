"""Per-turn file-checkpoint gate.

A live, in-process record of "which workspace files have already had a rewind
baseline stashed THIS turn", keyed by the session root task id. The first time
the AI edits a file in a turn, :meth:`ToolRuntime` stashes that file's pre-edit
content as the turn's baseline and marks the path here; every later edit of the
same file in the same turn is a no-op (the baseline already pins the turn's
starting state).

This is a RUNTIME accelerator only — exactly the property
:class:`noeta.runtime.cancellation.CancellationRegistry` and
:class:`noeta.runtime.background_shell.ProcessRegistry` carry. The AUTHORITATIVE
record of a turn's baselines is the ``file_baselines`` field on the
``ToolResultRecorded`` events; this gate just lets the live runtime avoid
re-stashing the same file twice in one turn without re-folding the log. It is
**never written to the log**, so it has zero effect on the persisted record.

Keyed by the SESSION ROOT (not the editing task) so the subtask
cascade can share ONE gate across a whole delegation tree: a parent that edited
X, then a subtask that edits the same X, must not stash a SECOND (mid-turn,
dirty) baseline for X. v1 subtasks run same-process / sequential / one lease at
a time, so a plain ``threading.Lock`` is enough — the SSE host
drives turns on background threads, so the lock is not merely defensive.

``reset_turn`` is called by the driver at every turn boundary (each new user
goal) so a NEW turn re-stashes a fresh baseline for any file it touches — this
is the "clear every turn" rule D6 depends on for restoring to any turn boundary.
"""

from __future__ import annotations

import threading


class FileCheckpointRegistry:
    """Thread-safe per-turn set of already-baselined file paths, by root task."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # session-root task id -> set of workspace-relative paths already
        # baselined this turn.
        self._seen: dict[str, set[str]] = {}

    def mark_if_first(self, root_task_id: str, path: str) -> bool:
        """Record ``path`` as baselined this turn; return whether it was the
        FIRST time this turn (so the caller stashes a baseline).

        Atomic test-and-set under the lock: a miss returns ``True`` AND marks
        the path, so two threads racing the same file's first edit cannot both
        stash a baseline."""
        key = str(root_task_id)
        with self._lock:
            seen = self._seen.setdefault(key, set())
            if path in seen:
                return False
            seen.add(path)
            return True

    def reset_turn(self, root_task_id: str) -> None:
        """Clear the baselined-paths set for ``root_task_id`` (turn boundary).

        Idempotent; an unknown root is a clean no-op."""
        with self._lock:
            self._seen.pop(str(root_task_id), None)
