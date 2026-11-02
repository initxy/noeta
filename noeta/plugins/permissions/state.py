"""What the permissions plugin folds from its own records."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from noeta.core.events import ExtensionRecorded
from noeta.core.extension import TaskView
from noeta.toolkit import permission_modes as pm

NAME = "permissions"
#: In a plan record: whether this plan runs with the auto-mode classifier,
#: settled when plan mode is entered (absent, in a log written before
#: noeta recorded it: when planning began in auto mode).
AUTO_DURING_PLAN = "auto_during_plan"
#: Decisions kept in the folded state (per call id).
_KEEP = 256
#: Task config key a child reads its parent's remembered rules from.
INHERITED_RULES_KEY = "permission_rules"
#: Task config key a child of a planning task reads the parent's plan name
#: from: its own plan file is ``<name>-agent-<child task id>.md`` (Claude
#: Code's sub-agent plan file).
PLAN_NAME_KEY = "plan_name"
#: Task config key a child of a planning task reads the parent's plan origin
#: from (``{"pre_mode", "auto_during_plan"}``): it plans as its parent does,
#: with or without the auto-mode classifier, whatever the service's default
#: mode.
PLAN_ORIGIN_KEY = "plan_origin"


@dataclass(frozen=True)
class PermState:
    """Folded from this extension's records."""

    #: Allow rules remembered from "always" answers (rule strings).
    rules: tuple[str, ...] = ()
    #: The mode switched to during the task (``None``: the starting one).
    mode: str | None = None
    #: Plan mode: ``{"path", "pre_mode", "entered_seq", "by_command",
    #: "auto_during_plan"}``, kept after it ends.
    plan: dict[str, Any] | None = None
    #: Plan-mode texts sent since planning began or the last compaction.
    plan_reminders: int = 0
    #: Plan mode was left since the task began (a later entry is a re-entry).
    plan_left: bool = False
    #: A plan's name was asked for: only the first approved plan of a task
    #: is named (Claude Code names one only while the session has none).
    plan_named: bool = False
    #: The name the first approved plan was given.
    plan_name: str | None = None
    steps: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    #: ``call_id -> "allow" | "deny"`` decided by ``can_use_tool``.
    auto: dict[str, str] = field(default_factory=dict)
    #: ``call_id -> {"rules": [...]}``: what an "always" answer remembers.
    asked: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``call_id -> {"decision", "reason", "category"}`` from the classifier.
    classified: dict[str, dict[str, Any]] = field(default_factory=dict)
    consecutive_denials: int = 0
    total_denials: int = 0
    #: The classifier moved to the main model.
    demoted: bool = False
    #: Notes owed to the next request: ``auto_exited``, ``plan_exited``,
    #: ``plan_active``, ``plan_reentry``.
    due: tuple[str, ...] = ()
    #: An auto-mode note was sent since auto mode was last left or the task
    #: last compacted (Claude Code sends the next one only after that).
    auto_noted: bool = False
    #: The seq of the newest prompt the plan-mode text was sent for.
    plan_noted: int = 0
    #: After a compaction, the auto-mode note waits for a prompt or a tool
    #: result newer than this seq (Claude Code's).
    guidance_after: int = 0
    #: The server-side check was offered (on the first main auto-mode
    #: request of the task, and again after a front end resumed it).
    probed: bool = False
    #: The task was compacted since it started or was last resumed.
    compacted: bool = False
    #: The mode vocabulary last published for front ends (``modes`` record).
    offered: dict[str, Any] | None = None
    #: The plan file read at a compaction, owed to the next request
    #: (``{"path", "text"}``).
    plan_file: dict[str, Any] | None = None


def state_of(view: TaskView) -> PermState:
    """The task's folded permissions state, with what this extension recorded
    on ``view`` that the runner has not folded yet (two mode switches taken
    in one step)."""
    found = view.ext_state(NAME)
    st = found if isinstance(found, PermState) else PermState()
    for ev in view.pending:
        if isinstance(ev, ExtensionRecorded) and ev.extension == NAME:
            st = fold(st, ev.kind, ev.data)
    return st


def inherited_rules(state: Any) -> list[str]:
    """A task's remembered rules, its own and those it inherited: what a
    child it spawns starts with (``config["permission_rules"]``)."""
    st = state.ext.get(NAME)
    own = list(st.rules) if isinstance(st, PermState) else []
    parent = state.config.get(INHERITED_RULES_KEY)
    rules = [x for x in parent if isinstance(x, str)] if isinstance(parent, list) else []
    return [*rules, *(x for x in own if x not in rules)]


def fold(state: Any, kind: str, data: Any) -> PermState:
    """One of this extension's records folded into its state."""
    st: PermState = state if isinstance(state, PermState) else PermState()
    d = data if isinstance(data, dict) else {}
    if kind == "rule":
        new = [x for x in d.get("rules") or [] if isinstance(x, str) and x not in st.rules]
        return replace(st, rules=(*st.rules, *new)) if new else st
    if kind == "mode":
        mode = str(d.get("mode"))
        due = st.due
        plan, reminders = st.plan, st.plan_reminders
        if mode == "plan" and isinstance(d.get("plan"), dict):
            plan, reminders = dict(d["plan"]), 0
            due = (*due, "plan_active", *(("plan_reentry",) if st.plan_left else ()))
        left_plan = st.mode == "plan" and mode != "plan"
        if left_plan:
            due = (*due, "plan_exited")
        due = _auto_exit(st, due, mode, plan)
        return replace(st, mode=mode, plan=plan, due=due, consecutive_denials=0,
                       plan_reminders=reminders, plan_left=st.plan_left or left_plan)
    if kind == "plan_exit":
        mode = str(d.get("mode") or "default")
        # Only the plan-exit note: the auto-mode note sent before planning
        # still stands (r2 m1).
        due = _auto_exit(st, (*st.due, "plan_exited"), mode, st.plan)
        return replace(st, mode=mode, due=due, plan_left=True,
                       plan_named=st.plan_named or bool(d.get("named")))
    if kind == "plan_name":
        name = d.get("name")
        return replace(st, plan_named=True, plan_name=st.plan_name
                       or (name if isinstance(name, str) else None))
    if kind == "noted":
        sent = set(d.get("kinds") or ())
        count = d.get("plan_reminders")
        noted = d.get("auto_noted")
        return replace(st, due=tuple(k for k in st.due if k not in sent),
                       auto_noted=noted if isinstance(noted, bool) else st.auto_noted,
                       plan_noted=int(d.get("plan_noted") or st.plan_noted),
                       plan_reminders=count if isinstance(count, int)
                       else st.plan_reminders,
                       plan_left=st.plan_left and "plan_reentry" not in sent)
    if kind == "compacted":
        # The notes sent before are gone: the auto-mode note goes out again
        # (with input newer than the compaction), and leaving auto mode owes
        # no exit note.
        extra: tuple[str, ...] = ("plan_active",) if d.get("plan") else ()
        due = tuple(k for k in st.due if k != "auto_exited")
        return replace(st, due=(*due, *extra), compacted=True, auto_noted=False,
                       guidance_after=int(d.get("seq") or st.guidance_after))
    if kind == "plan_file":
        return replace(st, plan_file=dict(d))
    if kind == "plan_file_sent":
        return replace(st, plan_file=None)
    if kind == "probed":
        return replace(st, probed=True)
    if kind == pm.MODES_KIND:
        return replace(st, offered=dict(d))
    if kind == "resumed":
        return replace(st, probed=False, compacted=False)
    if kind == "step":
        return replace(st, steps=st.steps + 1)
    if kind == "tool_call":
        return replace(st, tool_calls=st.tool_calls + 1)
    if kind == "cost":
        return replace(st, cost_usd=st.cost_usd + float(d.get("usd") or 0.0))
    if kind == "auto":
        return replace(st, auto=_keep({**st.auto, str(d["call_id"]): str(d["decision"])}))
    if kind == "asked":
        return replace(st, asked=_keep({**st.asked, str(d["call_id"]): dict(d)}))
    if kind == "classified":
        return _classified(st, d)
    if kind == "demoted":
        return replace(st, demoted=True)
    return st


def plan_classifier_on(pre_mode: str, *, auto_during_plan: bool) -> bool:
    """Whether plan mode entered from ``pre_mode`` runs with the auto-mode
    classifier, settled on entry: ``useAutoModeDuringPlan``
    (``auto_during_plan``) holds and planning began in auto mode. Claude
    Code also uses it for a plan begun in another mode when its auto-mode
    gate is open; noeta asks the person then (``parity/deviations.md``)."""
    return auto_during_plan and pre_mode == "auto"


def auto_active(mode: str | None, plan: dict[str, Any] | None) -> bool:
    """Whether auto mode is in force: in it, or planning with its classifier
    (the plan record's ``auto_during_plan``; Claude Code's auto mode stays
    active through such a plan)."""
    if mode == "auto":
        return True
    if mode != "plan":
        return False
    plan = plan or {}
    on = plan.get(AUTO_DURING_PLAN)
    return on if isinstance(on, bool) else plan.get("pre_mode") == "auto"


def _auto_exit(st: PermState, due: tuple[str, ...], mode: str,
               plan: dict[str, Any] | None) -> tuple[str, ...]:
    """``due`` with the exit note when this switch leaves auto mode after an
    auto-mode note was sent."""
    if st.auto_noted and auto_active(st.mode, st.plan) and not auto_active(mode, plan) \
            and "auto_exited" not in due:
        return (*due, "auto_exited")
    return due


def _classified(st: PermState, d: dict[str, Any]) -> PermState:
    classified = _keep({**st.classified, str(d["call_id"]): dict(d)})
    if d.get("decision") == "deny" and d.get("counted", True):
        cons, total = st.consecutive_denials + 1, st.total_denials + 1
    elif d.get("decision") == "allow":
        cons, total = 0, st.total_denials
    else:
        cons, total = st.consecutive_denials, st.total_denials
    if d.get("reset_total"):
        cons, total = 0, 0
    return replace(st, classified=classified, consecutive_denials=cons, total_denials=total)


def _keep(d: dict[str, Any]) -> dict[str, Any]:
    while len(d) > _KEEP:
        d.pop(next(iter(d)))
    return d
