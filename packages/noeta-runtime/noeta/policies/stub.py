"""Deterministic test Policies for Phase 0.

* ``StubFinishPolicy`` — finishes on first call. Issue 01.
* ``StubScriptedPolicy`` — pops the next Decision from a predetermined
  sequence on each ``decide``. Issue 02 uses this to choreograph
  ``tool_calls → finish`` style scripts in integration tests without a
  real LLM. Exhausted scripts raise ``IndexError`` so a runaway Engine
  loop surfaces loudly rather than silently re-running the last step.
"""

from __future__ import annotations

from typing import Any, Sequence

from noeta.protocols.decisions import Decision, FinishDecision
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View


class StubFinishPolicy:
    def __init__(self, answer: Any = "ok") -> None:
        self._answer = answer

    def decide(
        self, ctx: StepContext, view: View  # noqa: ARG002
    ) -> FinishDecision:
        return FinishDecision(answer=self._answer)


class StubScriptedPolicy:
    """Pops one Decision per ``decide`` call from a fixed script."""

    def __init__(self, decisions: Sequence[Decision]) -> None:
        self._remaining: list[Decision] = list(decisions)

    def decide(
        self, ctx: StepContext, view: View  # noqa: ARG002
    ) -> Decision:
        if not self._remaining:
            raise IndexError("StubScriptedPolicy script exhausted")
        return self._remaining.pop(0)
