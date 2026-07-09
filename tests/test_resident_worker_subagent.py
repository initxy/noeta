"""Regression: a resident WorkerLoop (the served product's ``background_drive``
/ ``start_workers`` path) must drive foreground AND background subagents to
terminal WITHOUT stranding them unseeded.

Root cause guarded here: only ``subtask_drain._descend_to_child`` seeds a
child's ``state.goal`` into the opening user message. The resident-worker step
primitive ``run_leased_task`` does not, and the dispatcher's untargeted FIFO
lease will hand an enqueued child to a resident worker. Part A makes the worker
settle the subtree through the drain (foreground); Part B keeps untargeted
leases off enqueued children (background race).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from noeta.core.fold import fold
from noeta.policies.react import SPAWN_SUBAGENT_TOOL
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE
from noeta.runtime.worker import WorkerLoop
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    default_coding_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)

PARENT_GOAL = "parent-goal: delegate and report"
CHILD_GOAL = "child-goal-zeta: do the isolated work and answer"
CHILD_RESULT = "zeta-answer: 42"
STARTED_MARKER = "runs concurrently while you keep working"
NOTICE_TAG = "<background-subagent "


def _spawn(background: bool) -> LLMResponse:
    args = {"agent": "explore", "goal": CHILD_GOAL}
    if background:
        args["background"] = True
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="spawn-1", tool_name=SPAWN_SUBAGENT_TOOL, arguments=args
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "spawn-1"},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _text(req: LLMRequest) -> str:
    parts: list[str] = []
    for m in req.messages:
        for b in m.content:
            if isinstance(b, TextBlock):
                parts.append(b.text)
            elif isinstance(b, ToolResultBlock) and isinstance(b.output, str):
                parts.append(b.output)
    return "\n".join(parts)


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    return ws


def _host(ws: Path, provider: FakeLLMProvider):
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    children = [preset_spec("explore")]
    host = make_host(
        make_registry(main, *children),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=True,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        budget=default_coding_budget(),
    )
    driver = make_driver(host)
    host.set_background_notifier(driver)
    return host, driver


def _start_worker(host) -> WorkerLoop:
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
    return loop


def _wait(pred, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _child_id(host, parent_id: str):
    for env in host.event_log.read(parent_id):
        if env.type in ("SubtaskSpawned", "BackgroundSubagentStarted"):
            return env.payload.subtask_id
    return None


def _types(host, task_id: str) -> list[str]:
    return [e.type for e in host.event_log.read(task_id)]


@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
def test_resident_worker_drives_subagent(tmp_path: Path, background: bool) -> None:
    def _responder(req: LLMRequest) -> LLMResponse:
        text = _text(req)
        if CHILD_GOAL in text and PARENT_GOAL not in text:
            return _end(CHILD_RESULT)          # the isolated (seeded) child
        if NOTICE_TAG in text:                 # background delivery notice turn
            return _end("got the background result; done")
        if STARTED_MARKER in text:             # background started receipt
            return _end("launched; carrying on")
        return _spawn(background)              # parent's opening turn

    provider = FakeLLMProvider(responder=_responder)
    host, driver = _host(_make_ws(tmp_path), provider)
    loop = _start_worker(host)
    try:
        seeded = driver.seed_start(goal=PARENT_GOAL, agent="main")
        parent_id = seeded.task_id
        host.dispatcher.release_yield(seeded.lease.lease_id)

        assert _wait(lambda: _child_id(host, parent_id) is not None), (
            "no subtask child was ever created"
        )
        child_id = _child_id(host, parent_id)

        assert _wait(
            lambda: any(
                t in _types(host, child_id)
                for t in ("TaskCompleted", "TaskFailed", "TaskCancelled")
            )
        ), f"child {child_id} never reached terminal: {_types(host, child_id)}"

        types = _types(host, child_id)
        assert "TaskFailed" not in types, (
            f"child {child_id} FAILED (unseeded?): {types}"
        )
        assert "TaskCompleted" in types, f"child did not complete: {types}"

        child = fold(host.event_log, host.content_store, child_id)
        # The child's opening user message must be its seeded goal.
        first_user = next(
            (m for m in child.runtime.messages if m.role == "user"), None
        )
        assert first_user is not None, "child had NO user message (unseeded)"
        assert any(
            isinstance(b, TextBlock) and CHILD_GOAL in b.text
            for b in first_user.content
        ), f"child's first user message was not its goal: {first_user!r}"
    finally:
        loop.stop()
