"""First-party example manifest plugin — goose-style tool-approval modes.

Demonstrated SDK capability
---------------------------
A **manifest plugin** (the SDK-extensibility redesign,
``docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md``, D1) that
contributes a single :class:`~noeta.sdk.Guard` on the ``guard`` surface —
*governance* authority, so once loaded it applies to every agent in the process
(spec D6). It shows how a configured object is packaged: the guard's immutable
:class:`ApprovalPolicy` is built from the environment at import (the mechanism
resolves a ``ref`` to a live object, never threading a per-plugin config dict).

Four modes decide the verdict for a proposed tool call (goose's permission
modes, expressed as Noeta ``Verdict`` values):

* ``chat`` — deny every tool call (the agent may reason and answer, but runs no
  tools).
* ``approve`` — require human approval for every tool call. **Default.**
* ``smart_approve`` — allow *low-risk* tools (a configurable tool-name
  classification; conservative default: only read-only ``read`` / ``grep`` /
  ``glob`` / ``ls``) and require approval for everything else.
* ``auto`` — allow every tool call.

A per-tool **override** (``always`` / ``ask`` / ``never``) always wins over the
mode, so an operator can pin one tool open or shut regardless of the active
mode. The guard only gates tool calls; subtask spawns and finishes pass through
(approval modes are about *tool execution*, mirroring the built-in
``PermissionGuard``'s treatment of ``ProposedFinish``).

Config shape
------------
:func:`build_policy` validates and builds an :class:`ApprovalPolicy` from a
dict (all keys optional)::

    {
      "mode": "smart_approve",                    # default "approve"
      "overrides": {"write": "never",             # always | ask | never
                    "read": "always"},
      "low_risk_tools": ["read", "grep", "glob", "ls"]   # replaces the default
    }                                                    # smart_approve set

An unknown ``mode``, a bad override token, or a non-list ``low_risk_tools``
raises ``ValueError`` (a misconfiguration fails the client build loudly rather
than a mid-session turn). The manifest ships a policy built from the
environment: ``NOETA_APPROVAL_MODE`` selects the mode (default ``approve``);
the finer knobs (``overrides`` / ``low_risk_tools``) stay available to a host
that constructs its own :class:`ApprovalModesGuard`.

Loading it
----------
Installed as a package it is discovered via its ``noeta.plugins`` entry point +
its shipped ``noeta-plugin.toml`` (see ``pyproject.toml``). In this repository
it is loaded by explicit path — no install::

    from noeta.sdk import Client, load_plugin_set, presets

    pset = load_plugin_set(
        builtins=False,
        modules=["examples/plugins/approval-modes/plugin.py"],
    )
    client = Client(presets.main_options(), plugins=pset)  # guard is process-wide
"""

# NOTE: deliberately *no* ``from __future__ import annotations``. This module is
# loaded by path (``load_plugins(modules=[...])`` / directory discovery), so it
# is never registered in ``sys.modules``. Stringized annotations would make
# ``@dataclass`` resolve the field types through ``sys.modules[__module__]``
# (its ``KW_ONLY`` detection), which is absent for a path-loaded module and
# raises. Eager (real) annotations sidestep that lookup entirely.

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


#: The four goose-style modes, in escalating-permission order.
MODES: tuple[str, ...] = ("chat", "approve", "smart_approve", "auto")

#: The per-tool override tokens: pin a tool open (``always`` → allow), gated
#: (``ask`` → require approval), or shut (``never`` → deny).
OVERRIDES: tuple[str, ...] = ("always", "ask", "never")

#: Mode used when config omits ``mode``.
DEFAULT_MODE = "approve"

#: Conservative default ``smart_approve`` classification — only read-only tools
#: are treated as low-risk. Everything else asks. An operator replaces this set
#: wholesale via the ``low_risk_tools`` config key.
DEFAULT_LOW_RISK_TOOLS: frozenset[str] = frozenset({"read", "grep", "glob", "ls"})


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Immutable approval configuration the guard reads.

    ``overrides`` maps a tool name to one of :data:`OVERRIDES`; a present entry
    decides the tool's verdict outright, before ``mode`` is consulted.
    ``low_risk_tools`` is the ``smart_approve`` allow set (only consulted in
    that mode).
    """

    mode: str = DEFAULT_MODE
    overrides: Mapping[str, str] = field(default_factory=dict)
    low_risk_tools: frozenset[str] = DEFAULT_LOW_RISK_TOOLS


class ApprovalModesGuard:
    """Synchronous tool-approval Guard driven by an :class:`ApprovalPolicy`.

    Priority 25 sits just after the built-in ``PermissionGuard`` (20): a hard
    allow/deny permission decision still precedes this approval-preference gate
    (the HookManager returns the first non-allow verdict in ascending-priority
    order), while this gate precedes the ``RepetitionGuard`` (30). Only
    ``ProposedToolCall`` is gated; spawns and finishes pass through.
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
        # "ask"
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
        # "approve" (the default): every tool call needs human sign-off.
        return VerdictResult.require_approval(
            f"approve mode requires human approval for tool {name!r}"
        )


def build_policy(config: Optional[Mapping[str, Any]] = None) -> ApprovalPolicy:
    """Validate ``config`` and build an :class:`ApprovalPolicy`.

    Raises ``ValueError`` (which the loader surfaces as a ``PluginError`` naming
    this plugin) on an unknown ``mode``, a bad override token, or a
    non-list ``low_risk_tools``. All keys are optional; an empty/absent config
    yields the default ``approve`` mode with the conservative low-risk set.
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


# ---------------------------------------------------------------------------
# Environment-sourced configuration + the manifest (spec D1).
# ---------------------------------------------------------------------------


#: The declarative config schema, carried on the manifest for operator tooling.
#: Descriptive only — the mechanism never reads it (config is environment-sourced).
CONFIG_SCHEMA = {
    "env": {
        "NOETA_APPROVAL_MODE": f"one of {MODES} (default: {DEFAULT_MODE})",
    }
}


#: The configured guard the manifest ships. Its mode comes from the environment
#: at import; a distributed install exposes it at the ``ref`` below
#: (``approval_modes:GUARD``), while the single-file load caches this very object
#: so resolution never re-imports.
GUARD = ApprovalModesGuard(
    build_policy({"mode": os.environ.get("NOETA_APPROVAL_MODE", DEFAULT_MODE)})
)


#: The single-file manifest (decorator sugar *is* the manifest, spec D1). The
#: builder name is the plugin identity. ``python -m noeta.sdk.plugin_check``
#: derives the TOML from this builder and verifies it against the shipped
#: ``noeta-plugin.toml`` / ``[tool.noeta]``.
plugin = PluginBuilder(
    "approval-modes", requires_noeta=">=0.4", config_schema=CONFIG_SCHEMA
)
plugin.contribute("guard", GUARD, name="approval_modes", ref="approval_modes:GUARD")
