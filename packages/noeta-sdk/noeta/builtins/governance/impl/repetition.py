"""``RepetitionGuard`` — break a stuck tool-call loop.

A policy re-proposing the *same* tool with the *same* arguments is almost
always stuck, so the identity key ``(tool_name,
to_canonical_bytes(arguments))`` is compared instead of the raw arguments:
canonical bytes sort keys, which keeps the comparison independent of any
provider's wire shape.

The verdict is a pure function of the policy and the recorded history
(``GuardContext.recent_tool_calls``, folded from the ``ToolCallStarted``
prefix) — no clock, no random, no external state — so a resume with the same
guard registered re-derives the identical guard-origin event.

Priority 30 sits between ``PermissionGuard`` (20) and ``HookGuard`` (100): a
hard allow/deny decision precedes this "suspected loop" heuristic, and the
heuristic precedes user PreToolUse hooks. Only ``ProposedToolCall`` is
watched — a loop is a *tool* phenomenon.
"""

from __future__ import annotations

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedToolCall,
    VerdictResult,
)
from noeta.runtime.governance import RepetitionPolicy


__all__ = ["RepetitionGuard"]


class RepetitionGuard:
    """Synchronous loop-detection Guard. Returns the configured action once a
    run of identical ``(tool_name, arguments)`` calls reaches the threshold;
    otherwise ``ALLOW``."""

    name = "repetition"
    priority = 30

    def __init__(self, policy: RepetitionPolicy) -> None:
        self._policy = policy

    def check(
        self, action: ProposedAction, ctx: GuardContext
    ) -> VerdictResult:
        if not isinstance(action, ProposedToolCall):
            return VerdictResult.allow()

        threshold = self._policy.threshold
        if threshold <= 0:
            return VerdictResult.allow()

        key = (
            action.call.tool_name,
            to_canonical_bytes(action.call.arguments),
        )

        # The threshold counts the unbroken tail run plus the proposed call,
        # so a non-matching entry anywhere in between resets it.
        prior_run = 0
        for entry in reversed(ctx.recent_tool_calls):
            if entry == key:
                prior_run += 1
            else:
                break
        consecutive = prior_run + 1

        if consecutive < threshold:
            return VerdictResult.allow()

        reason = (
            f"tool {action.call.tool_name!r} called with identical arguments "
            f"{consecutive} times in a row (repetition threshold "
            f"{threshold}); suspected loop"
        )
        if self._policy.action == "deny":
            return VerdictResult.deny(reason)
        return VerdictResult.require_approval(reason)
