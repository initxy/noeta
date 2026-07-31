"""Deterministic test Policies: choreograph an Engine loop without an LLM.

An exhausted ``StubScriptedPolicy`` raises ``IndexError`` so a runaway loop
surfaces loudly instead of silently re-running the last step.
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
