"""An interrupt landing in the ``release_yield`` hand-off window must stick.

``Client.send_goal`` on the worker-pool path seeds the turn (wake + targeted
lease + durable preludes), then ``dispatch_seeded`` yields the lease back to
the ready queue for a resident worker to claim. In that window the turn is
durably open (``TaskWoken`` folded ⇒ ``status == "running"``) but no lease is
active — and ``driver.interrupt``'s old gate probed ``has_active_lease`` only,
so the cancel-registry mark was never armed: the worker picked the task up and
drove the WHOLE turn. Esc as a silent no-op.

The fixed gate also treats the folded ``running`` status as "turn in flight",
so the mark arms and the claiming worker's first top-of-loop poll aborts the
turn before the model round.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from noeta.core.fold import fold
from noeta.protocols.messages import LLMRequest, LLMResponse, TextBlock, Usage
from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.worker import STOP_INTERRUPTED_SUSPEND_REASON, WorkerLoop
from noeta.runtime.workspace import FsWriteMode
from noeta.testing.fake_llm import FakeLLMProvider

from tests._sdk_session import (
    default_coding_budget,
    make_driver,
    make_host,
    make_registry,
    runner_main_spec,
)


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _wait(pred, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_interrupt_in_the_yield_window_aborts_the_picked_up_turn(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    calls: list[str] = []

    def responder(request: LLMRequest) -> LLMResponse:
        calls.append("call")
        return _end(f"turn{len(calls)}")

    provider = FakeLLMProvider(responder=responder)
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=True,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        budget=default_coding_budget(),
    )
    driver = make_driver(host)

    # Turn 1 runs synchronously; the conversation parks at next-goal.
    started = driver.start(goal="first", agent="main")
    task_id = started.task_id
    assert len(calls) == 1

    # Open the hand-off window: seed turn 2, yield the lease, NO worker yet.
    seeded = driver.seed_send_goal(task_id=task_id, goal="second")
    host.dispatcher.release_yield(seeded.lease.lease_id)
    assert fold(host.event_log, host.content_store, task_id).status == "running"
    assert not host.dispatcher.has_active_lease(task_id)

    # Esc lands inside the window. The mark MUST arm — this was the silent
    # no-op: the old lease-only gate saw an idle task here.
    driver.interrupt(task_id, reason="user pressed stop")
    assert host.is_cancelled(task_id)

    # A resident worker now claims the ready task; its first top-of-loop poll
    # must abort the turn before any model round.
    loop = WorkerLoop(
        host,
        worker_id="resident-0",
        poll_interval=0.02,
        heartbeat_interval=0.0,
        stale_sweep_interval=0.0,
        timer_poll_interval=0.0,
        shutdown_grace_s=2.0,
        next_goal_handle=NEXT_GOAL_WAKE_HANDLE,
    )
    th = threading.Thread(target=loop.run_forever, name="resident-0", daemon=True)
    th.start()
    try:
        assert _wait(
            lambda: any(
                e.type == "TaskSuspended"
                and e.payload.reason == STOP_INTERRUPTED_SUSPEND_REASON
                for e in host.event_log.read(task_id)
            )
        ), "the picked-up turn was never aborted"
    finally:
        loop.stop()

    # The stopped turn ran no model round, and the mark was consumed by the
    # settle — the conversation stays usable.
    assert len(calls) == 1
    assert not host.is_cancelled(task_id)
    task = fold(host.event_log, host.content_store, task_id)
    assert task.status == "suspended"


def test_idle_interrupt_still_arms_nothing(tmp_path: Path) -> None:
    """The original protection survives the widened gate: an idle conversation
    (suspended at next-goal, no seed in flight) must not get a mark that would
    swallow the user's NEXT message."""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    provider = FakeLLMProvider(responder=lambda req: _end("turn"))
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=True,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        budget=default_coding_budget(),
    )
    driver = make_driver(host)
    started = driver.start(goal="first", agent="main")

    driver.interrupt(started.task_id, reason="nothing running")

    assert not host.is_cancelled(started.task_id)
