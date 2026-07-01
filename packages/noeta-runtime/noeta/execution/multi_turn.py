"""Multi-turn coding-session policy wrapper for the `noeta code chat` path.

:class:`MultiTurnReActPolicy` (Phase 4.5 I3) wraps any ``Policy`` so that on
non-final turns a ``FinishDecision`` becomes a
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
    FinishDecision,
    YieldForHumanDecision,
)
from noeta.protocols.policy import Policy
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View


__all__ = [
    "MultiTurnReActPolicy",
    "NEXT_GOAL_WAKE_HANDLE",
]


#: The ``handle`` that distinguishes a "waiting for next goal"
#: suspension from any other ``YieldForHumanDecision`` use. Read models
#: key on it to recognise a chat turn boundary in the EventLog history.
NEXT_GOAL_WAKE_HANDLE = "noeta-code-next-goal"


class MultiTurnReActPolicy:
    """Wrap a coding-session ``Policy`` for the multi-turn chat path.

    * ``final=True`` — pass every ``Decision`` straight through.
      ``FinishDecision`` produces a real ``TaskCompleted`` event.
    * ``final=False`` — when the inner policy returns
      ``FinishDecision``, return a
      ``YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE,
      state_patch=finish.state_patch,
      assistant_message=finish.assistant_message)`` instead. Every
      other Decision shape (tool_calls / fail / yield_for_human /
      wait_timer / spawn_subtask) is untouched.

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
        return result

    # Forwarders so the wrapper passes structural ``Policy`` checks
    # and any attribute the inner policy exposes (e.g. tools) still
    # works from a debug perspective.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
