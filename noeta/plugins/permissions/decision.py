"""What the permission checks say about a call before any classifier, and
the options a person's card offers for it."""

from __future__ import annotations

from dataclasses import dataclass, replace

from noeta.plugins.permissions import wording as w
from noeta.toolkit.permission_rules import EDIT_TOOLS, SHELL_TOOLS


@dataclass(frozen=True)
class Decision:
    """What the permission system says about a call before any classifier."""

    kind: str  # "allow" | "deny" | "ask"
    reason: str = ""
    firm: bool = False
    #: The person-facing kind of ask: ``edit`` / ``command`` / ``read`` /
    #: ``protected`` / ``plan`` / ``tool`` / ``fetch``.
    why: str = ""
    always: tuple[str, ...] = ()
    always_label: str | None = None
    mode_choice: str | None = None
    #: In auto mode, an ask the classifier answers.
    classify: bool = False


def in_mode(mode: str, d: Decision, name: str, *, plan_classifier: bool = False) -> Decision:
    """What the mode makes of an ask. ``plan_classifier``: this plan runs
    with auto mode's classifier (Claude Code's ``useAutoModeDuringPlan``;
    noeta: only a plan begun in auto mode), which answers every ask but two that
    stay the person's: a file tool's write, and plan mode's refusal of a
    tool that is neither read-only nor a shell tool. A shell command plan
    mode would ask about (a write among them) runs on the classifier's
    word, as in Claude Code. Without ``plan_classifier`` the person answers
    every ask in plan mode."""
    if d.kind != "ask":
        return d
    if mode == "dontAsk":
        return Decision("deny", w.DONT_ASK_DENIED.format(name=name))
    if mode == "bypassPermissions" and not d.firm and d.why != "protected":
        return Decision("allow")
    if mode == "auto" and not d.firm:
        return replace(d, classify=True)
    if mode == "plan" and plan_classifier and not d.firm and name not in EDIT_TOOLS \
            and not (d.why == "plan" and name not in SHELL_TOOLS):
        return replace(d, classify=True)
    return d


def choices(d: Decision) -> list[dict[str, str]]:
    """The card's options (``id`` is what the answer does)."""
    out = [{"id": "allow", "label": w.CHOICE_YES}]
    if d.always and d.always_label:
        out.append({"id": "always", "label": d.always_label})
    if d.mode_choice and not d.firm:
        out.append({"id": f"mode:{d.mode_choice}", "label": w.CHOICE_MODE[d.mode_choice]})
    out.append({"id": "deny", "label": w.CHOICE_NO})
    return out
