"""Lifetime budget caps: a task's model calls, tool calls, cost, spawned
sub-agents and sub-agent depth (Claude Code's SDK has ``maxTurns`` and
``maxBudgetUsd``). A call past a cap is refused. The counts are folded from
the task's log, so any worker enforces the same caps."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from noeta.core import records as core_records
from noeta.core.events import ExtensionRecorded, ModelReplied, Recorded
from noeta.core.extension import Deny, Extension, Gate, TaskView
from noeta.core.models import price
from noeta.core.tool import ToolOutput
from noeta.core.types import ModelRequest, ToolUse

__all__ = ["BudgetCaps", "BudgetState", "Caps", "Counters", "budget_verdict"]

NAME = "budget"
#: A log from before this plugin was split out of the permissions plugin
#: has its counts (``step``, ``tool_call``, ``cost``) under that name.
FORMER_NAME = "permissions"


@dataclass(frozen=True)
class Caps:
    """Caps on a task's whole life (one task is a whole conversation, so
    size them for the longest conversation you will fund). ``None`` = no
    cap. ``max_subtask_depth``: root depth is 0, so ``1`` lets the root
    spawn a child but denies that child a grandchild."""

    max_iterations: int | None = None
    max_tool_calls: int | None = None
    max_cost_usd: float | None = None
    max_spawned_subtasks: int | None = None
    max_subtask_depth: int | None = 3


@dataclass(frozen=True)
class Counters:
    """What the caps are checked against. ``iterations`` counts the model
    call that produced the call being checked."""

    iterations: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    spawned: int = 0
    depth: int = 0


def budget_verdict(b: Caps, c: Counters, *, spawns: bool) -> str | None:
    """The deny reason, or ``None``. Iterations compare with ``>`` (the
    in-flight model call is already counted); the rest with ``>=``."""
    if b.max_iterations is not None and c.iterations > b.max_iterations:
        return f"max_iterations={b.max_iterations} exceeded"
    if b.max_cost_usd is not None and c.cost_usd >= b.max_cost_usd:
        return f"max_cost_usd={b.max_cost_usd} reached"
    if b.max_tool_calls is not None and c.tool_calls >= b.max_tool_calls:
        return f"max_tool_calls={b.max_tool_calls} reached"
    if spawns:
        if b.max_spawned_subtasks is not None and c.spawned >= b.max_spawned_subtasks:
            return f"max_spawned_subtasks={b.max_spawned_subtasks} reached"
        if b.max_subtask_depth is not None and c.depth >= b.max_subtask_depth:
            return f"max_subtask_depth={b.max_subtask_depth} reached"
    return None


@dataclass(frozen=True)
class BudgetState:
    """Folded from this plugin's records."""

    steps: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0


def fold(state: Any, kind: str, data: Any) -> BudgetState:
    st: BudgetState = state if isinstance(state, BudgetState) else BudgetState()
    if kind == "step":
        return replace(st, steps=st.steps + 1)
    if kind == "tool_call":
        return replace(st, tool_calls=st.tool_calls + 1)
    if kind == "cost":
        usd = data.get("usd") if isinstance(data, dict) else None
        return replace(st, cost_usd=st.cost_usd + float(usd or 0.0))
    return st


class BudgetCaps(Extension):
    """Arguments (each ``None`` for no cap): ``max_iterations`` (model
    calls; ``max_turns`` is another name for it), ``max_tool_calls``,
    ``max_cost_usd``, ``max_spawned_subtasks`` and ``max_subtask_depth``
    (default 3), the last two checked on the calls of ``spawn_tools``. Only
    a cap that is set is counted."""

    name = NAME
    folds = (FORMER_NAME,)

    def __init__(self, *, max_iterations: int | None = None, max_turns: int | None = None,
                 max_tool_calls: int | None = None, max_cost_usd: float | None = None,
                 max_spawned_subtasks: int | None = None, max_subtask_depth: int | None = 3,
                 spawn_tools: Iterable[str] = ("Agent", "Task")) -> None:
        if max_turns is not None and max_iterations is not None:
            raise ValueError(
                "Both `max_iterations` and `max_turns` are set — they express the same "
                "iteration cap and cannot be supplied together (ambiguous).")
        self.caps = Caps(max_iterations=max_iterations if max_turns is None else max_turns,
                         max_tool_calls=max_tool_calls, max_cost_usd=max_cost_usd,
                         max_spawned_subtasks=max_spawned_subtasks,
                         max_subtask_depth=max_subtask_depth)
        self.spawn_tools = frozenset(spawn_tools)

    def initial_state(self) -> BudgetState:
        return BudgetState()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return fold(state, kind, data)

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        reason = budget_verdict(self.caps, self._counters(view, call),
                                spawns=call.name in self.spawn_tools)
        return Deny(reason=reason) if reason is not None else None

    def _counters(self, view: TaskView, call: ToolUse) -> Counters:
        st = _state(view)
        before = 0
        for cl in view.state.calls:
            if cl.use.id == call.id:
                break
            if cl.result is None:
                before += 1
        depth = view.state.config.get("subtask_depth")
        if not isinstance(depth, int):
            depth = 1 if view.state.parent_id else 0
        return Counters(iterations=st.steps, tool_calls=st.tool_calls + before,
                        cost_usd=st.cost_usd, spawned=len(view.state.spawned), depth=depth)

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        if self.caps.max_iterations is not None:
            view.record(NAME, "step")
        return request

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        if self.caps.max_tool_calls is not None:
            view.record(NAME, "tool_call")
        return output

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        if self.caps.max_cost_usd is None:
            return
        ev = recorded.event
        usage, model = None, ""
        if isinstance(ev, ModelReplied):
            usage, model = ev.response.usage, ev.response.model
        elif isinstance(ev, ExtensionRecorded) and ev.extension == core_records.CORE:
            # What ``/cost`` counts besides replies: a reply the kernel
            # dropped, and calls outside the conversation (a compaction's).
            core = core_records.decode(ev)
            if isinstance(core, (core_records.Continue, core_records.TurnDone,
                                 core_records.Spent)):
                usage, model = core.usage, getattr(core, "model", "")
        if usage is None:
            return
        model = model or str(view.state.config.get("model") or "")
        view.record(NAME, "cost", {"usd": price(model, usage) if model else 0.0})


def _state(view: TaskView) -> BudgetState:
    found = view.ext_state(NAME)
    return found if isinstance(found, BudgetState) else BudgetState()
