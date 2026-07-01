"""StepTransition — the judgement tag for *why* a step had a next step.

Foundation B (README D-B1..D-B6). Each step in the Engine's run loop continues
for a reason. Most of the time that reason is the implicit default
(``next_turn`` — the LLM asked for tool calls, we ran them, loop again),
which is **not** worth a recorded event (D-B2). The interesting cases are
the *non-default* continuations — an approval was resolved, a transient
error is being retried, a context overflow is being recovered, the output
hit ``max_tokens``, or a compaction is being retried. Tagging those at
their deterministic emission point lets the later recovery guards
(② error recovery, ④ RepetitionGuard) be written as O(1) reads of
``RuntimeState.last_transition`` instead of accreting branch logic inside
the Engine body (which is held to a ≤500-line budget).

This module sits at L0 (``noeta.protocols``) and imports only the standard
library so it can be shared by every layer above without violating the
import-linter topology. It pins a provider-neutral vocabulary:
``overflow_recovery`` / ``max_output_recovery`` are Noeta-shape
semantic labels, never a vendor error code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: The locked continuation vocabulary. ``next_turn`` is the implicit
#: default (never emitted as an event, D-B2); the remaining five are the
#: non-default continuations that DO get a ``StepTransitionMarked`` event.
#: ``transient_retry`` / ``overflow_recovery`` / ``max_output_recovery`` /
#: ``compaction_retry`` are reserved for ②/③ — only ``approval_resume`` is
#: wired by Foundation B itself (the one real non-default resume that exists today).
TransitionReason = Literal[
    "next_turn",
    "approval_resume",
    "transient_retry",
    "overflow_recovery",
    "max_output_recovery",
    "compaction_retry",
]

#: Runtime-introspectable tuple of the same vocabulary (a ``Literal`` is a
#: typing construct, not a value), handy for tests / docs / drift checks.
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

    ``reason`` is one of :data:`TransitionReason`. ``attempt`` is reserved
    for ②'s retry ladder (a same-reason re-entry counter the anti-spiral
    guard can read); it defaults to 0 and Foundation B never increments it, but
    defining it now keeps the byte shape stable for ② (additive, no
    ``schema_version`` bump). Frozen so a recorded transition cannot be
    mutated after the fact.
    """

    reason: TransitionReason
    attempt: int = 0
