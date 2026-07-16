"""Regression: a resident WorkerLoop must not steal a task out of the
wake->lease window of a seed-after-wake resume and drive it without the
command's input.

``InteractionDriver._seed_wake_common`` (the send_goal / answer / deliver_event
/ approve / deny resume path) wakes the suspended task — flipping it to ``ready``
— then targeted-leases it, and only THEN appends the command's message. Between
the wake and the claim the task is ready but the new message is not yet durable:
an untargeted ``lease(task_id=None)`` poll landing there would lease the task and
re-drive the turn WITHOUT the command's input (dropping the user's message), and
the resume's own targeted lease would then find nothing and raise
NotResumableError — which the product misreads as "task not resumable" and
restarts the session fresh, stranding its history.

Same hazard as seed_start's enqueue->lease window (test_resident_worker_seed_
race.py); the resume path reaches it through ``wake`` instead of ``enqueue``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from noeta.core.fold import fold
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE
from noeta.runtime.worker import WorkerLoop
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    default_coding_budget,
    make_driver,
    make_host,
    make_registry,
    runner_main_spec,
)

FIRST_GOAL = "first-goal: turn one"
SECOND_GOAL = "SECOND-GOAL-MARKER: the user's new input"


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _host(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    requests: list[str] = []

    def responder(req):
        requests.append(
            "\n".join(
                b.text
                for m in req.messages
                for b in m.content
                if isinstance(b, TextBlock)
            )
        )
        return _end("ok")

    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responder=responder),
        model="gpt-test",
        multi_turn=True,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        budget=default_coding_budget(),
    )
    return host, make_driver(host), requests


def _users(host, task_id: str) -> list[str]:
    task = fold(host.event_log, host.content_store, task_id)
    return [
        b.text
        for m in task.runtime.messages
        if m.role == "user"
        for b in m.content
        if isinstance(b, TextBlock)
    ]


def test_send_goal_reserves_the_woken_task_against_an_untargeted_steal(
    tmp_path: Path,
) -> None:
    host, driver, _requests = _host(tmp_path)

    # Turn one: seed and drive to the next-goal suspend, no worker running.
    seeded = driver.seed_start(goal=FIRST_GOAL, agent="main")
    driver.drive_seeded(seeded)
    tid = seeded.task_id
    assert "TaskSuspended" in [e.type for e in host.event_log.read(tid)]

    # Interpose an untargeted poll at the wake point — exactly what a resident
    # worker's tick() does, made deterministic by driving it from inside wake().
    dispatcher = host.dispatcher
    real_wake = dispatcher.wake
    stolen: list = []

    def wake_then_poll(task_id, wake_event, *, reserved=False):
        out = real_wake(task_id, wake_event, reserved=reserved)
        thief = dispatcher.lease(worker_id="thief", lease_seconds=30.0, task_id=None)
        if thief is not None:
            stolen.append(thief)
        return out

    dispatcher.wake = wake_then_poll  # type: ignore[method-assign]

    # Turn two: append the user's new goal.
    s2 = driver.seed_send_goal(tid, goal=SECOND_GOAL)

    assert stolen == [], (
        "an untargeted poll leased the woken task inside its resume window — a "
        "resident worker would re-drive it without the command's input"
    )
    # The resume owns the wake: it leased the task and the new goal is durable.
    driver.drive_seeded(s2)
    users = _users(host, tid)
    assert any(SECOND_GOAL in u for u in users), (
        f"the user's second goal was dropped: {users}"
    )


def _wait(pred, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_send_goal_under_a_real_resident_worker_keeps_the_users_message(
    tmp_path: Path,
) -> None:
    """End-to-end with the served product's actual WorkerLoop pool: a second
    turn's message must survive and be the one the turn is driven on. The wake
    window is widened with a small sleep so the real worker's poll deterministically
    races into it (the production window is microseconds); the reservation is what
    keeps the poll off the woken task."""
    host, driver, requests = _host(tmp_path)

    # The resident pool runs for the WHOLE session, exactly like production.
    loop = WorkerLoop(
        host,
        worker_id="resident-0",
        poll_interval=0.01,
        heartbeat_interval=0.0,
        stale_sweep_interval=0.0,
        timer_poll_interval=0.0,
        shutdown_grace_s=2.0,
        next_goal_handle=NEXT_GOAL_WAKE_HANDLE,
    )
    th = threading.Thread(target=loop.run_forever, name="resident-0", daemon=True)
    th.start()
    try:
        # Turn one: seed_start reserves, so the running pool cannot steal it;
        # it drives to the next-goal suspend.
        seeded = driver.seed_start(goal=FIRST_GOAL, agent="main")
        host.dispatcher.release_yield(seeded.lease.lease_id)
        tid = seeded.task_id
        assert _wait(
            lambda: "TaskSuspended" in [e.type for e in host.event_log.read(tid)]
        ), "turn one never suspended"

        # Widen the wake->lease window so a resident-worker poll reliably lands
        # in it during turn two.
        real_wake = host.dispatcher.wake

        def slow_wake(task_id, wake_event, *, reserved=False):
            out = real_wake(task_id, wake_event, reserved=reserved)
            time.sleep(0.2)
            return out

        host.dispatcher.wake = slow_wake  # type: ignore[method-assign]

        # Turn two: append the user's new goal. Without the reservation a worker
        # steals the woken task during the widened window and seed_send_goal
        # raises NotResumableError; with it, the resume owns the wake.
        s2 = driver.seed_send_goal(tid, goal=SECOND_GOAL)
        host.dispatcher.release_yield(s2.lease.lease_id)

        assert _wait(lambda: any(SECOND_GOAL in u for u in _users(host, tid))), (
            f"the user's second goal never became durable: {_users(host, tid)}"
        )
        # The second turn was driven on the user's message, not an empty replay.
        assert _wait(lambda: any(SECOND_GOAL in r for r in requests)), (
            f"no LLM request carried the user's second goal: {requests}"
        )
    finally:
        loop.stop()
