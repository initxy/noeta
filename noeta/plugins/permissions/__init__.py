"""Permissions and guards.

* :class:`Permissions` — Claude Code's permission system over every tool
  call: the six modes, ``allow`` / ``deny`` / ``ask`` rules, working
  directories and protected paths, the auto-mode classifier and what may
  run while planning (plan mode itself is ``noeta.plugins.plan_mode``); plus
  declarative PreToolUse rules and ``can_use_tool`` (the budget caps are
  ``noeta.plugins.budget``).
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
    MatchArg,
    PreToolUseRule,
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
    "CanUseTool",
    "MatchArg",
    "PermState",
    "PermissionMode",
    "Permissions",
    "PreToolUseRule",
    "UntrustedProjectShellAllowlistWarning",
    "next_mode",
]
