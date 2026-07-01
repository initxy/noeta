"""``PermissionGuard`` — tool / agent allowlists with fail-closed
risk-level handling.

Issue 18. Routes ``ProposedToolCall`` through allowlist / denylist /
risk-level checks and ``ProposedSpawnSubtask`` through an agent
allowlist. ``ProposedFinish`` is never blocked — permission is about
gating *side-effecting* actions, not termination.

**Fail-closed semantics** (issue 18 B4): when ``max_risk_level`` is
configured, any of the following return ``DENY`` rather than silently
allowing:

* the tool name is not registered with the injected ``tools`` mapping
  (no metadata to consult);
* the tool's ``risk_level`` string is not one of the known levels
  (``low`` < ``medium`` < ``high`` — the project-wide canonical neutral
  scale; every production tool declares one of these).

Permission is a security boundary; an unknown configuration is a
``DENY``, not an ``ALLOW``.

The guard operates only on **neutral** Noeta tool names: any product
vocabulary (e.g. a Claude→Noeta ``allowed-tools`` alias map) is resolved
**above** the guard, in noeta-sdk, and the resolved neutral grants are
injected via ``PermissionPolicy.skill_allowed_tools``. The guard knows
both vocabularies for neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional

from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedSpawnSubtask,
    ProposedToolCall,
    VerdictResult,
)
from noeta.protocols.tool import Tool


__all__ = ["PermissionGuard", "PermissionPolicy", "SkillEnforcementMode"]


RiskLevel = Literal["low", "medium", "high"]


_KNOWN_RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")


#: Phase 4.5 Issue B — skill ``allowed-tools`` enforcement mode.
#: ``off`` (default) disables the feature; ``approval`` gates an
#: out-of-grant call through the Issue A HITL approval path;
#: ``deny`` fails it closed.
SkillEnforcementMode = Literal["off", "approval", "deny"]


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    """Allow/deny rules for tools and subtask spawns."""

    allowed_tools: Optional[frozenset[str]] = None
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    max_risk_level: Optional[RiskLevel] = None
    allowed_subtask_agents: Optional[frozenset[str]] = None
    # Phase 4.5 Issue A — tools that, when they would otherwise be
    # allowed, require an explicit human approval (HITL). The guard
    # returns ``require_approval`` (→ ``yield_for_human`` suspend) rather
    # than ``allow``. Hard-deny conditions (denylist / allowlist / risk
    # ceiling) still take precedence — there is no point asking a human
    # about a call the policy already forbids.
    require_approval_tools: frozenset[str] = field(default_factory=frozenset)
    # Per-CALL conditional approval predicate. Built ABOVE the guard (in
    # noeta-sdk, which can see tool semantics) and injected as a plain callable
    # so the guard stays ``protocols``-only. Given ``(tool_name, arguments)`` it
    # returns True iff THIS specific call needs human sign-off — used for
    # ``shell_run`` under default/acceptEdits: a command already in the effective
    # allowlist runs silently, an unknown one is gated through the Issue A HITL
    # path. ``None`` (every pre-feature path: CLI / bypass) ⇒ no
    # per-call gate, byte-identical to before. Excluded from eq/hash (it is a
    # closure that is never serialized).
    conditional_approval: Optional[
        Callable[[str, Mapping[str, Any]], bool]
    ] = field(default=None, compare=False)
    # Phase 4.5 Issue B — skill ``allowed-tools`` enforcement.
    # ``skill_tool_enforcement`` is the mode (default ``off``).
    # ``skill_allowed_tools`` is a plain immutable
    # ``(skill_name, frozenset_of_neutral_noeta_tool_names)`` map — the
    # grants are **already parsed and alias-resolved** by noeta-sdk
    # (``noeta.policies.skill_tools.resolve_skill_allowed_tools``), which
    # knows both the product (Claude) and neutral Noeta vocabularies. The
    # guard stores the resolved map directly and never imports any product
    # vocabulary (preserves ``guards may only import noeta.protocols`` AND
    # keeps the kernel free of provider tool-name opinion). A declaring
    # skill that resolved to an empty grant still appears here (fail-safe:
    # enforcement stays ON for that skill and it grants nothing).
    skill_tool_enforcement: SkillEnforcementMode = "off"
    skill_allowed_tools: tuple[tuple[str, frozenset[str]], ...] = ()
    # Phase 4.5 Issue E — skill-bundled script execution.
    # ``skill_script_tools`` names the exec tools whose execution is an
    # always-approval **guard invariant** (e.g. ``{"run_skill_script"}``):
    # such a call is gated by a fail-closed precheck → ``require_approval``
    # here, never falling through to ``allow`` and not relying on
    # ``require_approval_tools``. ``skill_scripts`` is the discovered
    # ``(skill, relpath)`` set the precheck validates against (the tool
    # gets the richer ``(skill, relpath, root_path)`` map separately).
    skill_script_tools: frozenset[str] = field(default_factory=frozenset)
    skill_scripts: frozenset[tuple[str, str]] = field(
        default_factory=frozenset
    )


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
        # Issue B: the skill->allowed-tools grants arrive already parsed
        # and alias-resolved (neutral Noeta tool names) from noeta-sdk; the
        # guard just stores the resolved map. A declaring skill always
        # appears in ``_declared_grants`` — a resolution that failed safe
        # maps to the empty frozenset (enforcement stays ON for that skill
        # and it grants nothing), never to "all tools".
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
        # ProposedFinish: never blocked by permission policy.
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
                    "PermissionGuard; fail-closed (issue 18 B4)"
                )
            risk = tool.risk_level
            if risk not in _KNOWN_RISK_LEVELS:
                return VerdictResult.deny(
                    f"tool {name!r} has unknown risk_level {risk!r}; "
                    f"fail-closed (known levels: {_KNOWN_RISK_LEVELS})"
                )
            if _KNOWN_RISK_LEVELS.index(risk) > _KNOWN_RISK_LEVELS.index(
                self._policy.max_risk_level
            ):
                return VerdictResult.deny(
                    f"tool {name!r} risk_level {risk!r} exceeds max "
                    f"{self._policy.max_risk_level!r}"
                )
        # Issue E: skill-script executors are gated by a guard-level
        # invariant BEFORE the ordinary Issue A gate. A
        # ``skill_script_tools`` call can only ever resolve to ``deny``
        # (fail-closed precheck) or ``require_approval`` — NEVER ``allow``,
        # and NOT dependent on ``require_approval_tools`` wiring.
        if name in self._policy.skill_script_tools:
            return self._check_skill_script(action, ctx)
        # Issue A: an otherwise-allowed tool may still need human sign-off.
        if name in self._policy.require_approval_tools:
            return VerdictResult.require_approval(
                f"tool {name!r} requires human approval"
            )
        # Per-call conditional gate (e.g. shell_run command not in the effective
        # allowlist). Consulted AFTER the static set so a tool already gated
        # there is not double-checked. The predicate sees the call arguments;
        # returning True routes through the same Issue A approval suspend.
        if self._policy.conditional_approval is not None and (
            self._policy.conditional_approval(name, action.call.arguments)
        ):
            return VerdictResult.require_approval(
                f"tool {name!r} call requires human approval"
            )
        # Issue B: skill `allowed-tools` enforcement, AFTER the explicit
        # Issue A gate (architect-pinned check order). Based only on the
        # *active* declaring skills (fold-derived ``ctx.active_skills``).
        skill_verdict = self._check_skill_allowed_tools(name, ctx)
        if skill_verdict is not None:
            return skill_verdict
        return VerdictResult.allow()

    def _check_skill_script(
        self, action: ProposedToolCall, ctx: GuardContext
    ) -> VerdictResult:
        """Issue E fail-closed precheck for a skill-script executor.

        Returns ``deny`` on any precheck failure (malformed args, the
        target skill not active, or an undiscovered ``(skill, relpath)``)
        — **no** approval suspend, **no** subprocess. Returns
        ``require_approval`` only when the call targets a discovered
        script of a currently-active skill. There is **no allow path**.
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
        """Issue B enforcement; ``None`` means "no skill opinion → allow".

        Enforcement is OFF unless the mode is on AND at least one
        **active** skill declares ``allowed-tools``. When on, the
        pre-approval set is the union of the active declaring skills'
        granted Noeta tools; a call outside it is gated by mode
        (``approval`` → HITL via Issue A; ``deny`` → fail closed).
        """
        if self._policy.skill_tool_enforcement == "off":
            return None
        active_declaring = [
            s for s in ctx.active_skills if s in self._declared_grants
        ]
        if not active_declaring:
            # No active skill opted into allowed-tools → enforcement OFF
            # (gating every tool when nothing declared would break
            # ordinary sessions).
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
