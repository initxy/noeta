"""A leased step that outlives its lease must keep it alive, and if the lease
is still lost the task must converge to a durable terminal.

When a turn is driven synchronously with no resident ``WorkerLoop``, nothing
heartbeats a long ``run_one_step``. A step longer than the lease TTL (a slow
LLM round-trip retried to its budget) loses its lease mid-flight; the step's
own terminal write is then rejected by ``is_lease_valid`` (``InvalidLease``),
and the task can hang forever in ``running`` — neither resumable (not at a
next-goal suspend) nor deletable (the dispatcher still reads an active lease).

Two guarantees close that hole:

* :func:`noeta.runtime.worker.keep_lease_alive` renews the lease for the
  duration of the step (the heartbeat the WorkerLoop path has resident).
* If the lease is still lost, ``InteractionDriver`` converges the task to a
  lease-free control-plane ``TaskFailed`` so fold always reaches terminal.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from tests._sdk_session import official_registry as official_agent_registry
from noeta.client import SdkHost
from noeta.core.fold import fold
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.protocols.errors import InvalidLease
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.runtime.worker import keep_lease_alive
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode


# ---------------------------------------------------------------------------
# keep_lease_alive renews the lease while a synchronous step runs
# ---------------------------------------------------------------------------


class _RecordingDispatcher:
    """Dispatcher double that records every heartbeat renewal."""

    def __init__(self) -> None:
        self.beats: list[tuple[str, float]] = []

    def heartbeat(self, lease_id: str, *, lease_seconds: float) -> float:
        self.beats.append((lease_id, lease_seconds))
        return lease_seconds


class _Lease:
    lease_id = "lease-x"
    task_id = "task-x"


def test_keep_lease_alive_renews_lease_while_step_runs() -> None:
    disp = _RecordingDispatcher()
    with keep_lease_alive(disp, _Lease(), interval=0.01, lease_seconds=123.0):
        time.sleep(0.1)  # a step that outlasts several heartbeat intervals
    assert disp.beats, "heartbeat should renew the lease at least once"
    # Every renewal carries the lease id + the renewal window we asked for.
    assert all(beat == ("lease-x", 123.0) for beat in disp.beats)


def test_keep_lease_alive_stops_after_block_exits() -> None:
    disp = _RecordingDispatcher()
    with keep_lease_alive(disp, _Lease(), interval=0.01, lease_seconds=60.0):
        time.sleep(0.03)
    # The context manager joins the heartbeat thread on exit, so no renewal can
    # land after the step returned (no leaked thread thrashing the lease).
    seen = len(disp.beats)
    time.sleep(0.05)
    assert len(disp.beats) == seen


def test_keep_lease_alive_noop_when_interval_nonpositive() -> None:
    disp = _RecordingDispatcher()
    with keep_lease_alive(disp, _Lease(), interval=0.0, lease_seconds=60.0):
        time.sleep(0.02)
    assert disp.beats == []  # disabled — no heartbeat thread starts


# ---------------------------------------------------------------------------
# A lost-lease step converges to a durable terminal (never stuck)
# ---------------------------------------------------------------------------


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _host(
    workspace: Path, *, responses: list[LLMResponse]
) -> tuple[SdkHost, InMemoryDispatcher, InMemoryEventLog]:
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


def test_force_terminal_on_lost_lease_writes_lease_free_terminal(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)

    started = driver.start(goal="hello", agent="main")
    # A normally-finishing interactive turn rests at a next-goal suspend.
    assert started.status == "suspended"
    assert started.wake_handle == NEXT_GOAL_WAKE_HANDLE
    task_id = started.task_id
    assert fold(host.event_log, host.content_store, task_id).status != "terminal"

    driver._force_terminal_on_lost_lease(task_id, InvalidLease("lease gone"))

    task = fold(host.event_log, host.content_store, task_id)
    assert task.status == "terminal"
    failed = [e for e in event_log.read(task_id) if e.type == "TaskFailed"]
    assert len(failed) == 1
    # The terminal is honest about why (a lost execution lease, not a policy
    # decision) and not retryable (nothing re-drives this transport).
    assert "lease lost" in failed[0].payload.reason
    assert failed[0].payload.retryable is False


def test_force_terminal_on_lost_lease_is_idempotent(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(host)
    task_id = driver.start(goal="x", agent="main").task_id

    driver._force_terminal_on_lost_lease(task_id, InvalidLease("a"))
    after_first = len(event_log.read(task_id))
    # A racing reclaim/cancel that already terminated the task → clean no-op.
    driver._force_terminal_on_lost_lease(task_id, InvalidLease("b"))
    assert len(event_log.read(task_id)) == after_first
    assert sum(e.type == "TaskFailed" for e in event_log.read(task_id)) == 1


def test_lost_lease_mid_turn_converges_to_terminal_not_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the in-flight turn loses its lease, the task ends terminal
    (settled / deletable) instead of hanging in ``running``.

    Drives the real ``drive_seeded`` path with ``run_leased_task`` forced to
    raise ``InvalidLease`` (a reclaim mid-step) and asserts the task converges
    to terminal — the failure mode that otherwise strands the task with no
    working UI affordance.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)

    seeded = driver.seed_start(goal="hello", agent="main")
    task_id = seeded.task_id
    assert fold(host.event_log, host.content_store, task_id).status != "terminal"

    def _raise_lost_lease(*_args: Any, **_kwargs: Any) -> None:
        raise InvalidLease("reclaimed mid-step")

    monkeypatch.setattr(
        "noeta.execution.driver.run_leased_task", _raise_lost_lease
    )

    with pytest.raises(InvalidLease):
        driver.drive_seeded(seeded)

    # Without the lease-free terminal write, the step's TaskFailed would be
    # rejected by is_lease_valid and the task would hang in ``running`` forever.
    task = fold(host.event_log, host.content_store, task_id)
    assert task.status == "terminal"
    assert any(e.type == "TaskFailed" for e in event_log.read(task_id))
