"""Pin the kernel ``Decision`` union to the neutral set.

CONTEXT.md pin the runtime to exactly the 7 neutral Decision
kinds — ``tool_calls`` / ``spawn_subtask`` / ``yield_for_human`` /
``wait_timer`` / ``wait_external`` / ``finish`` / ``fail`` — plus three
neutral generalizations that carry NO product meaning:

* ``SpawnSubtasksDecision`` — the fan-out of ``spawn_subtask``.
* ``StatePatchDecision`` — the loop-continuing state-write member of the
  ``tool_calls`` family.
* ``CompactionRequestedDecision`` — ③ (README D-3): the loop-continuing
  memory-management member. Compaction is a neutral MECHANISM (prune +
  summarize a long history), not a Claude-Code product control tool, so it
  belongs in the kernel union (mechanism-vs-material). The policy
  authors WHEN to compact + the summary; the kernel mechanically prunes /
  records the result.

No Claude-Code product control-tool Decision (TodoWrite / PlanMode /
AskUserQuestion) may live in the kernel (mechanism-vs-material):
those effects are re-expressed by noeta-sdk through the neutral channels
(``StatePatchDecision`` + ``state_patch`` for todo/plan, ``yield_for_human``
for ask).
"""

from __future__ import annotations

import typing

import noeta.protocols.decisions as decisions_mod
from noeta.protocols.decisions import (
    CompactionRequestedDecision,
    Decision,
    FailDecision,
    FinishDecision,
    SpawnSubtaskDecision,
    SpawnSubtasksDecision,
    StatePatchDecision,
    ToolCallsDecision,
    WaitExternalDecision,
    WaitTimerDecision,
    YieldForHumanDecision,
)


_EXPECTED = {
    FinishDecision,
    FailDecision,
    ToolCallsDecision,
    SpawnSubtaskDecision,
    SpawnSubtasksDecision,
    YieldForHumanDecision,
    WaitTimerDecision,
    WaitExternalDecision,
    StatePatchDecision,
    CompactionRequestedDecision,
}


def test_decision_union_is_exactly_the_neutral_set() -> None:
    assert set(typing.get_args(Decision)) == _EXPECTED


def test_no_product_control_tool_decisions_in_kernel() -> None:
    for name in (
        "TodoWriteDecision",
        "PlanModeDecision",
        "AskUserQuestionDecision",
    ):
        assert not hasattr(decisions_mod, name), (
            f"{name} must not exist in noeta.protocols.decisions "
            "(product control tools are owned by noeta-sdk)"
        )
