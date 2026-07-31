"""Snapshot body serialization: a Task's full 4-slice state as a portable dict.

Round-trips through :mod:`noeta.protocols.canonical` so tagged value types
(ContentRef, WakeCondition variants, SubtaskResult) keep their identity across
the snapshot boundary. Canonical JSON (sort_keys, compact separators) is
mandatory: resume refolds the same prefix and must land on the same content
address.
"""

from __future__ import annotations

from typing import Any

from noeta.protocols.canonical import (
    from_canonical_bytes,
    restore_dataclass,
    to_canonical_bytes,
)
from noeta.protocols.task import (
    ContextState,
    GovernanceState,
    RuntimeState,
    Task,
    TaskState,
)


_MEDIA_TYPE = "application/json"

# Consecutive ``tool_calls`` decisions the Engine may take before it writes a
# mid-loop ``TaskSnapshot`` and keeps running. Without it a Policy that never
# yields to a non-tool branch would leave no resume point for the whole run.
CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD = 20


def serialize_task_state(task: Task) -> bytes:
    return to_canonical_bytes(task.state_dict())


def deserialize_task_state(body: bytes) -> dict[str, Any]:
    state_dict: dict[str, Any] = from_canonical_bytes(body)
    return state_dict


def rehydrate_task(state_dict: dict[str, Any]) -> Task:
    """Stitch a deserialized state dict back into the typed slice dataclasses.

    The canonical layer has already restored the tagged values (ContentRef in
    ``context.plan_ref``, SubtaskResult in ``governance.subtask_results``,
    WakeCondition in ``wake_on``).
    """
    state = dict(state_dict["state"])
    if state.get("active_skills") and "active_content" not in state:
        # A body that carries only the skill sugar list seeds the generic
        # activation map from it, so an accelerated fold rebuilds the same
        # state a from-scratch fold derives from the full stream. No per-skill
        # hash is available; "" is fine because skills are pinned — the
        # renderer keys off the registry, not the hash.
        state["active_content"] = {
            "skill": {name: "" for name in state["active_skills"]}
        }
    return Task(
        task_id=state_dict["task_id"],
        status=state_dict["status"],
        parent_task_id=state_dict.get("parent_task_id"),
        subtask_depth=state_dict.get("subtask_depth", 0),
        runtime=RuntimeState(**state_dict["runtime"]),
        state=TaskState(**state),
        context=ContextState(**state_dict["context"]),
        # ``restore_dataclass`` (not ``**``) so a body carrying governance keys
        # this build does not declare rehydrates instead of crashing on an
        # unexpected keyword.
        governance=restore_dataclass(GovernanceState, state_dict["governance"]),
        wake_on=state_dict.get("wake_on"),
    )


def snapshot_media_type() -> str:
    return _MEDIA_TYPE
