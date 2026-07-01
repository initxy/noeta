"""Golden snapshot of a preset's **composed View** — the assembled prompt.

This is the composer-side companion to ``test_prompt_snapshot.py``. That suite
pins the *static* spec inputs (``spec.instructions`` + the tool schemas); it
never runs the composer, so it cannot catch drift introduced by the
**assembly** step itself:

* **control-tool schema injection** — ``spawn_subagent`` / ``todo_write`` /
  ``ask_user_question`` (and friends) are NOT executable workspace tools; the
  composer appends them to ``View.provider_tool_schemas`` after the real tools
  (``noeta.execution.builder._build_control_action_schemas``). A flag flip or a
  re-ordering there is invisible to the static-spec snapshot but visible here.
* **three-segment assembly** — the system prompt becomes ``stable_prefix``, the
  content channel renders ``semi_stable``, the message stream becomes
  ``dynamic_suffix``, and each segment carries a ``segment_hash``. A change to
  how the segments are cut or hashed shows up as a golden diff here.

The View is produced through the real construction path
(``noeta.execution.builder.build_session_inputs`` →
``ThreeSegmentComposer.compose``) for each preset, fed a fixed minimal Task
(one fixed user message, no skills / memory / environment activated). The
composer is a pure function — it never calls the LLM — so this exercises the
true assembly without a network round-trip.

Determinism (verified by running the suite twice — both PASS):

* ``model="stub-model"`` keeps the catalog-driven compaction OFF, so no
  per-model token math enters the bytes.
* The Task activates no content residents, so ``semi_stable`` is empty — the
  workspace-environment resident (which would carry the absolute workspace
  path + platform) never renders. The per-call temp ``workspace_dir`` therefore
  does **not** leak into the View; a probe confirmed the canonical bytes of the
  segments + schemas are byte-identical across two builds with different temp
  workspaces. No normalization is needed.
* The View is serialized with ``to_canonical`` (the same deterministic,
  key-sorted, object-id-free encoder used everywhere else), so no Python object
  ids / addresses / timestamps reach the golden.

Coverage: the **complete three segments** (content + per-segment hash) plus the
full ``provider_tool_schemas`` list — i.e. the entire model-visible composed
surface, control-tool schemas included.

Re-pin (regenerate goldens) with one command::

    UPDATE_SNAPSHOTS=1 uv run pytest \\
        tests/test_composer_view_snapshot.py -q -p no:cacheprovider
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
from noeta.guards.budget import Budget
from noeta.presets import official_specs
from noeta.protocols.canonical import to_canonical
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.task import Task
from noeta.storage.memory import InMemoryContentStore

from tests._snapshot import assert_snapshot, stable_json


# Presets whose composed View is snapshotted. ``general-purpose`` is omitted:
# its tool set is identical to ``main`` and it injects no control schemas, so
# its View adds no assembly coverage the others don't already give.
_PRESETS = ("main", "explore", "plan")

# The fixed minimal Task fed to every composer: one fixed goal carried as a
# single user message. Constant so the dynamic_suffix bytes are stable.
_FIXED_GOAL = "Fixed goal: say hi"


def _compose_view_payload(preset: str) -> dict[str, object]:
    """Build ``preset``'s composer through the real assembly path and compose a
    fixed minimal Task, returning the stable serialization of the View.

    Wiring mirrors ``official_specs()[preset].capabilities`` so the control-tool
    schema injection matches what the live session would emit (delegation /
    todo_write / ask_user_question / skill_invocation flags + the spawnable
    sub-agent directory). ``model="stub-model"`` keeps compaction off and the
    tool schemas free of any provider-edit drop.
    """
    spec = official_specs()[preset]
    caps = spec.capabilities
    allowed = frozenset(t.name for t in spec.tools)

    # A throwaway temp workspace: nothing is read from it (no skills/memory),
    # and the probe confirmed its absolute path never reaches the composed
    # bytes — so no path normalization is required for determinism.
    workspace = Path(tempfile.mkdtemp(prefix="composer_view_snapshot_"))
    content_store = InMemoryContentStore()

    inputs = build_session_inputs(
        workspace_dir=workspace,
        system_prompt=spec.instructions,
        allowed_tools=allowed,
        content_store=content_store,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        allowed_subtask_agents=frozenset(caps.spawnable),
        delegation_enabled=caps.delegation,
        todo_write_enabled=caps.todo_write,
        ask_user_question_enabled=caps.ask_user_question,
        skill_invocation_enabled=caps.skill_invocation,
        # The spawn_subagent control schema embeds the sub-agent directory
        # (name + description). Descriptions are pinned by test_prompt_snapshot;
        # here we pass empty descriptions so this golden stays focused on the
        # injection + ordering, not the descriptive prose.
        subtask_agent_directory=tuple((name, "") for name in caps.spawnable),
    )

    task = Task(task_id="t-fixed")
    task.runtime.messages = [
        Message(role="user", content=[TextBlock(text=_FIXED_GOAL)])
    ]
    view = inputs.composer.compose(task)

    return {
        "preset": preset,
        # The complete three segments: content (canonicalized — no object ids)
        # and the per-segment hash.
        "segments": [
            {
                "name": segment.name,
                "segment_hash": segment.segment_hash,
                "content": [to_canonical(message) for message in segment.content],
            }
            for segment in view.segments
        ],
        # The full provider tool surface: real executable tools followed by the
        # injected control-action schemas (spawn_subagent / todo_write / ...).
        "provider_tool_schemas": view.provider_tool_schemas,
    }


@pytest.mark.parametrize("preset", _PRESETS)
def test_composed_view_snapshot(preset: str) -> None:
    """The preset's composed View (three segments + provider tool schemas,
    control schemas included) matches its golden."""
    payload = stable_json(_compose_view_payload(preset))
    assert_snapshot(f"composer_view_{preset}.txt", payload)
