"""Claude Code's USD budget note (``--max-budget-usd``): with the task
config's ``max_budget_usd`` set, every model call is told what the whole
conversation (the root task and every task below it) has cost so far, the
budget and what is left. The cost is read from the tasks' logs, so any
worker tells the same; this process only remembers how far it has read."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from noeta.core import models, records
from noeta.core.events import (
    ExtensionRecorded,
    ModelCalled,
    ModelReplied,
    TaskCreated,
    TaskEnded,
)
from noeta.core.extension import Reminder, TaskView
from noeta.core.types import Usage

__all__ = ["BUDGET_KEY", "budget_text", "usd_budget_reminders"]

#: Task config: the conversation's budget in USD (``noeta -p --max-budget-usd``).
BUDGET_KEY = "max_budget_usd"
#: Tasks whose read position this process keeps.
_KEEP = 4096


@dataclass
class _Read:
    """How far this process has read one task's log: the seq, the cost so
    far, its model; once its log has ended (an ended task is never run
    again), its children too, so neither is asked for again."""

    seq: int = 0
    cost: float = 0.0
    model: str = ""
    ended: bool = False
    children: list[str] | None = None


_read: OrderedDict[str, _Read] = OrderedDict()


def _number(x: float) -> str:
    """A number as Claude Code prints it (JavaScript's form: ``5``, ``0.25``)."""
    return str(int(x)) if x == int(x) and abs(x) < 1e21 else repr(x)


def budget_text(used: float, total: float) -> str:
    """The note's fixed format, Claude Code's."""
    return (f"USD budget: ${_number(used)}/${_number(total)}; "
            f"${_number(total - used)} remaining")


def _price(model: str, usage: Usage) -> float:
    return models.price(model, usage) if model else 0.0


async def _task(view: TaskView, task_id: str) -> _Read:
    """``task_id``'s cost and children, reading only what its log added."""
    assert view.read_log is not None and view.child_tasks is not None
    entry = _read.pop(task_id, None) or _Read()
    if not entry.ended:
        for rec in await view.read_log(task_id, entry.seq):
            ev = rec.event
            if isinstance(ev, TaskCreated):
                entry.model = str(ev.config.get("model") or entry.model)
            elif isinstance(ev, ModelCalled):
                entry.model = ev.model or entry.model
            elif isinstance(ev, ModelReplied):
                entry.cost += _price(ev.response.model or entry.model, ev.response.usage)
            elif isinstance(ev, TaskEnded):
                entry.ended = True
            elif isinstance(ev, ExtensionRecorded):
                # A reply the kernel dropped (empty, or cut while reasoning),
                # and calls outside the conversation (a compaction's).
                core = records.decode(ev)
                if isinstance(core, (records.Continue, records.TurnDone)) \
                        and core.usage is not None:
                    entry.cost += _price(core.model or entry.model, core.usage)
                elif isinstance(core, records.Spent):
                    entry.cost += _price(core.model or entry.model, core.usage)
            entry.seq = rec.seq
    children = entry.children
    if children is None:
        children = await view.child_tasks(task_id)
        if entry.ended:  # read after its end: no child can be added
            entry.children = children
    _read[task_id] = entry
    while len(_read) > _KEEP:
        _read.popitem(last=False)
    return replace(entry, children=children)


async def conversation_cost(view: TaskView) -> float | None:
    """What the root task and every task below it have cost, in USD
    (``None`` when the logs cannot be read here). A task that has ended is
    read once: each call reads the logs of the tasks still running."""
    if view.read_log is None or view.child_tasks is None:
        return None
    total, pending, seen = 0.0, [view.state.root_id or view.task_id], set()
    while pending:
        task_id = pending.pop()
        if task_id in seen:
            continue
        seen.add(task_id)
        entry = await _task(view, task_id)
        total += entry.cost
        pending.extend(entry.children or ())
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
