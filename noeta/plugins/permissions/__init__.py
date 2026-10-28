"""Permissions and guards.

* :class:`Permissions` — Claude Code's permission system over every tool
  call: the six modes, ``allow`` / ``deny`` / ``ask`` rules, working
  directories and protected paths, the auto-mode classifier, plan mode with
  ``ExitPlanMode`` and ``/plan``; plus budget caps, the repetition guard,
  declarative PreToolUse rules and ``can_use_tool``.
"""

from noeta.plugins.permissions.extension import (
    DENIED_BY_HUMAN,
    MODE_SIGNAL,
    CanUseTool,
    Permissions,
    PermState,
    UntrustedProjectShellAllowlistWarning,
)
from noeta.plugins.permissions.guards import (
    Budget,
    MatchArg,
    PreToolUseRule,
    RepetitionAction,
    RepetitionPolicy,
)
from noeta.plugins.permissions.policy import (
    DEFAULT_RISK,
    MODE_CYCLE,
    PERMISSION_MODES,
    SHELL_COMPANION_TOOLS,
    PermissionMode,
    next_mode,
)

__all__ = [
    "DEFAULT_RISK",
    "DENIED_BY_HUMAN",
    "MODE_CYCLE",
    "MODE_SIGNAL",
    "PERMISSION_MODES",
    "SHELL_COMPANION_TOOLS",
    "Budget",
    "CanUseTool",
    "MatchArg",
    "PermState",
    "PermissionMode",
    "Permissions",
    "PreToolUseRule",
    "RepetitionAction",
    "RepetitionPolicy",
    "UntrustedProjectShellAllowlistWarning",
    "next_mode",
]
