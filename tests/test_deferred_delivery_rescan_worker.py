"""A deferred background result lands after a turn a resident worker drove.

A background sub-agent result whose push gave up (the parent stayed busy past
the delivery window) is re-scanned for its session when a turn settles. A turn
the ``Client`` drives itself already re-scans; these tests pin the two paths
that hand the turn to the resident pool instead — ``Client.dispatch_seeded``
and a bare :class:`WorkerLoop` over the host — which reach the host through
its optional ``note_root_turn_settled`` seam.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE
from noeta.runtime.worker import WorkerLoop
from noeta.sdk import Client
from noeta.sdk.testing import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests._sdk_session import (
    default_coding_budget,
    make_driver,
    make_host,
    make_registry,
    runner_main_spec,
)
from tests.test_shutdown_owns_background import (
    PARENT_GOAL,
    _bg_options,
    _bg_responder,
)


def _wait_for(pred: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _delivered(client: Client, task_id: str) -> int:
    return [e.type for e in client.events(task_id)].count(
        "BackgroundSubagentDelivered"
    )


def test_deferred_result_lands_after_a_dispatch_seeded_turn(tmp_path: Path) -> None:
    release = threading.Event()
    release.set()
    calls: list[float] = []
    client = Client(
        _bg_options(),
        provider=FakeLLMProvider(responder=_bg_responder(release, calls)),
        workspace_dir=tmp_path,
        model="sonnet",
    )
    host = client._host  # noqa: SLF001
    # Simulate a push that gave up: the finished child's hand-off is lost.
    dropped: list[str] = []
    real_on_exit = host._delivery.on_exit  # noqa: SLF001

    def lose_it(**kw: Any) -> None:
        dropped.append(kw.get("key") or "")

    host._delivery.on_exit = lose_it  # type: ignore[method-assign]  # noqa: SLF001
    try:
        out = client.start(goal=PARENT_GOAL)
        child_id = next(
            s.task_id for s in client.task_streams() if s.task_id != out.task_id
        )
        assert _wait_for(lambda: bool(dropped))
        assert client.task_status(child_id).status == "terminal"
        assert _delivered(client, out.task_id) == 0
        host._delivery.on_exit = real_on_exit  # type: ignore[method-assign]  # noqa: SLF001

        client.start_workers(1, poll_interval=0.02)
        seeded = client.seed_send_goal(out.task_id, goal="more")
        client.dispatch_seeded(seeded)
        assert _wait_for(lambda: _delivered(client, out.task_id) == 1), (
            "the deferred result never landed after the pool-driven turn"
        )
        time.sleep(0.2)
        assert _delivered(client, out.task_id) == 1
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# The worker seam itself: called only at a settled root turn
# ---------------------------------------------------------------------------


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _host(tmp_path: Path) -> Any:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    return make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responder=lambda req: _end("ok")),
        model="gpt-test",
        multi_turn=True,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        budget=default_coding_budget(),
    )


def _loop(host: Any, *, next_goal_handle: Any) -> WorkerLoop:
    return WorkerLoop(
        host,
        worker_id="resident-0",
        poll_interval=0.0,
        heartbeat_interval=0.0,
        stale_sweep_interval=0.0,
        timer_poll_interval=0.0,
        shutdown_grace_s=2.0,
        next_goal_handle=next_goal_handle,
    )


def _record_settled(host: Any) -> list[str]:
    settled: list[str] = []
    object.__setattr__(host, "note_root_turn_settled", settled.append)
    return settled


def test_worker_reports_a_settled_root_turn(tmp_path: Path) -> None:
    host = _host(tmp_path)
    settled = _record_settled(host)
    driver = make_driver(host)
    seeded = driver.seed_start(goal="hello", agent="main")
    host.dispatcher.release_yield(seeded.lease.lease_id)

    assert _loop(host, next_goal_handle=NEXT_GOAL_WAKE_HANDLE).tick()
    assert settled == [seeded.task_id]


def test_worker_without_a_next_goal_handle_reports_nothing(tmp_path: Path) -> None:
    host = _host(tmp_path)
    settled = _record_settled(host)
    driver = make_driver(host)
    seeded = driver.seed_start(goal="hello", agent="main")
    host.dispatcher.release_yield(seeded.lease.lease_id)

    assert _loop(host, next_goal_handle=None).tick()
    assert settled == []


def test_host_seam_redelivers_for_the_root(tmp_path: Path) -> None:
    """``SdkHost.note_root_turn_settled`` is the re-scan, and never raises."""
    host = _host(tmp_path)
    scanned: list[str] = []

    def scan(root_task_id: str) -> list[str]:
        scanned.append(root_task_id)
        raise RuntimeError("store hiccup")

    object.__setattr__(host, "redeliver_background_subagents", scan)
    host.note_root_turn_settled("root-1")
    assert scanned == ["root-1"]
