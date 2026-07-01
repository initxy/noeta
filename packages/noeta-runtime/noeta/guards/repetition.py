"""``RepetitionGuard`` — break a stuck tool-call loop (work item ④).

A policy that keeps proposing the *same* tool with the *same* arguments is
almost always stuck (an LLM re-issuing an identical call that did not advance
the task). This guard watches the recent tool-call history and, once the same
**identity key** has appeared ``threshold`` times **consecutively** (the prior
run plus the proposed call), returns the configured action — by default
``require_approval`` (D-4: hand the decision to a human via the existing HITL
path), or ``deny`` for a more aggressive profile.

**Identity key** (D-4) is provider-neutral: ``(tool_name,
to_canonical_bytes(arguments))``. ``to_canonical_bytes`` sorts keys and uses
compact separators, so ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` collapse
to the same key, and the comparison never depends on any provider's wire shape.

**Determinism**: the verdict is a pure function of the
policy and the recorded history (``GuardContext.recent_tool_calls``, which the
Engine folds from the recorded ``ToolCallStarted`` prefix). No clock, no
random, no external state — so a resume re-derives the identical verdict, and
the same input reproduces the same guard-origin event (``ToolCallDenied`` or
the approval suspend) across hosts, as long as the same-parameter guard is
registered.

The guard only watches ``ProposedToolCall``; spawns and finishes are out of
scope (a loop is a *tool* phenomenon).

Priority 30 sits between ``PermissionGuard`` (20) and ``HookGuard`` (100): a
hard allow/deny decision precedes this "suspected loop" heuristic, but the
heuristic precedes user PreToolUse hooks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedToolCall,
    VerdictResult,
)


__all__ = ["RepetitionAction", "RepetitionGuard", "RepetitionPolicy"]


#: D-4b — what to do once the loop is detected. ``require_approval`` (default)
#: routes through the existing HITL approval suspend; ``deny`` fails the call
#: closed (with a model-visible result so the model can self-correct).
RepetitionAction = Literal["require_approval", "deny"]


@dataclass(frozen=True, slots=True)
class RepetitionPolicy:
    """Tuning for :class:`RepetitionGuard`.

    ``threshold`` is the number of **consecutive** identical calls (counting
    the proposed one) that trips the action. ``threshold <= 0`` disables the
    guard entirely (a defensive off switch). ``window`` bounds how far back the
    Engine scans when building the history; it is an upper bound on the
    consecutive run the guard can observe.
    """

    #: Consecutive identical-call count (incl. the proposed call) that trips.
    #: Default 3 per D-4. ``<= 0`` disables the guard.
    threshold: int = 3
    #: Action on trip. Default ``require_approval`` per D-4 (decision to human).
    action: RepetitionAction = "require_approval"
    #: How many recent tool calls the Engine should fold into the history. Must
    #: be ``>= threshold`` for the guard to ever trip; defaulted generously.
    window: int = 8


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

        # Count the unbroken run of this exact key at the tail of the recorded
        # history, then add 1 for the proposed call.
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
