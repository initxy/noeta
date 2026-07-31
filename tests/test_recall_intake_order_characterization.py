"""Characterization goldens for the user-message intake recording ORDER.

The SDK-extensibility redesign
(``docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md``, D7,
Track A) re-expresses the built-in memory auto-recall as a
``reminder_provider`` on the ``turn_intake`` seam. Its **Risk** entry is
explicit: "Track-A seam ordering interacts with the existing attachment /
``goal_origin`` recording order — characterization test first (M3)."

This module pins that order **before** the seam moves. Two seams carry it today
(``noeta.execution.memory``):

* :func:`append_user_message_with_recall` — the intake seam: retrieval runs
  first (impure, reads the store now), then the **human turn** is recorded (with
  the caller's ``origin``, e.g. ``system`` for an MCP-prompt-expanded goal), then
  — only if there were hits — a single **recall follow-up** turn tagged
  ``origin="memory"``. No hits ⇒ exactly the plain-append bytes.
* :class:`RecallGoalPrelude` — the ``send_goal`` prelude wrapping that seam:
  **attachments** seed first (each its own ``origin="system"`` turn), then the
  goal-through-recall, then the ``activate_skills`` state patch last.

The characterization uses a recording stub ``engine`` that captures every
``append_user_message`` / ``apply_state_patch`` call in order (the seam is a
pure orchestration over the Engine's append/patch verbs, so a stub that records
the calls is a faithful and deterministic probe). A real ``MemoryStore`` with
one memory drives a genuine tier-1 recall hit so the recall follow-up's exact
text is pinned too.

Re-pin (regenerate goldens) with one command::

    UPDATE_SNAPSHOTS=1 uv run pytest \\
        tests/test_recall_intake_order_characterization.py -q -p no:cacheprovider
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from noeta.builtins.memory.impl.recall import (
    append_user_message_with_recall,
    memory_reminder_provider,
)
from noeta.builtins.memory.impl.store import MemoryStore
from noeta.execution.reminders import IntakeGoalPrelude
from noeta.protocols.messages import Block, MessageOrigin, TextBlock

from tests._snapshot import assert_snapshot, stable_json


class _RecordingEngine:
    """A stub ``Engine`` that records the intake verbs in call order.

    ``append_user_message`` / ``apply_state_patch`` are the only two Engine
    verbs the intake seams drive. Each returns the (opaque) task unchanged so
    the seam threads it as usual; the recorded ``ops`` list is the artifact
    under test — the exact ordered sequence of recordings the seam produces.
    """

    def __init__(self) -> None:
        self.ops: list[dict[str, object]] = []

    def append_user_message(
        self,
        task: Any,
        *,
        content: list[Block],
        lease_id: str,
        trace_id: Optional[str] = None,
        origin: Optional[MessageOrigin] = None,
    ) -> Any:
        self.ops.append(
            {
                "op": "append_user_message",
                "origin": origin,
                "text": "\n".join(
                    b.text for b in content if isinstance(b, TextBlock)
                ),
            }
        )
        return task

    def apply_state_patch(self, task: Any, *, patch: Any, lease_id: str) -> Any:
        self.ops.append(
            {
                "op": "apply_state_patch",
                "activate_skills": list(
                    getattr(patch, "activate_skills", ()) or ()
                ),
            }
        )
        return task


# A goal whose text names the memory by its slug — a tier-1 (by-name) recall
# hit, so the full body is loaded into the recall follow-up turn.
_GOAL_WITH_HIT = "Please run the deploy_process for me."
# A goal that matches no memory — the no-hit path (plain append, no follow-up).
_GOAL_NO_HIT = "What is the weather like today?"


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    """A one-memory store: slug ``deploy_process`` with a short body.

    The recorded recall text is the memory name + body (never the store path),
    so the golden is deterministic despite ``tmp_path`` varying per run.
    """
    (tmp_path / "deploy_process.md").write_text(
        "---\ndescription: how we deploy\n---\nRun make deploy.\n",
        encoding="utf-8",
    )
    return MemoryStore(root=tmp_path)


def test_intake_order_hit_with_system_origin(store: MemoryStore) -> None:
    """Recall hit + ``origin="system"`` goal: human turn (system) then the
    memory follow-up (memory), in that order."""
    engine = _RecordingEngine()
    append_user_message_with_recall(
        engine,  # type: ignore[arg-type]
        object(),
        content=[TextBlock(text=_GOAL_WITH_HIT)],
        lease_id="L1",
        store=store,
        origin="system",
    )
    assert_snapshot(
        "recall_intake_hit_system.txt", stable_json(engine.ops)
    )


def test_intake_order_no_hit_is_plain_append(store: MemoryStore) -> None:
    """No recall hit: exactly one plain human-turn append, no follow-up."""
    engine = _RecordingEngine()
    append_user_message_with_recall(
        engine,  # type: ignore[arg-type]
        object(),
        content=[TextBlock(text=_GOAL_NO_HIT)],
        lease_id="L1",
        store=store,
        origin=None,
    )
    assert_snapshot(
        "recall_intake_no_hit.txt", stable_json(engine.ops)
    )


def test_recall_goal_prelude_full_order(store: MemoryStore) -> None:
    """The full ``IntakeGoalPrelude`` order: attachments (system) -> goal
    (its origin) -> recall follow-up (memory) -> activate_skills patch last."""
    engine = _RecordingEngine()
    prelude = IntakeGoalPrelude(
        content=[TextBlock(text=_GOAL_WITH_HIT)],
        # The prelude takes the host-composed provider tuple, not a store —
        # the host binds the memory built-in's provider exactly like this,
        # recall first.
        providers=(memory_reminder_provider(store),),
        origin="system",
        attachment_texts=("attachment one", "attachment two"),
        activate_skills=("skill-a",),
    )
    prelude(engine, object(), lease_id="L1")
    assert_snapshot(
        "recall_intake_prelude_full.txt", stable_json(engine.ops)
    )


def test_prelude_order_invariants(store: MemoryStore) -> None:
    """Explicit ordering assertions, independent of the golden bytes.

    Pins the load-bearing sequence directly so a re-ordering during the
    Track-A seam move is caught as an ordering failure, not just a diff:
    attachments first (both ``system``), then the goal, then the ``memory``
    recall follow-up, then the state patch last.
    """
    engine = _RecordingEngine()
    prelude = IntakeGoalPrelude(
        content=[TextBlock(text=_GOAL_WITH_HIT)],
        # The prelude takes the host-composed provider tuple, not a store —
        # the host binds the memory built-in's provider exactly like this,
        # recall first.
        providers=(memory_reminder_provider(store),),
        origin="system",
        attachment_texts=("attachment one", "attachment two"),
        activate_skills=("skill-a",),
    )
    prelude(engine, object(), lease_id="L1")
    ops = engine.ops

    # 1-2: the two attachments, each an origin="system" append, in order.
    assert ops[0] == {
        "op": "append_user_message",
        "origin": "system",
        "text": "attachment one",
    }
    assert ops[1] == {
        "op": "append_user_message",
        "origin": "system",
        "text": "attachment two",
    }
    # 3: the goal itself carries the caller's origin (here "system").
    assert ops[2]["op"] == "append_user_message"
    assert ops[2]["origin"] == "system"
    assert ops[2]["text"] == _GOAL_WITH_HIT
    # 4: the recall follow-up is tagged origin="memory".
    assert ops[3]["op"] == "append_user_message"
    assert ops[3]["origin"] == "memory"
    # 5 (last): the skill-activation state patch, after every append.
    assert ops[-1] == {
        "op": "apply_state_patch",
        "activate_skills": ["skill-a"],
    }
    assert len(ops) == 5
