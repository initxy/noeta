"""``InteractionDriver.inject_goal`` / ``Client.inject_goal`` — verb dispatch.

One verb, three landings on the folded task status:

* **running** — write a durable ``InjectionRequested`` (lease-free ``system_emit``,
  the same seam ``cancel`` uses) and poke the process-local inbox; return the
  still-``running`` outcome without taking a lease. The running step loop drains
  it at its next turn boundary (proved end-to-end in
  ``tests/test_mid_turn_injection.py``).
* **suspended on the next-goal handle** — fall through to ``send_goal`` (wake +
  lease + drive), the ordinary follow-up turn.
* **anything else** (terminal, or a different wake handle) — the typed
  ``NotResumableError``, exactly as the other human commands give.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._sdk_session import official_registry as official_agent_registry

from noeta.client import SdkHost
from noeta.core.fold import fold
from noeta.execution.driver import (
    InteractionDriver,
    NotResumableError,
    multi_turn_policy_wrapper,
)
from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _host(workspace: Path, *, responses: list[LLMResponse]):
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        workspace_dir=workspace,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
    )
    return host, dispatcher, event_log


def test_inject_goal_on_next_goal_falls_through_to_send_goal(tmp_path: Path) -> None:
    """A task resting on the next-goal handle has no turn in flight, so
    ``inject_goal`` behaves exactly like ``send_goal``: it appends the message,
    drives the turn, and lands back on the next-goal suspend."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("t1"), _end_turn("t2")])
    driver = InteractionDriver(host)

    started = driver.start(goal="hello", agent="main")
    assert started.wake_handle == NEXT_GOAL_WAKE_HANDLE

    out = driver.inject_goal(started.task_id, goal="follow-up")
    # Drove a real turn: back on the next-goal suspend, message in the ledger.
    assert out.status == "suspended"
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE
    # No InjectionRequested was written — the fall-through path uses send_goal.
    types = [e.type for e in event_log.read(started.task_id)]
    assert "InjectionRequested" not in types
    assert types.count("TaskWoken") >= 1


def test_inject_goal_without_drive_refuses_a_parked_task_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """``drive=False`` is for a caller that must never drive a turn itself (a
    host's wake pump): a task resting on the next-goal handle gets the typed
    refusal instead of a ``send_goal`` on the caller's thread, and the log is
    untouched so the caller can seed the follow-up its own way."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("t1"), _end_turn("t2")])
    driver = InteractionDriver(host)

    started = driver.start(goal="hello", agent="main")
    before = len(event_log.read(started.task_id))
    with pytest.raises(NotResumableError) as exc:
        driver.inject_goal(started.task_id, goal="follow-up", drive=False)
    assert exc.value.status == "suspended"
    assert "not waiting for a running turn" in str(exc.value)
    assert len(event_log.read(started.task_id)) == before
    # the ordinary landing is unchanged: the same call with ``drive`` drives
    out = driver.inject_goal(started.task_id, goal="follow-up")
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE


def test_inject_goal_on_running_writes_durable_request_and_pokes_inbox(
    tmp_path: Path,
) -> None:
    """The running branch: a durable ``InjectionRequested`` is written and the
    process-local inbox carries the descriptor, WITHOUT taking a lease or
    driving. ``inject_goal`` dispatches on the FOLDED task status, so we fold the
    task to ``running`` by recording a ``TaskWoken`` (as an in-flight turn would
    leave it) before injecting."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("t1")])
    driver = InteractionDriver(host)

    started = driver.start(goal="hello", agent="main")
    task_id = started.task_id
    # Represent a turn in flight: a TaskWoken after the last suspend folds to
    # ``running``. Written control-plane (system_emit) exactly like the driver's
    # own lease-free writes, so no lease is required to stage the fixture.
    from noeta.protocols.events import TaskWokenPayload
    from noeta.protocols.wake import HumanResponseReceived

    event_log.system_emit(
        task_id=task_id,
        type="TaskWoken",
        payload=TaskWokenPayload(
            wake_event=HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
        ),
        actor="test",
        origin="system",
    )
    assert fold(event_log, host.content_store, task_id).status == "running"

    out = driver.inject_goal(task_id, goal="mid-flight message")

    # A durable request marker was written; the inbox carries it; no drive.
    types = [e.type for e in event_log.read(task_id)]
    assert "InjectionRequested" in types
    req = [e for e in event_log.read(task_id) if e.type == "InjectionRequested"][-1]
    assert req.payload.count == 1
    pending = host.pending_injections(task_id)
    assert len(pending) == 1
    assert req.payload.injection_id in pending
    # The verb reported the still-running status without manufacturing a terminal.
    assert out.status == "running"
    # The pending injection folds into governance for a resume's drain.
    folded = fold(event_log, host.content_store, task_id)
    assert req.payload.injection_id in folded.governance.pending_injections


def test_inject_goal_on_terminal_raises_not_resumable(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, _ = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(host)

    started = driver.start(goal="hello", agent="main")
    driver.cancel(started.task_id)  # → terminal

    with pytest.raises(NotResumableError) as exc:
        driver.inject_goal(started.task_id, goal="too late")
    assert exc.value.code == "not_resumable"
    assert exc.value.status == "terminal"


def test_inject_goal_leaves_no_write_on_refusal(tmp_path: Path) -> None:
    """A refused inject (terminal task) writes nothing durable."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(host)
    started = driver.start(goal="hello", agent="main")
    driver.cancel(started.task_id)
    before = len(event_log.read(started.task_id))
    with pytest.raises(NotResumableError):
        driver.inject_goal(started.task_id, goal="x")
    assert len(event_log.read(started.task_id)) == before


def test_cancel_discards_pending_injections(tmp_path: Path) -> None:
    """A pending injection is dropped from the inbox when the conversation is
    cancelled — the teardown mirror of ``discard_cancellation`` (the durable
    ``InjectionRequested`` stays on the log; only the accelerator is freed)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("t1")])
    driver = InteractionDriver(host)
    started = driver.start(goal="hello", agent="main")
    task_id = started.task_id

    # Fold to running (a turn in flight) so inject takes the running branch.
    from noeta.protocols.events import TaskWokenPayload
    from noeta.protocols.wake import HumanResponseReceived

    event_log.system_emit(
        task_id=task_id,
        type="TaskWoken",
        payload=TaskWokenPayload(
            wake_event=HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
        ),
        actor="test",
        origin="system",
    )
    driver.inject_goal(task_id, goal="mid-flight")
    assert host.pending_injections(task_id)  # queued

    driver.cancel(task_id)
    assert host.pending_injections(task_id) == {}  # inbox freed
