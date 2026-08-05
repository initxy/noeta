"""Interrupt landing during a foreground delegation settles the ROOT resumable.

Two defects pinned here. First, the arm: while the drain drives children the
root rests ``suspended`` on its member wake with no lease of its own, so
``interrupt``'s old gate (lease probe only) armed nothing — Esc during
delegation was a complete no-op. The widened ``_turn_in_flight`` treats a
delegation-suspended root as a turn in flight.

Second, the landing: ``_abort_cancelled_drain`` assumed the root was already
terminal (true for ``cancel``, which writes ``TaskCancelled`` first). For an
interrupt the root is NOT terminal — it was left stranded on a
``SubtaskCompleted`` wake that could never fire, with a dangling spawn
``tool_use`` and a stale registry mark. The settle now lands it exactly like
the worker's stopped turn: dangling spawns closed with failed "interrupted"
results, next-goal suspend with ``reason="interrupted"``, mark discarded —
the conversation resumable by simply typing again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.fold import fold
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE, HumanResponseReceived
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.worker import STOP_INTERRUPTED_SUSPEND_REASON
from noeta.runtime.workspace import FsWriteMode
from noeta.testing.fake_llm import FakeLLMProvider

from tests._sdk_session import (
    default_coding_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)

PARENT_GOAL = "delegate the research to explore"
CHILD_GOAL = "research-topic-sigma and report back"
SPAWN_CALL_ID = "fg-spawn-1"


def _spawn_foreground() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=SPAWN_CALL_ID,
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": "explore", "goal": CHILD_GOAL},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": SPAWN_CALL_ID},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _host(tmp_path: Path, provider: FakeLLMProvider):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    host = make_host(
        make_registry(main, preset_spec("explore")),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=True,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        budget=default_coding_budget(),
    )
    return host, make_driver(host)


def _spawn_result_blocks(root: Any) -> list[ToolResultBlock]:
    return [
        b
        for m in root.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.call_id == SPAWN_CALL_ID
    ]


def test_interrupt_during_child_turn_lands_root_resumable(tmp_path: Path) -> None:
    calls: list[int] = []
    driver_ref: list[Any] = []
    root_ref: list[str] = []

    def responder(request: LLMRequest) -> LLMResponse:
        calls.append(1)
        if len(calls) == 1:
            return _spawn_foreground()  # the parent delegates
        if len(calls) == 2:
            # Esc pressed while the CHILD's model round is in flight.
            driver_ref[0].interrupt(root_ref[0], reason="user pressed stop")
            return _end("child-answer-should-be-abandoned")
        return _end("after-resume")

    provider = FakeLLMProvider(responder=responder)
    host, driver = _host(tmp_path, provider)
    driver_ref.append(driver)

    seeded = driver.seed_start(goal=PARENT_GOAL, agent="main")
    root_ref.append(seeded.task_id)
    out = driver.drive_seeded(seeded)

    # The root landed the worker's stopped-turn rest: next-goal suspend,
    # reason "interrupted" — not stranded on the member wake, not terminal.
    assert out.status == "suspended"
    root = fold(host.event_log, host.content_store, seeded.task_id)
    assert isinstance(root.wake_on, HumanResponseReceived)
    assert root.wake_on.handle == NEXT_GOAL_WAKE_HANDLE
    suspends = [
        e for e in host.event_log.read(seeded.task_id) if e.type == "TaskSuspended"
    ]
    assert suspends[-1].payload.reason == STOP_INTERRUPTED_SUSPEND_REASON

    # The abandoned child was cascade-cancelled, terminal on its own stream.
    child_ids = [
        e.payload.subtask_id
        for e in host.event_log.read(seeded.task_id)
        if e.type == "SubtaskSpawned"
    ]
    assert len(child_ids) == 1
    child_types = [e.type for e in host.event_log.read(child_ids[0])]
    assert "TaskCancelled" in child_types

    # The dangling spawn tool_use was closed with a failed interrupted result,
    # so the resumed request carries no dangling function call.
    paired = _spawn_result_blocks(root)
    assert len(paired) == 1
    assert paired[0].success is False
    assert "interrupted" in (paired[0].error or "")

    # The registry mark was consumed by the settle — not left to pre-abort
    # the conversation's next turn.
    assert not host.is_cancelled(seeded.task_id)

    # And the conversation genuinely resumes by typing again.
    resumed = driver.send_goal(seeded.task_id, goal="continue please")
    assert resumed.status == "suspended"
    texts = [
        " ".join(getattr(b, "text", "") for b in m.content)
        for m in fold(
            host.event_log, host.content_store, seeded.task_id
        ).runtime.messages
    ]
    assert any("after-resume" in t for t in texts)
    assert not any("abandoned" in t for t in texts)
