"""Operator-chosen tool-approval modes, packaged as a single-file manifest plugin.

Demonstrated SDK capability: the ``guard`` surface. A guard is process-scoped
governance — once the plugin is loaded the gate applies to every agent, so an
operator cannot opt one agent out by leaving the plugin off its activation list.

The manifest mechanism resolves a contribution ``ref`` to a live object and
threads no per-plugin config dict; that is why the shipped :data:`GUARD` reads
its mode from the environment at import. Keeping configuration out of the
contribution keeps it out of agent identity. The finer knobs (per-tool
overrides, the ``smart_approve`` classification) stay reachable by constructing
an :class:`ApprovalModesGuard` directly — see ``README.md``.
"""

# No ``from __future__ import annotations`` here on purpose. A path-loaded
# plugin module is never registered in ``sys.modules``, so ``@dataclass`` cannot
# resolve stringized field annotations through ``sys.modules[__module__]`` (its
# ``KW_ONLY`` detection) and raises. Eager annotations skip that lookup.

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from noeta.sdk import (
    GuardContext,
    PluginBuilder,
    ProposedAction,
    ProposedToolCall,
    VerdictResult,
)


__all__ = [
    "ApprovalPolicy",
    "ApprovalModesGuard",
    "build_policy",
    "GUARD",
    "plugin",
    "MODES",
    "OVERRIDES",
    "DEFAULT_MODE",
    "DEFAULT_LOW_RISK_TOOLS",
]


#: The modes, in escalating-permission order.
MODES: tuple[str, ...] = ("chat", "approve", "smart_approve", "auto")

#: The per-tool override tokens: pin a tool open (``always``), gated (``ask``),
#: or shut (``never``).
OVERRIDES: tuple[str, ...] = ("always", "ask", "never")

DEFAULT_MODE = "approve"

#: The ``smart_approve`` allow set. Deliberately conservative — a tool absent
#: from this set asks, so a classification gap fails towards the human rather
#: than towards an unreviewed call. An operator replaces the set wholesale.
DEFAULT_LOW_RISK_TOOLS: frozenset[str] = frozenset({"read", "grep", "glob", "ls"})


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Immutable approval configuration.

    A present ``overrides`` entry decides a tool's verdict outright, before
    ``mode`` is consulted, so an operator can pin one tool open or shut without
    disturbing the mode. ``low_risk_tools`` is read only in ``smart_approve``.
    """

    mode: str = DEFAULT_MODE
    overrides: Mapping[str, str] = field(default_factory=dict)
    low_risk_tools: frozenset[str] = DEFAULT_LOW_RISK_TOOLS


class ApprovalModesGuard:
    """Synchronous tool-approval Guard driven by an :class:`ApprovalPolicy`.

    Priority 25 lands between the built-in ``PermissionGuard`` (20) and
    ``RepetitionGuard`` (30): the HookManager returns the first non-allow
    verdict in ascending-priority order, so a hard permission denial must be
    able to speak before this softer approval preference does.

    Only ``ProposedToolCall`` is gated — approval modes are about tool
    execution, so spawns and finishes pass through as the built-in
    ``PermissionGuard`` lets ``ProposedFinish`` through.
    """

    name = "approval_modes"
    priority = 25

    def __init__(self, policy: ApprovalPolicy) -> None:
        self._policy = policy

    def check(
        self, action: ProposedAction, ctx: GuardContext
    ) -> VerdictResult:
        if not isinstance(action, ProposedToolCall):
            return VerdictResult.allow()
        name = action.call.tool_name
        override = self._policy.overrides.get(name)
        if override is not None:
            return self._verdict_for_override(name, override)
        return self._verdict_for_mode(name)

    def _verdict_for_override(self, name: str, token: str) -> VerdictResult:
        if token == "always":
            return VerdictResult.allow()
        if token == "never":
            return VerdictResult.deny(
                f"tool {name!r} is pinned off by an approval-modes override"
            )
        # "ask" — the remaining token.
        return VerdictResult.require_approval(
            f"tool {name!r} requires approval by an approval-modes override"
        )

    def _verdict_for_mode(self, name: str) -> VerdictResult:
        mode = self._policy.mode
        if mode == "auto":
            return VerdictResult.allow()
        if mode == "chat":
            return VerdictResult.deny(
                f"chat mode runs no tools; {name!r} was not run"
            )
        if mode == "smart_approve":
            if name in self._policy.low_risk_tools:
                return VerdictResult.allow()
            return VerdictResult.require_approval(
                f"tool {name!r} is not classified low-risk; approval required "
                f"(smart_approve)"
            )
        # "approve" — the remaining mode, and the default.
        return VerdictResult.require_approval(
            f"approve mode requires human approval for tool {name!r}"
        )


def build_policy(config: Optional[Mapping[str, Any]] = None) -> ApprovalPolicy:
    """Validate ``config`` and build an :class:`ApprovalPolicy`.

    Rejects a bad mode / override token / ``low_risk_tools`` with ``ValueError``
    rather than silently falling back: the loader surfaces it as a
    ``PluginError`` naming this plugin, so a typo fails the client build instead
    of quietly widening the fence mid-session.
    """
    data = dict(config or {})

    mode = data.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        raise ValueError(
            f"approval-modes: unknown mode {mode!r}; expected one of {MODES}"
        )

    raw_overrides = data.get("overrides") or {}
    if not isinstance(raw_overrides, Mapping):
        raise ValueError(
            "approval-modes: 'overrides' must be a mapping of tool name -> "
            f"one of {OVERRIDES}"
        )
    overrides: dict[str, str] = {}
    for tool_name, token in raw_overrides.items():
        if token not in OVERRIDES:
            raise ValueError(
                f"approval-modes: override for tool {str(tool_name)!r} is "
                f"{token!r}; expected one of {OVERRIDES}"
            )
        overrides[str(tool_name)] = token

    raw_low = data.get("low_risk_tools")
    if raw_low is None:
        low_risk = DEFAULT_LOW_RISK_TOOLS
    elif isinstance(raw_low, (str, bytes)) or not isinstance(raw_low, Iterable):
        raise ValueError(
            "approval-modes: 'low_risk_tools' must be a list of tool names"
        )
    else:
        low_risk = frozenset(str(t) for t in raw_low)

    return ApprovalPolicy(mode=mode, overrides=overrides, low_risk_tools=low_risk)


#: Carried on the manifest so operator tooling can list the knobs. Descriptive
#: only — nothing in the loader reads it, so it must be kept true by hand.
CONFIG_SCHEMA = {
    "env": {
        "NOETA_APPROVAL_MODE": f"one of {MODES} (default: {DEFAULT_MODE})",
    }
}


#: The configured guard the manifest ships. Built once at import, so a host must
#: set ``NOETA_APPROVAL_MODE`` *before* loading the plugin. A distributed install
#: resolves it through the ``ref`` below; a single-file load caches this very
#: object, so the two paths agree without a second import.
GUARD = ApprovalModesGuard(
    build_policy({"mode": os.environ.get("NOETA_APPROVAL_MODE", DEFAULT_MODE)})
)


#: The builder *is* this plugin's manifest, and its name is the plugin identity
#: — the enable-list key, not the filename. ``python -m noeta.sdk.plugin_check``
#: derives TOML from it and verifies the shipped ``noeta-plugin.toml`` matches,
#: which is what stops the two from drifting.
plugin = PluginBuilder(
    "approval-modes", requires_noeta=">=0.4", config_schema=CONFIG_SCHEMA
)
plugin.contribute("guard", GUARD, name="approval_modes", ref="approval_modes:GUARD")
