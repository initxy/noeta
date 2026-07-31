"""Multi-turn coding-session policy wrapper for the `noeta code chat` path.

:class:`MultiTurnReActPolicy` (Phase 4.5 I3) wraps any ``Policy`` so that on
non-final turns a ``FinishDecision`` — or a ``FailDecision``, so one bad turn
does not seal the conversation — becomes a
``YieldForHumanDecision`` whose handler emits
``TaskSuspended(wake_on=HumanResponseReceived(handle="…"))`` — the
existing Phase-1 wake-resume primitive. The wrapped policy never
sees the substitution; the Engine + fold never see a new event type.
Its ``final`` flag is **mutable** via :meth:`set_final` so the same
wrapper instance carries across turns of a single chat without
rebuilding the Engine / Policy / Composer (per the architect's review
note in #noeta:e6e863bb msg 67102bd3).

Layer: hoisted to ``noeta.execution``; imports only
``noeta.protocols`` (Decision shapes + Policy protocol). No imports from
``noeta.agent`` / ``noeta.core`` / ``noeta.runtime``.
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
    """Wrap a coding-session ``Policy`` for the multi-turn chat path.

    * ``final=True`` — pass every ``Decision`` straight through.
      ``FinishDecision`` produces a real ``TaskCompleted`` event.
    * ``final=False`` — a ``FinishDecision`` becomes a
      ``YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE,
      state_patch=finish.state_patch,
      assistant_message=finish.assistant_message)``, and a
      ``FailDecision`` becomes the same suspend carrying
      ``suspend_reason="turn_failed: <reason>"``. Every other Decision shape
      (tool_calls / yield_for_human / wait_timer / spawn_subtask) is untouched.

    **Why a failed turn suspends rather than terminates.** ``TaskFailed`` is a
    terminal event: it seals the ledger, so one transient fault — a provider
    5xx, a tool crash escalated to fail, a structured-output nudge budget spent
    — would throw away the whole conversation and every bit of context the human
    built up in it. For a chat-shaped product the correct reading of a failed
    turn is "*this turn* did not work, here is why, the task is parked until you
    decide what to do", which is exactly the suspend the wrapper already
    performs between ordinary turns. The human's next message resumes the same
    task; nothing else in the conversation is lost.

    **``retryable`` is deliberately not the gate.** The flag answers "would
    re-driving this same step help?", which is a question about the terminal
    path — and it is ``False`` for precisely the failures a human *can* rescue
    (``llm_error`` covers the transient provider 5xx; ``llm_empty_response``,
    ``react_max_steps_exceeded`` and a spent nudge budget all clear up when the
    person rephrases). Gating on it would keep terminating the motivating case.
    Once a turn is parked, the next input is a *new* turn rather than a re-drive,
    so the flag has no consumer on this path and is not carried forward; its
    reason text is, in ``TaskSuspended.reason``.

    ``final=True`` still terminates, because there is no next human turn to park
    for.

    The runner flips ``final`` via :meth:`set_final` BEFORE driving
    the final turn so the wrapper stays a singleton across the
    chat. Never re-construct it mid-session — the Composer + Engine
    cache state that depends on the policy instance the way the
    architect's note #67102bd3 spells out.
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
        """Toggle whether the next ``decide()`` returns
        ``FinishDecision`` verbatim (``True``) or translates it to a
        suspend (``False``). The runner flips this between turns."""
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

    # Forwarders so the wrapper passes structural ``Policy`` checks
    # and any attribute the inner policy exposes (e.g. tools) still
    # works from a debug perspective.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
