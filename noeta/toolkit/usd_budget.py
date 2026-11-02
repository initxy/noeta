"""Claude Code's USD budget note (``--max-budget-usd``): with the task
config's ``max_budget_usd`` set, every model call is told what the whole
conversation (the root task and every task below it) has cost so far, the
budget and what is left. The cost is read from the tasks' logs, so any
worker tells the same; this process only remembers how far it has read."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from noeta.core import models, records
from noeta.core.events import ExtensionRecorded, ModelCalled, ModelReplied, TaskCreated
from noeta.core.extension import Reminder, TaskView

__all__ = ["BUDGET_KEY", "budget_text", "usd_budget_reminders"]

#: Task config: the conversation's budget in USD (``noeta -p --max-budget-usd``).
BUDGET_KEY = "max_budget_usd"
#: Tasks whose read position this process keeps.
_KEEP = 4096
#: task id -> (seq read through, cost so far, its model).
_read: OrderedDict[str, tuple[int, float, str]] = OrderedDict()


def _number(x: float) -> str:
    """A number as Claude Code prints it (JavaScript's form: ``5``, ``0.25``)."""
    return str(int(x)) if x == int(x) and abs(x) < 1e21 else repr(x)


def budget_text(used: float, total: float) -> str:
    """The note's fixed format, Claude Code's."""
    return (f"USD budget: ${_number(used)}/${_number(total)}; "
            f"${_number(total - used)} remaining")


async def _task_cost(view: TaskView, task_id: str) -> float:
    assert view.read_log is not None
    seq, cost, model = _read.pop(task_id, (0, 0.0, ""))
    for rec in await view.read_log(task_id, seq):
        ev = rec.event
        if isinstance(ev, TaskCreated):
            model = str(ev.config.get("model") or model)
        elif isinstance(ev, ModelCalled):
            model = ev.model or model
        elif isinstance(ev, ModelReplied):
            used = ev.response.model or model
            cost += models.price(used, ev.response.usage) if used else 0.0
        elif isinstance(ev, ExtensionRecorded):
            # A reply the kernel dropped (empty, or cut while reasoning).
            core = records.decode(ev)
            if isinstance(core, (records.Continue, records.TurnDone)) and core.usage is not None:
                used = core.model or model
                cost += models.price(used, core.usage) if used else 0.0
        seq = rec.seq
    _read[task_id] = (seq, cost, model)
    while len(_read) > _KEEP:
        _read.popitem(last=False)
    return cost


async def conversation_cost(view: TaskView) -> float | None:
    """What the root task and every task below it have cost, in USD
    (``None`` when the logs cannot be read here)."""
    if view.read_log is None or view.child_tasks is None:
        return None
    total, pending, seen = 0.0, [view.state.root_id or view.task_id], set()
    while pending:
        task_id = pending.pop()
        if task_id in seen:
            continue
        seen.add(task_id)
        total += await _task_cost(view, task_id)
        pending.extend(await view.child_tasks(task_id))
    return total


async def usd_budget_reminders(view: TaskView, order: int) -> Sequence[Reminder]:
    """The note for this model call, as a system note at ``order``."""
    budget: Any = view.state.config.get(BUDGET_KEY)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)):
        return ()
    used = await conversation_cost(view)
    if used is None:
        return ()
    return (Reminder(text=budget_text(used, float(budget)), kind="usd_budget", data={},
                     role="system", order=order),)
