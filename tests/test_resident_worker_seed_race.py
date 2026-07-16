"""Regression: a resident WorkerLoop must not steal a ROOT task out of its own
seed window and drive it unseeded.

``InteractionDriver.seed_start`` enqueues the freshly created task, then claims
it with a targeted lease, and only THEN writes ``ModelBound`` + the opening user
message carrying the goal. Between the enqueue and that claim the task is ready
but unseeded — with the served product's resident worker pool running, an
untargeted FIFO ``lease(task_id=None)`` poll could land in that window, steal the
task, and drive it with an empty message history (which the provider rejects with
a "no user message" 400).

This is the same hazard ``BackgroundSubagentRegistry._submit`` guards with
``enqueue(reserved=True)`` (see tests/test_resident_worker_subagent.py); the root
task reaches it through the seed path instead.
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

GOAL = "root-goal-omega: answer the question"
ANSWER = "omega-answer: done"


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
    provider = FakeLLMProvider(responder=lambda req: _end(ANSWER))
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
    return host, make_driver(host)


def _steal_at_enqueue(host, stolen: list) -> None:
    """Interpose an untargeted poll at the exact enqueue point.

    A real resident worker only hits this window on a microsecond race; driving
    the poll from inside ``enqueue`` makes the same interleaving deterministic.
    """
    dispatcher = host.dispatcher
    real_enqueue = dispatcher.enqueue

    def enqueue_then_poll(task_id: str, *, reserved: bool = False) -> None:
        real_enqueue(task_id, reserved=reserved)
        thief = dispatcher.lease(
            worker_id="thief", lease_seconds=30.0, task_id=None
        )
        if thief is not None:
            stolen.append(thief)

    dispatcher.enqueue = enqueue_then_poll  # type: ignore[method-assign]


def _types(host, task_id: str) -> list[str]:
    return [e.type for e in host.event_log.read(task_id)]


def _wait(pred, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_seed_start_reserves_the_root_task_against_an_untargeted_steal(
    tmp_path: Path,
) -> None:
    host, driver = _host(tmp_path)
    stolen: list = []
    _steal_at_enqueue(host, stolen)

    seeded = driver.seed_start(goal=GOAL, agent="main")

    assert stolen == [], (
        "an untargeted poll leased the root task inside its seed window — a "
        "resident worker would drive it with an empty message history"
    )
    # The seed owns the task: ModelBound and the goal are durable before anyone
    # else can lease it.
    types = _types(host, seeded.task_id)
    assert "ModelBound" in types, f"seed wrote no ModelBound: {types}"
    task = fold(host.event_log, host.content_store, seeded.task_id)
    first_user = next((m for m in task.runtime.messages if m.role == "user"), None)
    assert first_user is not None, "root task had NO user message (unseeded)"
    assert any(
        isinstance(b, TextBlock) and GOAL in b.text for b in first_user.content
    ), f"root task's first user message was not its goal: {first_user!r}"


def test_seeded_root_task_is_still_picked_up_by_the_pool(tmp_path: Path) -> None:
    """The reservation is a ONE-SHOT claim guard, not a permanent exclusion:
    once seeded, ``_yield_seeded_lease``'s ``release_yield`` must hand the task
    back to the pool as an ordinary untargeted-leaseable task."""
    host, driver = _host(tmp_path)
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
        seeded = driver.seed_start(goal=GOAL, agent="main")
        host.dispatcher.release_yield(seeded.lease.lease_id)

        # A multi-turn session parks on TaskSuspended (waiting for the next
        # goal) rather than TaskCompleted, so the turn being driven at all is
        # the signal that the pool leased it.
        assert _wait(
            lambda: any(
                t in _types(host, seeded.task_id)
                for t in ("TaskSuspended", "TaskCompleted", "TaskFailed")
            )
        ), (
            f"the pool never drove the seeded root task (stranded by the "
            f"reservation?): {_types(host, seeded.task_id)}"
        )
        types = _types(host, seeded.task_id)
        assert "TaskFailed" not in types, f"root task FAILED: {types}"
        task = fold(host.event_log, host.content_store, seeded.task_id)
        assert any(
            m.role == "assistant"
            and any(isinstance(b, TextBlock) and ANSWER in b.text for b in m.content)
            for m in task.runtime.messages
        ), "the pool leased the task but never produced the turn's answer"
    finally:
        loop.stop()
