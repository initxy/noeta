"""What the plan-mode plugin folds: its own records, and the mode switches
recorded under the permissions plugin's name (``mode``, ``plan_exit``:
where front ends read the task's mode, and where a log written before plan
mode was its own plugin keeps all of its plan records)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from noeta.core.events import ExtensionRecorded
from noeta.core.extension import TaskView
from noeta.toolkit.plugin_records import PERMISSIONS, PLAN_MODE

NAME = PLAN_MODE
#: The record name mode switches go under, plan mode's included: front
#: ends read the mode there (``noeta.toolkit.permission_modes``) and the
#: permissions plugin folds it from there.
MODE_RECORDS = PERMISSIONS
#: In a plan record: whether this plan runs with the auto-mode classifier,
#: settled when plan mode is entered (absent, in a log written before
#: noeta recorded it: when planning began in auto mode).
AUTO_DURING_PLAN = "auto_during_plan"
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
class PlanState:
    """Folded from this plugin's records and the mode switches."""

    #: The permission mode last switched to (``None``: none recorded).
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
    #: Notes owed to the next request: ``plan_exited``, ``plan_active``,
    #: ``plan_reentry``.
    due: tuple[str, ...] = ()
    #: The seq of the newest prompt the plan-mode text was sent for.
    plan_noted: int = 0
    #: The plan file read at a compaction, owed to the next request
    #: (``{"path", "text"}``).
    plan_file: dict[str, Any] | None = None

    @property
    def planning(self) -> bool:
        return self.mode == "plan" and self.plan is not None


def state_of(view: TaskView) -> PlanState:
    """The task's folded plan-mode state, with what was recorded on ``view``
    that the runner has not folded yet (two switches taken in one step)."""
    found = view.ext_state(NAME)
    st = found if isinstance(found, PlanState) else PlanState()
    for ev in view.pending:
        if isinstance(ev, ExtensionRecorded) and ev.extension in (NAME, MODE_RECORDS):
            st = fold(st, ev.kind, ev.data)
    return st


def fold(state: Any, kind: str, data: Any) -> PlanState:
    """One record folded in (this plugin's, or one under ``MODE_RECORDS``;
    the kinds are named alike in both, as a log written before the split
    has them all under ``MODE_RECORDS``)."""
    st: PlanState = state if isinstance(state, PlanState) else PlanState()
    d = data if isinstance(data, dict) else {}
    if kind == "mode":
        mode = str(d.get("mode"))
        due, plan, reminders = st.due, st.plan, st.plan_reminders
        if mode == "plan" and isinstance(d.get("plan"), dict):
            plan, reminders = dict(d["plan"]), 0
            due = (*due, "plan_active", *(("plan_reentry",) if st.plan_left else ()))
        left_plan = st.mode == "plan" and mode != "plan"
        if left_plan:
            due = (*due, "plan_exited")
        return replace(st, mode=mode, plan=plan, due=due, plan_reminders=reminders,
                       plan_left=st.plan_left or left_plan)
    if kind == "plan_exit":
        return replace(st, mode=str(d.get("mode") or "default"),
                       due=(*st.due, "plan_exited"), plan_left=True,
                       plan_named=st.plan_named or bool(d.get("named")))
    if kind == "plan_name":
        name = d.get("name")
        return replace(st, plan_named=True, plan_name=st.plan_name
                       or (name if isinstance(name, str) else None))
    if kind == "noted":
        sent = set(d.get("kinds") or ())
        count = d.get("plan_reminders")
        return replace(st, due=tuple(k for k in st.due if k not in sent),
                       plan_noted=int(d.get("plan_noted") or st.plan_noted),
                       plan_reminders=count if isinstance(count, int) else st.plan_reminders,
                       plan_left=st.plan_left and "plan_reentry" not in sent)
    if kind == "compacted" and d.get("plan"):
        # The plan-mode text sent before is gone: it goes out again.
        return replace(st, due=(*st.due, "plan_active"))
    if kind == "plan_file":
        return replace(st, plan_file=dict(d))
    if kind == "plan_file_sent":
        return replace(st, plan_file=None)
    return st


def plan_classifier_on(pre_mode: str, *, auto_during_plan: bool) -> bool:
    """Whether plan mode entered from ``pre_mode`` runs with the auto-mode
    classifier, settled on entry: ``useAutoModeDuringPlan``
    (``auto_during_plan``) holds and planning began in auto mode. Claude
    Code also uses it for a plan begun in another mode when its auto-mode
    gate is open; noeta asks the person then (``parity/deviations.md``)."""
    return auto_during_plan and pre_mode == "auto"


def plans_with_classifier(plan: dict[str, Any]) -> bool:
    """Whether ``plan`` runs with the auto-mode classifier (its record's
    flag; a log from before the flag: when planning began in auto mode)."""
    on = plan.get(AUTO_DURING_PLAN)
    return on if isinstance(on, bool) else plan.get("pre_mode") == "auto"
