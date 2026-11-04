"""The ``PlanMode`` extension: Claude Code's plan mode — ``/plan``,
``EnterPlanMode`` / ``ExitPlanMode``, the plan file and its name, and the
notes that tell the model it is planning and that planning is over."""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from typing import Any

from noeta.core.events import Compacted, External, InputTaken, Recorded, Say
from noeta.core.extension import (
    Ask,
    Command,
    CommandContext,
    Extension,
    Gate,
    Reminder,
    TaskView,
)
from noeta.core.tool import Tool, ToolContext, tool_flag
from noeta.core.types import Message, ToolUse
from noeta.plugins.plan_mode import notes as nt
from noeta.plugins.plan_mode import plan as pl
from noeta.plugins.plan_mode.state import (
    AUTO_DURING_PLAN,
    MODE_RECORDS,
    NAME,
    PLAN_NAME_KEY,
    PLAN_ORIGIN_KEY,
    PlanState,
    fold,
    plan_classifier_on,
    plans_with_classifier,
    state_of,
)
from noeta.toolkit import plugin_records as pr
from noeta.toolkit.paths import plans_dir as default_plans_dir
from noeta.toolkit.permission_modes import MODE_SIGNAL, PERMISSION_MODES
from noeta.toolkit.permission_rules import EDIT_TOOLS

__all__ = ["ASK_PLAN", "PlanMode"]

#: The ask for a call that would change things while planning, when no
#: permissions plugin judges it (the permissions plugin's wording).
ASK_PLAN = "{name} would change things while in plan mode"


class PlanMode(Extension):
    """Plan mode. Arguments a host sets:

    * ``plans_dir`` — where plan files go (default ``<noeta home>/plans``).
    * ``auto_during_plan`` — a plan begun in auto mode runs with the
      auto-mode classifier (Claude Code's top-level
      ``useAutoModeDuringPlan``, default on; the permissions plugin applies
      it). Settled when plan mode is entered.

    The task's mode is the permissions plugin's ``mode`` record, which this
    plugin writes for every switch into plan mode and out of it by
    ``ExitPlanMode``; a front end switches it with the ``permission_mode``
    signal. With the permissions plugin, it judges every call (plan mode's
    limits included). Without it, this plugin keeps planning read-only by
    itself: a call of a tool that is neither read-only nor
    ``safe_while_planning``, other than a write to the plan file, is asked
    about (a shell command too: telling a read-only one apart is the
    permissions plugin's)."""

    name = NAME
    folds = (MODE_RECORDS,)

    def __init__(self, *, plans_dir: str | None = None, auto_during_plan: bool = True) -> None:
        self.plans_dir = plans_dir or str(default_plans_dir())
        self.auto_during_plan = bool(auto_during_plan)
        self._exit_plan = pl.ExitPlanModeTool()
        self._enter_plan = pl.EnterPlanModeTool(self._enter_by_tool)

    # ------------------------------------------------------------ state

    def initial_state(self) -> PlanState:
        return PlanState()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return fold(state, kind, data)

    def inherit(self, state: Any) -> dict[str, Any]:
        """A child of a planning task plans too, as its parent does, with a
        plan file named after its parent's."""
        out: dict[str, Any] = {}
        st = state.ext.get(NAME)
        st = st if isinstance(st, PlanState) else PlanState()
        name = state.config.get(PLAN_NAME_KEY) if isinstance(state.config, dict) else None
        path = (st.plan or {}).get("path")
        if not isinstance(name, str) and isinstance(path, str):
            name = posixpath.splitext(posixpath.basename(path))[0]
        if isinstance(name, str) and name:
            out[PLAN_NAME_KEY] = name
        if st.planning:
            assert st.plan is not None
            out["permission_mode"] = "plan"
            out[PLAN_ORIGIN_KEY] = {"pre_mode": st.plan.get("pre_mode"),
                                    AUTO_DURING_PLAN: plans_with_classifier(st.plan)}
        return out

    def _current(self, view: TaskView, st: PlanState) -> str:
        """The task's mode: as last recorded, else its config's."""
        for mode in (st.mode, view.state.config.get("permission_mode")):
            if isinstance(mode, str) and mode in PERMISSION_MODES:
                return mode
        return "default"

    # ------------------------------------------------------------ tools and commands

    def tools(self, view: TaskView) -> Sequence[Tool]:
        # Sub-agents do not plan for approval (none is offered it).
        if view.state.parent_id:
            return ()
        return (self._enter_plan, self._exit_plan)

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return (Command(name="plan", description="Switch to plan mode; with a request, plan it",
                        argument_hint="[request]"),)

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        return nt.PLAN_COMMAND.format(args=args)

    # ------------------------------------------------------------ the gate

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        if _permissions_on(view):
            return None
        st = state_of(view)
        if not st.planning or call.name in (pl.ENTER_PLAN_MODE, pl.EXIT_PLAN_MODE):
            return None
        tool = view.find_tool(call.name)
        # A tool no plugin offers as its own (one a plugin builds from the
        # others', such as ToolSearch, or none at all) changes nothing.
        if tool is None or getattr(tool, "read_only", False) is True \
                or tool_flag(tool, "safe_while_planning"):
            return None
        if call.name in EDIT_TOOLS and self._plan_file(view, st, call):
            return None
        return Ask(prompt=ASK_PLAN.format(name=call.name),
                   detail={"by": NAME, "tool": call.name, "reason": ASK_PLAN.format(
                       name=call.name)})

    def _plan_file(self, view: TaskView, st: PlanState, call: ToolUse) -> bool:
        path = call.input.get("file_path") or call.input.get("notebook_path")
        if not isinstance(path, str) or not path or st.plan is None:
            return False
        if not posixpath.isabs(path):
            path = posixpath.join(view.cwd or "/", path)
        return posixpath.normpath(path) == posixpath.normpath(str(st.plan.get("path")))

    # ------------------------------------------------------------ events

    async def on_start(self, view: TaskView) -> None:
        """A task that starts in plan mode (its config's ``permission_mode``,
        the permissions plugin's starting mode, a sub-agent of a planning
        task) is recorded as entered, so it plans from its first request."""
        st = state_of(view)
        if st.mode is not None:
            return
        config = view.state.config
        if (pr.mode_of(view) or config.get("permission_mode")) != "plan":
            return
        # A sub-agent of a planning task plans as its parent does (the
        # parent's plan origin); the permissions plugin knows which modes
        # this host allows to start from.
        origin = config.get(PLAN_ORIGIN_KEY)
        origin = origin if isinstance(origin, dict) else {}
        pre = pr.plan_start_mode(view) or next(
            (m for m in (origin.get("pre_mode"),) if m in PERMISSION_MODES and m != "plan"),
            "default")
        classify = origin.get(AUTO_DURING_PLAN)
        nt.plan_start(view, pre, plans_dir=self.plans_dir,
                      parent_plan=config.get(PLAN_NAME_KEY),
                      classify=classify if isinstance(classify, bool) else plan_classifier_on(
                          pre, auto_during_plan=self.auto_during_plan))

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if isinstance(ev, InputTaken):
            for item in ev.items:
                self._on_input(view, item.body, recorded.seq)
        elif isinstance(ev, Compacted) and state_of(view).mode == "plan":
            view.record(NAME, "compacted", {"plan": True})

    def _on_input(self, view: TaskView, body: Any, seq: int) -> None:
        if isinstance(body, External) and body.name == MODE_SIGNAL:
            mode = body.data.get("mode") if isinstance(body.data, dict) else None
            if mode == "plan":
                self._enter(view, seq)
            elif not _permissions_on(view) and isinstance(mode, str) \
                    and mode in PERMISSION_MODES and mode != state_of(view).mode:
                # Leaving plan mode is the permissions plugin's switch when
                # there is one (it knows which modes this host allows).
                view.record(MODE_RECORDS, "mode", {"mode": mode})
        elif isinstance(body, External) and body.name == pl.PLAN_NAME_SIGNAL:
            name = (body.data or {}).get("name") if isinstance(body.data, dict) else None
            if isinstance(name, str) and name and state_of(view).plan_name is None:
                view.record(NAME, "plan_name", {"name": name})
                view.notify(f"plan: {name}")
        elif isinstance(body, Say) and body.message.role == "user" and \
                body.message.text.startswith(nt.PLAN_COMMAND_HEAD):
            self._enter(view, seq, by_command=True)

    def _enter(self, view: TaskView, seq: int, *, by_command: bool = False) -> None:
        st = state_of(view)
        current = self._current(view, st)
        if current == "plan":
            return
        view.record(MODE_RECORDS, "mode", nt.plan_record(
            st, current, self.plans_dir, seq, auto_during_plan=self.auto_during_plan,
            by_command=by_command))

    def _enter_by_tool(self, ctx: ToolContext) -> bool:
        """``EnterPlanMode``: record the switch to plan mode, as ``/plan``
        does; ``False`` when the task is planning already."""
        st = ctx.state.get(NAME)
        st = st if isinstance(st, PlanState) else PlanState()
        current = st.mode or "default"
        if current == "plan":
            return False
        ctx.record(MODE_RECORDS, "mode", nt.plan_record(
            st, current, self.plans_dir, 0, auto_during_plan=self.auto_during_plan))
        return True

    # ------------------------------------------------------------ requests

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        return [*nt.plan_file_reminders(view), *await nt.plan_reminders(view, state_of(view))]

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """At a compaction: the plan file, given again with the next request."""
        await nt.plan_file_resident(view)
        return ()


def _permissions_on(view: TaskView) -> bool:
    """Whether the task runs the permissions plugin, which then judges
    every call and owns the switches between the other modes."""
    return pr.plugin(view, pr.PERMISSIONS) is not None
