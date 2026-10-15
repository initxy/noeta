"""Concurrency-safe tool calls of one response run together (Claude Code's
``isConcurrencySafe`` partition, at most ``MAX_TOOL_USE_CONCURRENCY`` at a
time), while the recorded stream and the results keep call order.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from noeta.agent.spec import BudgetSpec
from noeta.protocols.decisions import ToolCall
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.tool import ToolContext, ToolResult, is_concurrency_safe
from noeta.runtime.tool import MAX_TOOL_USE_CONCURRENCY, ToolRuntime
from noeta.sdk import Options, query
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.decorator import DecoratedTool


class _Probe(DecoratedTool):
    """A tool that records when its body runs; with ``barrier`` it blocks
    until its partners run at the same time."""

    def __init__(
        self,
        name: str,
        *,
        safe: bool,
        log: list[str],
        barrier: threading.Barrier | None = None,
        delay: float = 0.0,
    ) -> None:
        super().__init__(
            self._body,
            name=name,
            version="1",
            risk_level="low",
            input_schema={"type": "object", "additionalProperties": True},
        )
        self.concurrency_safe = safe
        self._log = log
        self._barrier = barrier
        self._delay = delay

    def _body(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tag = str(arguments.get("tag", ""))
        self._log.append(f"start {tag}")
        if self._barrier is not None:
            self._barrier.wait(timeout=5)
        time.sleep(self._delay)
        self._log.append(f"end {tag}")
        return ToolResult(success=True, output=f"{self.name}:{tag}")


def test_concurrency_cap_is_claude_codes() -> None:
    assert MAX_TOOL_USE_CONCURRENCY == 10


def test_concurrency_safe_is_opt_in() -> None:
    assert is_concurrency_safe(_Probe("x", safe=True, log=[]))
    assert not is_concurrency_safe(object())


def test_complete_runs_bodies_together_and_records_in_order() -> None:
    log: list[str] = []
    barrier = threading.Barrier(3)
    tool = _Probe("probe", safe=True, log=log, barrier=barrier)
    event_log = InMemoryEventLog()
    runtime = ToolRuntime(event_log=event_log, content_store=InMemoryContentStore())
    started = [
        runtime.start(
            tool,
            ToolCall(tool_name="probe", arguments={"tag": t}, call_id=f"c{t}"),
            task_id="t",
            lease_id="l",
            trace_id="tr",
        )
        for t in ("1", "2", "3")
    ]
    results = runtime.complete(started)
    assert [r.output for r in results] == ["probe:1", "probe:2", "probe:3"]
    assert not barrier.broken
    stream = [
        (e.type, getattr(e.payload, "call_id", None)) for e in event_log.read("t")
    ]
    assert stream == [
        ("ToolCallStarted", "c1"),
        ("ToolCallStarted", "c2"),
        ("ToolCallStarted", "c3"),
        ("ToolResultRecorded", "c1"),
        ("ToolCallFinished", "c1"),
        ("ToolResultRecorded", "c2"),
        ("ToolCallFinished", "c2"),
        ("ToolResultRecorded", "c3"),
        ("ToolCallFinished", "c3"),
    ]


def _response(*calls: tuple[str, str]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id=f"c{tag}", tool_name=name, arguments={"tag": tag})
            for name, tag in calls
        ],
        usage=Usage(uncached=1, output=1),
    )


def _run(
    tmp_path: Path,
    tools: list[_Probe],
    response: LLMResponse,
    budget: BudgetSpec | None = None,
):
    provider = FakeLLMProvider(
        responses=[
            response,
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="done")],
                usage=Usage(uncached=1, output=1),
            ),
        ]
    )
    result = query(
        Options(
            system_prompt="x",
            name="main",
            permission_mode="bypassPermissions",
            allowed_tools=tuple(tools),
            budget=budget,
        ),
        goal="go",
        provider=provider,
        workspace_dir=tmp_path,
    )
    assert result.answer() == "done"
    follow_up = provider.received_requests[1].messages
    results = [
        b
        for m in follow_up
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    return results


def test_safe_calls_of_one_response_run_together(tmp_path: Path) -> None:
    log: list[str] = []
    barrier = threading.Barrier(2)
    look = _Probe("Look", safe=True, log=log, barrier=barrier)
    results = _run(tmp_path, [look], _response(("Look", "a"), ("Look", "b")))
    assert not barrier.broken
    assert [(r.call_id, r.success) for r in results] == [("ca", True), ("cb", True)]


def test_an_unsafe_call_splits_the_run_and_order_holds(tmp_path: Path) -> None:
    log: list[str] = []
    look = _Probe("Look", safe=True, log=log, delay=0.05)
    write = _Probe("Change", safe=False, log=log)
    results = _run(
        tmp_path,
        [look, write],
        _response(("Look", "a"), ("Look", "b"), ("Change", "w"), ("Look", "c")),
    )
    assert [r.call_id for r in results] == ["ca", "cb", "cw", "cc"]
    # The unsafe call starts only after both earlier reads ended, and the
    # last read only after it.
    assert log.index("start w") > max(log.index("end a"), log.index("end b"))
    assert log.index("start c") > log.index("end w")


def test_each_guard_sees_the_calls_started_before_it(tmp_path: Path) -> None:
    """A budget of one call admits the first safe call and denies the second:
    the second guard runs after the first call's ``ToolCallStarted``."""
    log: list[str] = []
    look = _Probe("Look", safe=True, log=log)
    results = _run(
        tmp_path,
        [look],
        _response(("Look", "a"), ("Look", "b")),
        budget=BudgetSpec(max_tool_calls=1, max_subtask_depth=3),
    )
    assert [(r.call_id, r.success) for r in results] == [("ca", True), ("cb", False)]
    assert log == ["start a", "end a"]
