"""Permissions and guards.

* :class:`Permissions` — one gate over every tool call: permission modes,
  the shell and WebFetch allowlists, remembered "always" rules, budget caps,
  the repetition guard, declarative PreToolUse rules and ``can_use_tool``.
"""

from noeta.plugins.permissions.extension import (
    DENIED_BY_HUMAN,
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
    PERMISSION_MODES,
    SHELL_COMPANION_TOOLS,
    PermissionMode,
)

__all__ = [
    "DEFAULT_RISK",
    "DENIED_BY_HUMAN",
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
]
