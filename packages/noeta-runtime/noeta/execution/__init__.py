"""The in-process agent execution machine: session construction, the interaction
driver, multi-turn policy wrapping, engine resolution, and the delegation drain.

Product-agnostic by contract — this package may import the lower layers plus the
identity layer (``noeta.agent.spec`` / ``noeta.agent.registry``) and nothing
above it, enforced by the import-linter layered topology.
"""

from __future__ import annotations

from noeta.execution.driver import (
    InteractionDriver,
    ModelBindPrelude,
    ModelSelectorError,
    NotResumableError,
    ProviderSelectorError,
    TaskAlreadyTerminalError,
    UnknownTaskError,
    multi_turn_policy_wrapper,
)
from noeta.execution.host import (
    AgentRegistryProtocol,
    ResidentHost,
)
from noeta.execution.builder import (
    COMPACTION_OFF,
    CompactionConfig,
    SessionInputs,
    build_session_inputs,
)
from noeta.execution.multi_turn import (
    MultiTurnReActPolicy,
    NEXT_GOAL_WAKE_HANDLE,
)
from noeta.execution.resolver import (
    GenericEngineResolver,
    agent_name_of,
)
from noeta.execution.subtask_drain import (
    DrainHost,
    UnsupportedSubtaskSuspend,
    drive_pending_subtasks,
)

__all__ = [
    "agent_name_of",
    "AgentRegistryProtocol",
    "build_session_inputs",
    "COMPACTION_OFF",
    "CompactionConfig",
    "DrainHost",
    "drive_pending_subtasks",
    "GenericEngineResolver",
    "InteractionDriver",
    "ModelBindPrelude",
    "ModelSelectorError",
    "NotResumableError",
    "ProviderSelectorError",
    "TaskAlreadyTerminalError",
    "UnknownTaskError",
    "multi_turn_policy_wrapper",
    "MultiTurnReActPolicy",
    "NEXT_GOAL_WAKE_HANDLE",
    "ResidentHost",
    "SessionInputs",
    "UnsupportedSubtaskSuspend",
]
