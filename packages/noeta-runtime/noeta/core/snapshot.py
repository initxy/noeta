"""Snapshot body serialization.

A Snapshot body is the full 4-slice state of a Task as a portable dict.
Serialization round-trips through :mod:`noeta.protocols.canonical` so
tagged value types (ContentRef, WakeCondition variants, SubtaskResult)
keep their identity across the snapshot boundary.

Canonical JSON (sort_keys, compact separators) is required so the
content hash is stable across runs — a hard prerequisite for resume:
a refold of the same prefix must reproduce the same content address.
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

# Mid-loop snapshot trigger:
# when the Engine has spent this many consecutive ``tool_calls`` decisions
# without yielding to a non-tool branch, it writes a ``TaskSnapshot`` and
# keeps running. The threshold is exposed here so callers can configure
# it from a single canonical location; the spec default is 20.
CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD = 20


def serialize_task_state(task: Task) -> bytes:
    """Produce the canonical bytes that go into ContentStore."""
    return to_canonical_bytes(task.state_dict())


def deserialize_task_state(body: bytes) -> dict[str, Any]:
    """Inverse of ``serialize_task_state`` for state-dict round-trips."""
    state_dict: dict[str, Any] = from_canonical_bytes(body)
    return state_dict


def rehydrate_task(state_dict: dict[str, Any]) -> Task:
    """Rebuild a ``Task`` object from a deserialized state dict.

    Canonical layer already restored tagged values (ContentRef inside
    ``context.plan_ref``, SubtaskResult inside ``governance.subtask_results``,
    WakeCondition in ``wake_on``); we just stitch them back into the
    typed slice dataclasses.
    """
    state = dict(state_dict["state"])
    if state.get("active_skills") and "active_content" not in state:
        # D2: a snapshot body written before the
        # generic activation map existed carries only the skill sugar
        # list. Seed the map's skill entry from it so an accelerated fold
        # rebuilds the same state a from-scratch fold derives from the
        # full stream (the sugar keeps the two in lockstep thereafter).
        state["active_content"] = {"skill": list(state["active_skills"])}
    return Task(
        task_id=state_dict["task_id"],
        status=state_dict["status"],
        parent_task_id=state_dict.get("parent_task_id"),
        subtask_depth=state_dict.get("subtask_depth", 0),
        runtime=RuntimeState(**state_dict["runtime"]),
        state=TaskState(**state),
        context=ContextState(**state_dict["context"]),
        # ``restore_dataclass`` (not ``**``) so an old snapshot body that still
        # carries the retired ``agent_fingerprint`` / ``host_config_fingerprint``
        # / ``registry_fingerprint`` governance keys rehydrates instead of
        # crashing on an unexpected keyword (R1 tolerance).
        governance=restore_dataclass(GovernanceState, state_dict["governance"]),
        wake_on=state_dict.get("wake_on"),
    )


def snapshot_media_type() -> str:
    return _MEDIA_TYPE
