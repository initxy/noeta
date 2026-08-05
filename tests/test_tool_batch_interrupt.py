"""A human stop landing inside a tool batch stops at the next call boundary.

Before the interrupt-responsiveness work the ``tool_calls`` handler ran the
whole batch with zero cancel polls — a stop landing during call 1 of N sat
through every remaining call. Now the handler polls the Engine's cooperative
predicate between calls: the not-yet-executed calls get synthesized
``success=False`` "interrupted" results (the same balance-the-batch move the
approval suspend makes), the batched tool message flushes durably, and
``TaskCancellationRequested`` unwinds to the worker's settle. Calls that
already ran keep their real results — the interrupted turn stays real history.

Structural invariant pinned here: every ``tool_use`` the model emitted has a
matching ``tool_result`` after the stop, so the resumed conversation composes
a provider request with no dangling function call.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.core.engine import Engine
from noeta.policies.stub import StubScriptedPolicy
from noeta.core.wiring import wire_default_observers
from noeta.protocols.decisions import ToolCall, ToolCallsDecision
from noeta.protocols.errors import TaskCancellationRequested
from noeta.protocols.messages import ToolResultBlock
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment


class _RecordingTool:
    """Minimal Tool that records its invocations into a shared list."""

    risk_level = "low"
    input_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}

    def __init__(self, name: str, invoked: list[str]) -> None:
        self.name = name
        self.description = f"records an invocation of {name}"
        self._invoked = invoked

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self._invoked.append(self.name)
        return ToolResult(success=True, output=f"{self.name}-out")


def _build(
    calls: list[ToolCall], invoked: list[str]
) -> tuple[Engine, InMemoryEventLog, str, Any]:
    dispatcher = InMemoryDispatcher()
    content_store = InMemoryContentStore()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    tools: dict[str, Any] = {
        name: _RecordingTool(name, invoked) for name in ("alpha", "beta", "gamma")
    }
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=StubScriptedPolicy([ToolCallsDecision(calls=calls)]),
        tools=tools,
        tool_runtime=ToolRuntime(
            event_log=event_log, content_store=content_store
        ),
    )
    task = engine.create_task(goal="batch-interrupt", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    return engine, event_log, lease.lease_id, task


def _result_blocks(task: Any) -> dict[str, ToolResultBlock]:
    out: dict[str, ToolResultBlock] = {}
    for msg in task.runtime.messages:
        if msg.role != "tool":
            continue
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                out[block.call_id] = block
    return out


def test_stop_during_call_one_skips_the_rest_and_balances_the_batch() -> None:
    invoked: list[str] = []
    calls = [
        ToolCall(tool_name="alpha", arguments={}, call_id="c-1"),
        ToolCall(tool_name="beta", arguments={}, call_id="c-2"),
        ToolCall(tool_name="gamma", arguments={}, call_id="c-3"),
    ]
    engine, log, lease_id, task = _build(calls, invoked)

    # The stop lands "during" call 1: the predicate turns truthy once alpha
    # has run, exactly as a registry mark set mid-execution would read at the
    # next poll.
    with pytest.raises(TaskCancellationRequested):
        engine.run_one_step(
            task, lease_id=lease_id, cancelled=lambda: bool(invoked)
        )

    # Only the first call executed.
    assert invoked == ["alpha"]
    starts = [
        e.payload.call_id for e in log.read(task.task_id)
        if e.type == "ToolCallStarted"
    ]
    assert starts == ["c-1"]

    # Balance: every call in the batch has a tool_result — the real one for
    # the executed call, synthesized failures for the stopped ones — so the
    # resumed request carries no dangling function call. (The scripted stub
    # emits no assistant tool_use message, so the result set is the pin.)
    results = _result_blocks(task)
    assert set(results) == {"c-1", "c-2", "c-3"}
    assert results["c-1"].success is True
    assert results["c-1"].output == "alpha-out"
    for stopped in ("c-2", "c-3"):
        assert results[stopped].success is False
        assert "interrupted" in (results[stopped].error or "")

    # The batched message flushed durably BEFORE the unwind.
    types = [e.type for e in log.read(task.task_id)]
    assert "MessagesAppended" in types


def test_no_cancel_seam_runs_the_whole_batch() -> None:
    """``cancelled=None`` (resume) keeps the historical run-everything path."""
    invoked: list[str] = []
    calls = [
        ToolCall(tool_name="alpha", arguments={}, call_id="c-1"),
        ToolCall(tool_name="beta", arguments={}, call_id="c-2"),
    ]
    engine, _log, lease_id, task = _build(calls, invoked)

    # StubScriptedPolicy is exhausted after the one batch; the second decide
    # raises its scripted StopIteration-shaped failure, so only assert the
    # batch itself ran to completion.
    try:
        engine.run_one_step(task, lease_id=lease_id, cancelled=None)
    except Exception:  # noqa: BLE001 — the post-batch decide is out of scope
        pass

    assert invoked == ["alpha", "beta"]
    results = _result_blocks(task)
    assert results["c-1"].success is True
    assert results["c-2"].success is True
