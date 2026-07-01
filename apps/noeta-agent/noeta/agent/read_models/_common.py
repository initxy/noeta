"""read_models._common — private constants shared across code-session read models."""

from __future__ import annotations

__all__ = [
    "_APPROVAL_HANDLE_PREFIX",
]


# Approval-suspend handle convention — mirrors ``InteractionDriver``'s
# ``f"approval-{call_id}"``. Kept as a local constant so this read-only module
# need not import ``noeta.agent.driver`` (which pulls the resolver/Engine host
# seam). The next-goal handle is the canonical constant from the policy module.
_APPROVAL_HANDLE_PREFIX = "approval-"
