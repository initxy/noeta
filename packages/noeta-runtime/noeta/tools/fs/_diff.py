"""Unified-diff primitives shared by ``edit`` / ``write`` /
``apply_patch``.

These pure functions + the diff media-type constant compute the audit
artifact every fs write tool emits: a ``difflib`` unified diff (``a/`` ·
``b/`` framing), its ``+N/-M`` line-stat counts, and the before/after
sha256 hashes that go into ``ToolResult.output``. They live here so
``edit.py`` and ``patch.py`` share the *exact same* diff bytes — the
recorded artifact's hash must stay stable, so the output format must not
drift between the two write tools.

The function bodies are moved verbatim from ``edit.py`` (no behaviour
change); the public names are the new shared seam.
"""

from __future__ import annotations

import difflib
import hashlib


__all__ = [
    "DIFF_MEDIA_TYPE",
    "compute_diff",
    "diff_stat_counts",
    "file_hash",
]


#: Media type recorded for every diff artifact (ContentStore + I6 endpoint).
DIFF_MEDIA_TYPE = "text/x-diff"


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    """Return ``(added, removed)`` line counts (excluding the `+++`/`---` headers)."""
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed
