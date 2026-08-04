"""Anti-spiral must not false-kill a long session that legitimately compacts
more than once.

Anti-spiral is based on **boundary monotonic progress**: the policy only emits
a ``CompactionRequested`` whose boundary exceeds the cumulative
``summary_boundary`` already collapsed (else ``FailDecision`` /
``compaction_no_progress``), and the kernel escalates only when the boundary it
is about to write fails to advance. Real tool work grows the raw history, which
makes a larger prefix summarisable, which advances the boundary — so a second
proactive compaction is legitimate and must survive. The tension is that a
genuinely stuck compaction (boundary cannot move) must still terminate, and
these two cases are one line apart.

Both tests drive the **real** ``Engine`` + ``ReActPolicy`` +
``ThreeSegmentComposer`` + a real recording tool (no Stub policy, no
``fake_view``): the policy↔composer boundary contract only exists end to end,
so a fake view cannot falsify either side.
"""

from __future__ import annotations

from typing import Any

from noeta.context.composer import COMPOSER_VERSION, ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.builtins.react.impl import ReActPolicy
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolUseBlock,
)
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.llm import RuntimeLLMClient
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


_SYSTEM_PROMPT = "You are a coding agent. Work the task then finish."
_SUMMARY_MARKER = "Summarize the conversation so far"
#: Each assistant tool_use turn carries this much bulk in a TextBlock. Text
#: blocks are NEVER pruned by the Composer's ``_prune_tail`` (it only nullifies
#: stale tool-result OUTPUTS), so this is the part of the raw history that
#: keeps GROWING the projected estimate after a compaction has collapsed the
#: prior prefix — that growth is what forces a SECOND (legitimate) compaction
#: with a strictly larger summarise boundary.
_BULK = "z" * 700


class _BulkTool:
    """Real tool — its result is small (it gets pruned anyway); the growth that
    re-triggers compaction lives in the assistant TextBlocks, not here."""

    name = "work"
    risk_level = "low"
    input_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
    }

    def invoke(
        self, arguments: dict[str, Any], ctx: ToolContext  # noqa: ARG002
    ) -> ToolResult:
        step = arguments.get("step", "?")
        return ToolResult(success=True, output=f"ok-{step}", summary="ok")


class _MultiCompactionProvider:
    """Drive a real two-compaction session deterministically.

    * a summarize call (recognised by the fixed system marker) → summary text;
    * otherwise a normal turn → a bulky-TextBlock + ``work`` tool_use until the
      SECOND compaction has landed, then a finish.

    The compaction count is tracked by how many summarize calls have been
    served (each compaction = one summarize round-trip). Stateful is fine: this
    test asserts loop-behaviour on a single live drive.
    """

    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []
        self.summarize_calls = 0
        self._turn = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        system_text = ""
        if request.system is not None:
            system_text = "".join(
                b.text
                for b in request.system.content
                if isinstance(b, TextBlock)
            )
        if _SUMMARY_MARKER in system_text:
            self.summarize_calls += 1
            return LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="CONDENSED-SUMMARY")],
            )
        # Finish only AFTER a second compaction has actually happened.
        if self.summarize_calls >= 2:
            return LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="all done")],
            )
        self._turn += 1
        return LLMResponse(
            stop_reason="tool_use",
            content=[
                TextBlock(text=f"step {self._turn}: " + _BULK),
                ToolUseBlock(
                    call_id=f"call-{self._turn}",
                    tool_name="work",
                    arguments={"step": self._turn},
                ),
            ],
        )


def _seed_long_history(n: int) -> list[str]:
    return [f"turn-{i} " + "y" * 300 for i in range(n)]


def _policy(llm: Any) -> ReActPolicy:
    # Tiny window so the seeded history trips the proactive trigger; tail
    # budget protects only the last turn or two so a compaction collapses
    # most of the prefix and a later one can advance further as history grows.
    return ReActPolicy(
        llm=llm,
        tools={"work": _BulkTool()},
        system_prompt=_SYSTEM_PROMPT,
        model="gpt-4o",
        max_steps=30,
        context_window=900,
        max_output_tokens=100,
        compaction_buffer=100,
        tail_token_budget=150,
        composer_version=COMPOSER_VERSION,
    )


def _composer(content_store: Any) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt=_SYSTEM_PROMPT,
        tools={"work": _BulkTool()},
        content_store=content_store,
        tail_token_budget=150,
    )


def _run() -> tuple[
    str, InMemoryEventLog, InMemoryContentStore, Any, _MultiCompactionProvider
]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    wire_default_observers(event_log, dispatcher)
    provider = _MultiCompactionProvider()
    llm = RuntimeLLMClient(
        provider=provider, event_log=event_log, content_store=content_store
    )
    tool_runtime = ToolRuntime(
        event_log=event_log, content_store=content_store
    )
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=_composer(content_store),
        policy=_policy(llm),
        tools={"work": _BulkTool()},
        tool_runtime=tool_runtime,
        hooks=HookManager(),
    )
    task = engine.create_task(goal="long task", policy_name="react")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="rec")
    assert lease is not None
    for text in _seed_long_history(14):
        engine.append_user_message(task, content=[TextBlock(text=text)], lease_id=lease.lease_id)
    final = engine.run_one_step(task, lease_id=lease.lease_id)
    return task.task_id, event_log, content_store, final, provider


def test_two_legit_compactions_around_real_tool_work_complete() -> None:
    """Proactive compaction → real tool work (history grows) → a SECOND
    legitimate proactive compaction must SUCCEED (not TaskFailed), and the task
    completes. Any anti-spiral rule that judges by a sticky per-step tag rather
    than by boundary progress kills this session even though the second
    boundary strictly advanced.
    """
    task_id, log, _cs, final, _provider = _run()
    types = [e.type for e in log.read(task_id)]

    # Two compactions actually landed (each summarised a NEW, larger prefix).
    compacted = [e for e in log.read(task_id) if e.type == "Compacted"]
    assert len(compacted) >= 2, types
    # Each Compacted strictly advanced the boundary (monotonic progress).
    boundaries = [e.payload.boundary_count for e in compacted]
    assert all(b2 > b1 for b1, b2 in zip(boundaries, boundaries[1:])), boundaries

    # Real tool work happened between/around the compactions.
    assert "ToolResultRecorded" in types

    # The session COMPLETED — the second legit compaction was NOT false-killed.
    assert "TaskCompleted" in types
    assert "TaskFailed" not in types
    assert final.status == "terminal"


def test_second_summarize_input_is_previous_summary_plus_delta() -> None:
    """The bounded summarize input: the SECOND compaction's summarize request
    must open with the previous summary message (summary-of-summary), not
    re-send the whole raw prefix from index 0 — the unbounded shape is what let
    the Nth summarize call itself outgrow the window. This is the one place
    that notices if a Composer change breaks
    ``_previous_summary_message``'s shape-detection: that failure degrades
    SILENTLY to the unbounded input (deliberately — lossless is the safe
    direction), so the loop test above keeps passing either way."""
    _task_id, _log, _cs, _final, provider = _run()

    summarize_requests = [
        req
        for req in provider.calls
        if req.system is not None
        and any(
            _SUMMARY_MARKER in b.text
            for b in req.system.content
            if isinstance(b, TextBlock)
        )
    ]
    assert len(summarize_requests) >= 2

    head = summarize_requests[1].messages[0]
    assert head.role == "user"
    head_text = "".join(
        b.text for b in head.content if isinstance(b, TextBlock)
    )
    assert "CONDENSED-SUMMARY" in head_text


def test_stuck_compaction_with_no_boundary_progress_still_fails() -> None:
    """The complementary half: a genuinely stuck compaction (the trigger fires
    but no larger prefix is summarisable, so the boundary cannot advance) must
    still terminate — boundary-based anti-spiral must not be permissive.

    Here the protected tail budget swallows the WHOLE history, so the policy's
    summarise boundary is 0 and never advances: the policy self-terminates
    with a non-retryable ``FailDecision(compaction_no_progress)`` before
    emitting any ``CompactionRequested`` (no infinite loop, no burned steps).
    """
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    wire_default_observers(event_log, dispatcher)
    provider = _MultiCompactionProvider()
    llm = RuntimeLLMClient(
        provider=provider, event_log=event_log, content_store=content_store
    )
    composer = ThreeSegmentComposer(
        system_prompt=_SYSTEM_PROMPT,
        tools={},
        content_store=content_store,
        tail_token_budget=100_000,
    )
    policy = ReActPolicy(
        llm=llm,
        tools={},
        system_prompt=_SYSTEM_PROMPT,
        model="gpt-4o",
        max_steps=30,
        context_window=900,
        max_output_tokens=100,
        compaction_buffer=100,
        # Tail budget dwarfs the window: every message is "protected", so the
        # boundary is pinned at 0 — no prefix is ever summarisable.
        tail_token_budget=100_000,
        composer_version=COMPOSER_VERSION,
    )
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
    )
    task = engine.create_task(goal="stuck task", policy_name="react")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="rec")
    assert lease is not None
    for text in _seed_long_history(14):
        engine.append_user_message(task, content=[TextBlock(text=text)], lease_id=lease.lease_id)
    final = engine.run_one_step(task, lease_id=lease.lease_id)

    assert final.status == "terminal"
    failed = [e for e in event_log.read(task.task_id) if e.type == "TaskFailed"]
    assert len(failed) == 1
    assert failed[0].payload.retryable is False
    assert "compaction" in failed[0].payload.reason
    # The stuck path never produced a Compacted event (it failed before).
    assert not [
        e for e in event_log.read(task.task_id) if e.type == "Compacted"
    ]
