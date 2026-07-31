"""``PermissionGuard`` — tool / agent allowlists with fail-closed risk handling.

Permission is a security boundary, so an unknown configuration denies: with
``max_risk_level`` set, an unregistered tool name (no metadata to consult) or
an unrecognised ``risk_level`` string is a ``DENY``, never an ``ALLOW``.
``ProposedFinish`` is never blocked — permission gates *side-effecting*
actions, not termination.

The guard speaks only neutral Noeta tool names. Any product vocabulary (a
Claude→Noeta ``allowed-tools`` alias map, say) is resolved above it and
injected already-resolved through ``PermissionPolicy.skill_allowed_tools``,
so the guard needs to know neither vocabulary.
"""

from __future__ import annotations

from typing import Optional

from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedSpawnSubtask,
    ProposedToolCall,
    VerdictResult,
)
from noeta.protocols.tool import Tool
from noeta.runtime.governance import KNOWN_RISK_LEVELS, PermissionPolicy


__all__ = ["PermissionGuard"]


class PermissionGuard:
    """Synchronous permission Guard. ``DENY`` on policy violation or
    fail-closed conditions; otherwise ``ALLOW``."""

    name = "permission"
    priority = 20

    def __init__(
        self, policy: PermissionPolicy, tools: dict[str, Tool]
    ) -> None:
        self._policy = policy
        self._tools = tools
        # A declaring skill always appears in ``_declared_grants``: a grant
        # that failed to resolve maps to the empty frozenset (enforcement
        # stays ON for that skill and it grants nothing), never to "all
        # tools".
        self._declared_grants: dict[str, frozenset[str]] = {
            skill: grant for skill, grant in policy.skill_allowed_tools
        }

    def check(
        self, action: ProposedAction, ctx: GuardContext
    ) -> VerdictResult:
        if isinstance(action, ProposedToolCall):
            return self._check_tool(action, ctx)
        if isinstance(action, ProposedSpawnSubtask):
            return self._check_spawn(action, ctx)
        # ProposedFinish is never blocked by permission policy.
        return VerdictResult.allow()

    def _check_tool(
        self, action: ProposedToolCall, ctx: GuardContext
    ) -> VerdictResult:
        name = action.call.tool_name
        if name in self._policy.denied_tools:
            return VerdictResult.deny(f"tool {name!r} denied by policy")
        if (
            self._policy.allowed_tools is not None
            and name not in self._policy.allowed_tools
        ):
            return VerdictResult.deny(f"tool {name!r} not in allowlist")
        if self._policy.max_risk_level is not None:
            tool = self._tools.get(name)
            if tool is None:
                return VerdictResult.deny(
                    f"tool {name!r} has no metadata registered with "
                    "PermissionGuard; fail-closed"
                )
            risk = tool.risk_level
            if risk not in KNOWN_RISK_LEVELS:
                return VerdictResult.deny(
                    f"tool {name!r} has unknown risk_level {risk!r}; "
                    f"fail-closed (known levels: {KNOWN_RISK_LEVELS})"
                )
            if KNOWN_RISK_LEVELS.index(risk) > KNOWN_RISK_LEVELS.index(
                self._policy.max_risk_level
            ):
                return VerdictResult.deny(
                    f"tool {name!r} risk_level {risk!r} exceeds max "
                    f"{self._policy.max_risk_level!r}"
                )
        # Skill-script executors are gated ahead of the ordinary approval set:
        # such a call may only resolve to ``deny`` or ``require_approval``,
        # never ``allow``, and never depends on ``require_approval_tools``
        # wiring.
        if name in self._policy.skill_script_tools:
            return self._check_skill_script(action, ctx)
        if name in self._policy.require_approval_tools:
            return VerdictResult.require_approval(
                f"tool {name!r} requires human approval"
            )
        # Per-call conditional gate (e.g. a ``shell_run`` command outside the
        # effective allowlist), consulted after the static set so a tool
        # already gated there is not double-checked.
        if self._policy.conditional_approval is not None and (
            self._policy.conditional_approval(name, action.call.arguments)
        ):
            return VerdictResult.require_approval(
                f"tool {name!r} call requires human approval"
            )
        # Skill ``allowed-tools`` enforcement runs last: an explicit approval
        # requirement must not be downgraded by a skill grant.
        skill_verdict = self._check_skill_allowed_tools(name, ctx)
        if skill_verdict is not None:
            return skill_verdict
        return VerdictResult.allow()

    def _check_skill_script(
        self, action: ProposedToolCall, ctx: GuardContext
    ) -> VerdictResult:
        """Fail-closed precheck for a skill-script executor.

        Any precheck failure (malformed args, the target skill not active, an
        undiscovered ``(skill, relpath)``) is a ``deny`` — no approval
        suspend, no subprocess. A discovered script of an active skill is
        ``require_approval``. There is no allow path.
        """
        args = action.call.arguments
        skill = args.get("skill")
        relpath = args.get("relpath")
        if not (isinstance(skill, str) and skill):
            return VerdictResult.deny(
                "run_skill_script requires a non-empty 'skill' string"
            )
        if not (isinstance(relpath, str) and relpath):
            return VerdictResult.deny(
                "run_skill_script requires a non-empty 'relpath' string"
            )
        if skill not in ctx.active_skills:
            return VerdictResult.deny(
                f"skill {skill!r} is not active; cannot run its scripts"
            )
        if (skill, relpath) not in self._policy.skill_scripts:
            return VerdictResult.deny(
                f"{skill!r}/{relpath!r} is not a discovered skill script"
            )
        return VerdictResult.require_approval(
            f"running skill script {skill!r}/{relpath!r} requires approval"
        )

    def _check_skill_allowed_tools(
        self, name: str, ctx: GuardContext
    ) -> Optional[VerdictResult]:
        """Skill ``allowed-tools`` enforcement; ``None`` means "no opinion".

        Enforcement stays OFF unless the mode is on AND at least one
        **active** skill declares ``allowed-tools`` — gating every tool when
        nothing declared would break ordinary sessions. When on, the granted
        set is the union over the active declaring skills; a call outside it
        is gated by mode.
        """
        if self._policy.skill_tool_enforcement == "off":
            return None
        active_declaring = [
            s for s in ctx.active_skills if s in self._declared_grants
        ]
        if not active_declaring:
            return None
        granted: frozenset[str] = frozenset().union(
            *(self._declared_grants[s] for s in active_declaring)
        )
        if name in granted:
            return VerdictResult.allow()
        reason = (
            f"tool {name!r} not in the allowed-tools grant of active "
            f"skills {sorted(active_declaring)!r}"
        )
        if self._policy.skill_tool_enforcement == "deny":
            return VerdictResult.deny(reason)
        return VerdictResult.require_approval(reason)

    def _check_spawn(
        self, action: ProposedSpawnSubtask, ctx: GuardContext
    ) -> VerdictResult:
        agent = action.decision.agent_name
        if (
            self._policy.allowed_subtask_agents is not None
            and agent not in self._policy.allowed_subtask_agents
        ):
            return VerdictResult.deny(
                f"agent {agent!r} not in subtask allowlist"
            )
        return VerdictResult.allow()
