"""Permissions, guards and hooks.

* :class:`Permissions` — one gate over every tool call: permission modes,
  the shell and WebFetch allowlists, remembered "always" rules, budget caps,
  the repetition guard, declarative PreToolUse rules and ``can_use_tool``.
* :class:`Hooks` — PostToolUse / Notification commands (side effects only).
* :func:`pending` / :func:`pending_tree` — what a task waits on a human for.
"""

from noeta.ext.permissions.extension import (
    DENIED_BY_HUMAN,
    CanUseTool,
    Permissions,
    PermState,
    UntrustedProjectShellAllowlistWarning,
)
from noeta.ext.permissions.guards import (
    Budget,
    MatchArg,
    PreToolUseRule,
    RepetitionAction,
    RepetitionPolicy,
)
from noeta.ext.permissions.hooks import (
    DEFAULT_HOOK_COMMAND_TIMEOUT_S,
    DEFAULT_HOOK_QUEUE_MAX,
    Hooks,
    NotificationRule,
    NotifyHandle,
    NotifyRunner,
    PostToolUseRule,
)
from noeta.ext.permissions.pending import Pending, pending, pending_tree
from noeta.ext.permissions.policy import (
    DEFAULT_RISK,
    PERMISSION_MODES,
    SHELL_COMPANION_TOOLS,
    PermissionMode,
)

__all__ = [
    "DEFAULT_HOOK_COMMAND_TIMEOUT_S",
    "DEFAULT_HOOK_QUEUE_MAX",
    "DEFAULT_RISK",
    "DENIED_BY_HUMAN",
    "PERMISSION_MODES",
    "SHELL_COMPANION_TOOLS",
    "Budget",
    "CanUseTool",
    "Hooks",
    "MatchArg",
    "NotificationRule",
    "NotifyHandle",
    "NotifyRunner",
    "Pending",
    "PermState",
    "PermissionMode",
    "Permissions",
    "PostToolUseRule",
    "PreToolUseRule",
    "RepetitionAction",
    "RepetitionPolicy",
    "UntrustedProjectShellAllowlistWarning",
    "pending",
    "pending_tree",
]
