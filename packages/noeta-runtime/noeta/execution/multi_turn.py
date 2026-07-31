"""The multi-turn conversation wrapper: on a non-final turn it rewrites a
terminal ``Decision`` into a suspend that parks the task for the next human
message, reusing the wake-resume primitive rather than adding an event type.

The wrapped policy never sees the substitution, and the Engine and fold see
nothing new. ``final`` is mutable so one wrapper instance carries across every
turn of a conversation without rebuilding the Engine / Policy / Composer.
"""

from __future__ import annotations

from typing import Any

from noeta.protocols.decisions import (
    Decision,
    FailDecision,
    FinishDecision,
    YieldForHumanDecision,
)
from noeta.protocols.policy import Policy
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View
from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE


__all__ = [
    "MultiTurnReActPolicy",
    "NEXT_GOAL_WAKE_HANDLE",
    "TURN_FAILED_SUSPEND_TAG",
]


#: ``TaskSuspended.reason`` tag for a turn that failed and was parked for the
#: human instead of terminating the conversation. The full policy reason follows
#: after ``": "``, so a host can both branch on the tag and show the detail.
TURN_FAILED_SUSPEND_TAG = "turn_failed"


class MultiTurnReActPolicy:
    """Wrap a ``Policy`` for the multi-turn conversation path.

    With ``final=False`` a ``FinishDecision`` and a ``FailDecision`` both become
    the next-goal suspend (the failure carrying
    ``suspend_reason="turn_failed: <reason>"``); every other Decision shape
    passes through untouched. With ``final=True`` everything passes through,
    because there is no next human turn to park for.

    **Why a failed turn suspends rather than terminates.** ``TaskFailed`` seals
    the ledger, so one transient fault — a provider 5xx, a tool crash escalated
    to fail, a spent structured-output nudge budget — would throw away the whole
    conversation and every bit of context the human built up in it. The correct
    reading of a failed turn is "*this turn* did not work, here is why, the task
    is parked until you decide what to do": the human's next message resumes the
    same task and nothing else is lost.

    **``retryable`` is deliberately not the gate.** It answers "would re-driving
    this same step help?", and it is ``False`` for precisely the failures a human
    *can* rescue (``llm_error``, ``llm_empty_response``,
    ``react_max_steps_exceeded``, a spent nudge budget — all of which clear up
    when the person rephrases). Once a turn is parked the next input is a *new*
    turn rather than a re-drive, so the flag has no consumer here; its reason
    text survives in ``TaskSuspended.reason``.

    Never re-construct the wrapper mid-conversation: the Composer and Engine
    cache state keyed to the policy instance. Flip :meth:`set_final` instead.
    """

    def __init__(
        self,
        inner: Policy,
        *,
        final: bool = True,
        wake_handle: str = NEXT_GOAL_WAKE_HANDLE,
    ) -> None:
        self._inner = inner
        self._final = bool(final)
        self._wake_handle = wake_handle

    @property
    def final(self) -> bool:
        return self._final

    def set_final(self, final: bool) -> None:
        """Toggle terminal-Decision pass-through; flipped between turns, and set
        before driving the last turn of a conversation."""
        self._final = bool(final)

    def decide(self, ctx: StepContext, view: View) -> Decision:
        result = self._inner.decide(ctx, view)
        if self._final:
            return result
        if isinstance(result, FinishDecision):
            return YieldForHumanDecision(
                prompt=self._wake_handle,
                state_patch=result.state_patch,
                assistant_message=result.assistant_message,
            )
        if isinstance(result, FailDecision):
            return YieldForHumanDecision(
                prompt=self._wake_handle,
                state_patch=result.state_patch,
                assistant_message=result.assistant_message,
                suspend_reason=f"{TURN_FAILED_SUSPEND_TAG}: {result.reason}",
            )
        return result

    # Forwarding keeps the wrapper passing structural ``Policy`` checks and
    # keeps any attribute the inner policy exposes reachable through it.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
