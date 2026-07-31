"""Turn interrupt — stopping an in-flight turn without ending the conversation.

``interrupt`` is the third member of the human-stop family: ``cancel`` kills
the conversation, ``close`` archives it, ``interrupt`` halts only the turn and
leaves the task resting at its next-goal suspend. These tests pin what
separates it from its two neighbours (not terminal, not closed, still
resumable), that the in-flight result really is abandoned, and that the
interrupted turn's events survive — interrupt is not a rewind.

The mid-turn stop is driven through ``FakeLLMProvider.responder``: it fires
``interrupt`` from inside the provider call, so the mark lands while the turn
is between its two cancel polls — exactly where a human pressing stop lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from tests._sdk_session import official_registry as official_agent_registry

from noeta.client import SdkHost
from noeta.core.fold import fold
from noeta.execution.driver import (
    InteractionDriver,
    TaskAlreadyTerminalError,
    multi_turn_policy_wrapper,
)
from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.protocols.messages import LLMRequest, LLMResponse, TextBlock, Usage
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.runtime.worker import STOP_INTERRUPTED_SUSPEND_REASON
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={},
    )


def _host(
    workspace: Path, *, provider: FakeLLMProvider
) -> tuple[SdkHost, InMemoryDispatcher, InMemoryEventLog]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=provider,
        model="gpt-test",
        workspace_dir=workspace,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
    )
    return host, dispatcher, event_log


def _texts(task: Any) -> list[str]:
    return [
        " ".join(getattr(b, "text", "") for b in getattr(m, "content", []))
        for m in task.runtime.messages
    ]


def _started(
    tmp_path: Path,
) -> tuple[InteractionDriver, SdkHost, InMemoryEventLog, FakeLLMProvider, str]:
    """A live conversation resting at its next-goal suspend after one turn."""
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(responses=[_end_turn("turn1")])
    host, _, event_log = _host(ws, provider=provider)
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    return driver, host, event_log, provider, started.task_id


def _interrupt_during_next_turn(
    driver: InteractionDriver,
    provider: FakeLLMProvider,
    task_id: str,
    *,
    reason: Optional[str] = None,
) -> None:
    """Arm the provider so the NEXT model round is interrupted from inside it."""

    def responder(request: LLMRequest) -> LLMResponse:  # noqa: ARG001
        driver.interrupt(task_id, reason=reason)
        return _end_turn("turn2-should-be-abandoned")

    provider.responder = responder


# ---------------------------------------------------------------------------
# The landing: stopped, but still a conversation
# ---------------------------------------------------------------------------


def test_interrupt_lands_the_turn_on_a_resumable_suspend(tmp_path: Path) -> None:
    driver, host, event_log, provider, task_id = _started(tmp_path)
    _interrupt_during_next_turn(driver, provider, task_id, reason="user pressed stop")

    out = driver.send_goal(task_id, goal="second")

    assert out.status == "suspended"
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE
    marker = next(e for e in event_log.read(task_id) if e.type == "TurnInterrupted")
    assert marker.payload.reason == "user pressed stop"
    assert marker.payload.interrupted_by == "user"


def test_interrupt_abandons_the_in_flight_result(tmp_path: Path) -> None:
    """The round the stop caught is dropped without being acted on — no
    assistant message from it reaches the conversation."""
    driver, host, event_log, provider, task_id = _started(tmp_path)
    _interrupt_during_next_turn(driver, provider, task_id)

    driver.send_goal(task_id, goal="second")

    texts = _texts(fold(event_log, host.content_store, task_id))
    assert any("turn1" in t for t in texts)
    assert not any("abandoned" in t for t in texts)


def test_interrupt_neither_terminates_nor_closes(tmp_path: Path) -> None:
    """What separates it from its two neighbours."""
    driver, host, event_log, provider, task_id = _started(tmp_path)
    _interrupt_during_next_turn(driver, provider, task_id)

    driver.send_goal(task_id, goal="second")

    task = fold(event_log, host.content_store, task_id)
    assert task.status == "suspended"
    assert not task.governance.closed
    types = {e.type for e in event_log.read(task_id)}
    assert "TaskCancelled" not in types
    assert "ConversationClosed" not in types


def test_interrupt_records_its_reason_on_the_suspend(tmp_path: Path) -> None:
    """The rest is self-describing: a park reached by a human stop, not by the
    model asking a question."""
    driver, host, event_log, provider, task_id = _started(tmp_path)
    _interrupt_during_next_turn(driver, provider, task_id)

    driver.send_goal(task_id, goal="second")

    suspends = [e for e in event_log.read(task_id) if e.type == "TaskSuspended"]
    assert suspends[-1].payload.reason == STOP_INTERRUPTED_SUSPEND_REASON
    # The turn before it parked for the ordinary reason — the tag is per-turn.
    assert suspends[0].payload.reason != STOP_INTERRUPTED_SUSPEND_REASON


def test_interrupt_keeps_the_partial_turn_on_the_stream(tmp_path: Path) -> None:
    """Interrupt is not a rewind: the stopped turn's history stays, and no
    re-base marker is written."""
    driver, host, event_log, provider, task_id = _started(tmp_path)
    before = len(event_log.read(task_id))
    _interrupt_during_next_turn(driver, provider, task_id)

    driver.send_goal(task_id, goal="second")

    after = event_log.read(task_id)
    assert len(after) > before
    assert after[:before] == event_log.read(task_id)[:before]
    types = [e.type for e in after]
    assert "TaskRewound" not in types
    # The stopped turn genuinely opened and composed before it was caught.
    assert types.count("TaskWoken") == 1
    assert "ContextPlanComposed" in types


def test_interrupt_then_send_goal_continues_the_conversation(
    tmp_path: Path,
) -> None:
    driver, host, event_log, provider, task_id = _started(tmp_path)
    _interrupt_during_next_turn(driver, provider, task_id)
    driver.send_goal(task_id, goal="second")

    # Disarm and drive a real turn.
    provider.responder = lambda request: _end_turn("turn3")  # noqa: ARG005
    out = driver.send_goal(task_id, goal="third")

    assert out.status == "suspended"
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE
    texts = _texts(fold(event_log, host.content_store, task_id))
    assert any("third" in t for t in texts)
    assert any("turn3" in t for t in texts)


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def test_interrupt_on_an_idle_conversation_leaves_it_usable(tmp_path: Path) -> None:
    """No turn in flight ⇒ nothing to stop. The request is recorded, but the
    cancel mark must NOT be armed for a turn the user has not sent yet."""
    driver, host, event_log, provider, task_id = _started(tmp_path)

    out = driver.interrupt(task_id, reason="nothing running")
    assert out.status == "suspended"

    provider.responder = lambda request: _end_turn("turn2")  # noqa: ARG005
    resumed = driver.send_goal(task_id, goal="second")

    assert resumed.status == "suspended"
    texts = _texts(fold(event_log, host.content_store, task_id))
    assert any("second" in t for t in texts)
    # The turn actually RAN — it was not swallowed by a stale stop mark.
    assert any("turn2" in t for t in texts)


def test_interrupt_refuses_a_terminal_task(tmp_path: Path) -> None:
    driver, _, _, _, task_id = _started(tmp_path)
    driver.cancel(task_id)
    with pytest.raises(TaskAlreadyTerminalError) as excinfo:
        driver.interrupt(task_id)
    assert excinfo.value.verb == "interrupt"
