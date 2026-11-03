"""What active skills say about a tool call: their ``allowed-tools``
grant (enforced, and pre-approving the tools it names) and the
``run_skill_script`` check."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from noeta.core.extension import Ask, Deny, Gate, TaskView
from noeta.core.types import ToolUse
from noeta.toolkit import permission_rules as pr
from noeta.toolkit.inline_commands import rules_cover

Enforcement = Literal["off", "approval", "deny"]
#: A whole-tool rule matches by name alone: no path is judged.
_ANY = pr.RuleContext(root="/", home="/", cwd="/")


def allowed_tools_gate(tool: str, active: Mapping[str, Any],
                       enforcement: Enforcement) -> Gate | None:
    """Enforcement is on only when the mode is on and an active skill
    declares ``allowed-tools``; the grant is the union over them, each name
    covering the tools a whole-tool permission rule of it covers (``Read``
    covers ``Grep``, ``mcp__server`` that server's tools)."""
    if enforcement == "off":
        return None
    declaring = sorted(n for n, a in active.items() if a.get("allowed_tools") is not None)
    if not declaring:
        return None
    granted = {t for n in declaring for t in active[n]["allowed_tools"]}
    if "*" in granted or any(pr.match(pr.Rule(tool=t, content=None, text=t), tool, {}, _ANY,
                                      for_allow=True)
                             for t in granted):
        return None
    reason = f"tool {tool!r} not in the allowed-tools grant of active skills {declaring!r}"
    if enforcement == "deny":
        return Deny(reason=reason)
    return Ask(prompt=reason, detail={"tool": tool, "skills": declaring})


def pre_approved(view: TaskView, call: ToolUse, active: Mapping[str, Any]) -> bool:
    """Whether an active skill's ``allowed-tools`` covers this call: while
    the skill is in the conversation its tools run without asking (Claude
    Code's pre-approval). A deny, or an ask rule, still stands."""
    texts = [t for entry in active.values() for t in entry.get("allow_rules") or ()]
    return rules_cover(view, call, texts)


def script_gate(call: ToolUse, active: Mapping[str, Any]) -> Gate:
    """Fail closed: malformed, inactive or undiscovered => deny; a
    discovered script of an active skill => ask. Never allow."""
    skill = call.input.get("skill")
    relpath = call.input.get("relpath")
    if not (isinstance(skill, str) and skill):
        return Deny(reason="run_skill_script requires a non-empty 'skill' string")
    if not (isinstance(relpath, str) and relpath):
        return Deny(reason="run_skill_script requires a non-empty 'relpath' string")
    if skill not in active:
        return Deny(reason=f"skill {skill!r} is not active; cannot run its scripts")
    if relpath not in (active[skill].get("scripts") or []):
        return Deny(reason=f"{skill!r}/{relpath!r} is not a discovered skill script")
    return Ask(prompt=f"running skill script {skill!r}/{relpath!r} requires approval",
               detail={"skill": skill, "relpath": relpath, "args": call.input.get("args", [])})
