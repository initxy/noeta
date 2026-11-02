"""The USD budget note's cost: every model call of the task tree, the
compaction's included, read from the logs; a task that has ended is read
once."""

from __future__ import annotations

from noeta.core import models, records
from noeta.core.events import (
    Event,
    ModelReplied,
    Recorded,
    TaskCreated,
    TaskEnded,
)
from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.core.types import Message, ModelResponse, Text, Usage
from noeta.toolkit import usd_budget

MODEL = "claude-opus-5-5"
USED = Usage(uncached=1000, output=500)


def _reply() -> ModelReplied:
    return ModelReplied(response=ModelResponse(
        message=Message(role="assistant", content=[Text(text="ok")]), stop_reason="end_turn",
        usage=USED, model=MODEL))


class Logs:
    def __init__(self) -> None:
        self.logs: dict[str, list[Event]] = {}
        self.children: dict[str, list[str]] = {}
        self.reads: list[str] = []
        self.child_reads: list[str] = []

    async def read_log(self, task_id: str, after: int = 0) -> list[Recorded]:
        self.reads.append(task_id)
        return [Recorded(seq=i + 1, at=0.0, event=e)
                for i, e in enumerate(self.logs.get(task_id, [])) if i + 1 > after]

    async def child_tasks(self, task_id: str) -> list[str]:
        self.child_reads.append(task_id)
        return list(self.children.get(task_id, []))

    def view(self, root: str) -> TaskView:
        state = TaskState(task_id=root)
        return TaskView(state=state, read_log=self.read_log, child_tasks=self.child_tasks)


async def test_the_cost_counts_compaction_calls_and_reads_an_ended_task_once() -> None:
    usd_budget._read.clear()
    logs = Logs()
    spent = records.Spent(model=MODEL, usage=USED, purpose="compaction")
    logs.logs["root"] = [TaskCreated(agent="a", config={"model": MODEL}), _reply(),
                         records.event(spent)]
    logs.logs["child"] = [TaskCreated(agent="a", config={"model": MODEL}), _reply(),
                          TaskEnded(status="done")]
    logs.children["root"] = ["child"]
    one = models.price(MODEL, USED)
    view = logs.view("root")
    assert await usd_budget.conversation_cost(view) == 3 * one
    logs.reads.clear()
    logs.child_reads.clear()
    logs.logs["root"].append(_reply())
    assert await usd_budget.conversation_cost(view) == 4 * one
    # The ended child is neither read nor asked for its children again.
    assert logs.reads == ["root"] and logs.child_reads == ["root"]


