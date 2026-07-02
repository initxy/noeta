"""noeta.execution — the in-process agent execution machine (D1/D7).

Hoisted out of ``noeta.agent`` so the SDK can drive an agent end-to-end without
the coding product: the multi-turn policy wrappers, the sub-agent delegation
drain, the :class:`GenericEngineResolver` skeleton, the
:class:`ResidentSessionRunner` base, and the Protocol-typed
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
    STUB_MODEL_ALLOWLIST,
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
    derive_compaction_config,
)
from noeta.execution.multi_turn import (
    MultiTurnReActPolicy,
    NEXT_GOAL_WAKE_HANDLE,
)
from noeta.execution.resolver import (
    GenericEngineResolver,
    agent_name_of,
)
from noeta.execution.runner import (
    PreparedSession,
    ResidentSessionRunner,
)
from noeta.execution.commands import (
    CommandResolution,
    SlashCommand,
    first_sentence,
    get_command,
    list_commands,
    render_help,
    resolve_command,
)
from noeta.execution.environment import (
    load_environment,
    record_environment,
)
from noeta.execution.instructions import (
    DEFAULT_INSTRUCTIONS_FILENAMES,
    load_instructions,
    record_instructions,
)
from noeta.execution.skills import (
    DEFAULT_SKILLS_SUBDIR,
    activate_skills,
    build_skill_composer,
    build_skill_hashes,
    build_skill_script_wiring,
    extract_skill_allowed_tools_raw,
    load_workspace_skills,
    merge_skill_registries,
    resolve_skill_roots,
    resolve_skill_scripts,
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
    "build_skill_composer",
    "build_skill_hashes",
    "build_skill_script_wiring",
    "CommandResolution",
    "COMPACTION_OFF",
    "CompactionConfig",
    "DEFAULT_INSTRUCTIONS_FILENAMES",
    "DEFAULT_SKILLS_SUBDIR",
    "derive_compaction_config",
    "DrainHost",
    "drive_pending_subtasks",
    "extract_skill_allowed_tools_raw",
    "first_sentence",
    "GenericEngineResolver",
    "get_command",
    "InteractionDriver",
    "list_commands",
    "load_environment",
    "load_instructions",
    "load_workspace_skills",
    "ModelBindPrelude",
    "ModelSelectorError",
    "NotResumableError",
    "ProviderSelectorError",
    "TaskAlreadyTerminalError",
    "merge_skill_registries",
    "multi_turn_policy_wrapper",
    "MultiTurnReActPolicy",
    "NEXT_GOAL_WAKE_HANDLE",
    "PreparedSession",
    "record_environment",
    "record_instructions",
    "render_help",
    "ResidentHost",
    "ResidentSessionRunner",
    "resolve_command",
    "resolve_skill_roots",
    "resolve_skill_scripts",
    "SessionInputs",
    "skill_content_hash",
    "SlashCommand",
    "STUB_MODEL_ALLOWLIST",
    "UnsupportedSubtaskSuspend",
]
