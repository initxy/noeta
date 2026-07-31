"""StepTransition — the judgement tag for *why* a step had a next step.

Only *non-default* continuations are worth recording: an approval resolved, a
transient error retried, a context overflow recovered, output that hit
``max_tokens``, a compaction retried. Tagging those at their deterministic
emission point lets recovery guards read ``RuntimeState.last_transition`` in
O(1) instead of accreting branch logic inside the Engine body. The vocabulary
is provider-neutral by construction — ``overflow_recovery`` /
``max_output_recovery`` are Noeta-shape semantic labels, never vendor error
codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: The locked continuation vocabulary. ``next_turn`` is the implicit default
#: and is never emitted as an event; the remaining five are the non-default
#: continuations that DO get a ``StepTransitionMarked`` event.
TransitionReason = Literal[
    "next_turn",
    "approval_resume",
    "transient_retry",
    "overflow_recovery",
    "max_output_recovery",
    "compaction_retry",
]

#: The same vocabulary as a runtime value — a ``Literal`` is a typing
#: construct only, so drift checks and docs need this tuple to read it.
TRANSITION_REASONS: tuple[TransitionReason, ...] = (
    "next_turn",
    "approval_resume",
    "transient_retry",
    "overflow_recovery",
    "max_output_recovery",
    "compaction_retry",
)


@dataclass(frozen=True, slots=True)
class StepTransition:
    """A typed continuation tag.

    ``attempt`` is the same-reason re-entry counter an anti-spiral guard reads
    to tell one recovery from a loop of them. Frozen so a recorded transition
    cannot be mutated after the fact.
    """

    reason: TransitionReason
    attempt: int = 0
