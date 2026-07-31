"""noeta.execution — the in-process agent execution machine (D1/D7).

Hoisted out of ``noeta.agent`` so the SDK can drive an agent end-to-end without
the coding product: the multi-turn policy wrappers, the sub-agent delegation
drain, the :class:`GenericEngineResolver` skeleton, and the Protocol-typed
:class:`InteractionDriver` (issue 01 complete — noeta.agent keeps thin
re-export shims until the issue-07 flip).

Code-agnostic by contract: this package may import the lower layers
(``noeta.protocols`` / ``noeta.core`` / ``noeta.policies`` / the kernel-services
band) plus the sdk-owned identity layer ``noeta.agent.spec`` /
``noeta.agent.registry`` — but never the noeta-agent product modules
(``noeta.agent.host`` / ``noeta.agent.backend`` / …), enforced by the import-linter
layered topology (see .importlinter).
"""

from __future__ import annotations

from noeta.execution.driver import (
    InteractionDriver,
    ModelBindPrelude,
    ModelSelectorError,
    NotResumableError,
    ProviderSelectorError,
    TaskAlreadyTerminalError,
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
from noeta.execution.environment import (
    EnvironmentKit,
)
from noeta.execution.instructions import (
    InstructionsKit,
)
# Microkernel phase 2a: the skill material (indexer / script tool / wiring)
# moved into the ``skills`` built-in plugin (``noeta.builtins.skills.impl``);
# this package re-exports only the kernel seams.
from noeta.execution.skills import (
    SkillsKit,
    activate_skills,
    build_skill_hashes,
    skill_content_hash,
)
from noeta.execution.subtask_drain import (
    DrainHost,
    UnsupportedSubtaskSuspend,
    drive_pending_subtasks,
)

__all__ = [
    "activate_skills",
    "agent_name_of",
    "AgentRegistryProtocol",
    "build_session_inputs",
    "build_skill_hashes",
    "SkillsKit",
    "COMPACTION_OFF",
    "CompactionConfig",
    "EnvironmentKit",
    "InstructionsKit",
    "DrainHost",
    "drive_pending_subtasks",
    "GenericEngineResolver",
    "InteractionDriver",
    "ModelBindPrelude",
    "ModelSelectorError",
    "NotResumableError",
    "ProviderSelectorError",
    "TaskAlreadyTerminalError",
    "multi_turn_policy_wrapper",
    "MultiTurnReActPolicy",
    "NEXT_GOAL_WAKE_HANDLE",
    "ResidentHost",
    "SessionInputs",
    "skill_content_hash",
    "UnsupportedSubtaskSuspend",
]
