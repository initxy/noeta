"""Private constants shared across the code-session read models."""

from __future__ import annotations

__all__ = [
    "_APPROVAL_HANDLE_PREFIX",
]


# Mirrors the ``f"approval-{call_id}"`` handle ``InteractionDriver`` suspends
# on. Duplicated as a local constant so these read-only projections stay off
# ``noeta.execution.driver``, which would drag the Engine host seam in.
_APPROVAL_HANDLE_PREFIX = "approval-"
