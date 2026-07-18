"""Feedback reference materialization: turn "the correct result / finalized
artifact" into a stable markdown snapshot.

The reference is key evidence for strong attribution; its semantics are "the
version at the moment of submission", so it is materialized to disk
**at submit time**:
- Pasted text: written to the snapshot verbatim.

Snapshot path convention (never stored in the DB):
DATA_DIR/feedback/<space_id>/<feedback_id>/reference.md.
"""
from __future__ import annotations

import logging
from pathlib import Path

from noeta.agent.config import Settings

logger = logging.getLogger(__name__)

#: Snapshot size cap (characters): defensive truncation; the analysis
#: agent's read_reference has its own paging.
_MAX_SNAPSHOT_CHARS = 200_000


class ReferenceError(Exception):
    """Reference materialization failed (user-readable message; the API layer
    passes it through as the 4xx detail)."""


def reference_path(settings: Settings, space_id: str, feedback_id: str) -> Path:
    """Path of the reference snapshot file (derived by convention, see the
    module docstring)."""
    return settings.data_path / "feedback" / space_id / feedback_id / "reference.md"


class FeedbackReferenceService:
    def __init__(self, settings: Settings, auth_provider=None) -> None:
        self._settings = settings
        # Auth seam kept for wiring compatibility: the pasted-text path needs
        # no external identity.
        self._auth_provider = auth_provider

    # ------------------------------------------------------------- entry points

    def materialize_text(
        self, space_id: str, feedback_id: str, text: str
    ) -> Path:
        """Pasted-text reference: written to the snapshot verbatim."""
        body = text.strip()
        if not body:
            raise ReferenceError("reference content is empty")
        return self._write(space_id, feedback_id, body)

    def read(self, space_id: str, feedback_id: str) -> str | None:
        path = reference_path(self._settings, space_id, feedback_id)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    # ------------------------------------------------------------- internals

    def _write(self, space_id: str, feedback_id: str, content: str) -> Path:
        if len(content) > _MAX_SNAPSHOT_CHARS:
            content = content[:_MAX_SNAPSHOT_CHARS] + "\n\n… (snapshot too long; truncated)"
        path = reference_path(self._settings, space_id, feedback_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
