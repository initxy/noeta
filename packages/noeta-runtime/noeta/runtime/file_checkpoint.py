"""Per-turn gate on which workspace files already carry a rewind baseline.

A runtime accelerator only: the authoritative baselines are the
``file_baselines`` on the ``ToolResultRecorded`` events, and this gate is
never written to the log — it only spares the live runtime a re-fold to learn
a file was already stashed this turn. Keyed by the ROOT task so a whole
delegation tree shares ONE gate: were a subtask to stash a second baseline for
a file its parent already edited, that baseline would pin mid-turn (dirty)
content instead of the turn's starting state. Clearing it at every turn
boundary is what lets a rewind restore to any turn boundary.
"""

from __future__ import annotations

import threading


class FileCheckpointRegistry:
    """Thread-safe per-turn set of already-baselined file paths, by root task."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # root task id -> workspace-relative paths baselined this turn.
        self._seen: dict[str, set[str]] = {}

    def mark_if_first(self, root_task_id: str, path: str) -> bool:
        """Record ``path`` as baselined this turn; True ⇒ the caller must
        stash the baseline.

        Test and set are one atomic step under the lock: two threads racing
        the same file's first edit must not both stash a baseline, or the
        loser overwrites the turn's starting state with dirty content."""
        key = str(root_task_id)
        with self._lock:
            seen = self._seen.setdefault(key, set())
            if path in seen:
                return False
            seen.add(path)
            return True

    def reset_turn(self, root_task_id: str) -> None:
        """Clear ``root_task_id``'s baselined paths at a turn boundary so the
        next turn stashes fresh ones. Idempotent."""
        with self._lock:
            self._seen.pop(str(root_task_id), None)
