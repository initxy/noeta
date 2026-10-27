"""A cancel is final, even when another process is driving the turn.

Two Clients over one sqlite file stand in for two replicas over a shared
store: B cancels while A's turn is waiting on the model. The tool call the
model then asks for must not run, A's drive must settle quietly, and nothing
A writes afterwards may bring the task back from terminal.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from noeta.core.fold import fold
from noeta.execution.driver import NotResumableError, TaskAlreadyTerminalError
from noeta.protocols.events import TaskSuspendedPayload
from noeta.protocols.messages import ToolUseBlock
from noeta.protocols.wake import HumanResponseReceived
from noeta.sdk import Client, HostConfig, LLMResponse, Options, TextBlock, Usage
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider


def _text(n: int) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=f"done{n}")],
        usage=Usage(uncached=1, output=1),
        raw={"id": str(n)},
    )


def _bash(n: int, command: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=f"b{n}", tool_name="Bash", arguments={"command": command}
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": str(n)},
    )


def test_cancel_from_another_client_stops_the_running_turn(tmp_path: Path) -> None:
    db = str(tmp_path / "store.sqlite")
    ws = tmp_path / "ws"
    ws.mkdir()
    side = ws / "side.txt"
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def respond(_req: Any) -> LLMResponse:
        calls.append(1)
        n = len(calls)
        if n == 1:
            return _text(n)
        if n == 2:
            entered.set()
            release.wait(20)
            return _bash(n, f"echo SIDE_EFFECT >> {side}")
        return _text(n)

    def make(queue: str) -> Client:
        return Client(
            Options(system_prompt="x", permission_mode="bypassPermissions"),
            provider=FakeLLMProvider(responder=respond),
            workspace_dir=ws,
            model="sonnet",
            host_config=HostConfig(storage_path=db, queue=queue),
        )

    a, b = make("a"), make("b")
    try:
        tid = a.start(goal="hi").task_id
        errors: list[BaseException] = []

        def drive() -> None:
            try:
                a.send_goal(tid, goal="do it")
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        t = threading.Thread(target=drive)
        t.start()
        assert entered.wait(10)
        b.cancel(tid)
        release.set()
        t.join(20)
        assert not t.is_alive()

        assert errors == []
        assert not side.exists(), "a tool ran after the task was cancelled"
        assert b.task_status(tid).status == "terminal"
        assert a.task_status(tid).status == "terminal"
        types = [e.type for e in b.events(tid)]
        after = types[types.index("TaskCancelled") + 1 :]
        assert "ToolCallStarted" not in after
        assert "TaskSuspended" not in after
        with pytest.raises((NotResumableError, TaskAlreadyTerminalError)):
            b.send_goal(tid, goal="again")
    finally:
        release.set()
        a.shutdown()
        b.shutdown()


def test_fold_keeps_terminal_after_a_late_lifecycle_event() -> None:
    log, store = InMemoryEventLog(), InMemoryContentStore()
    client = Client(
        Options(system_prompt="x"),
        provider=FakeLLMProvider(responses=[_text(1)]),
        model="sonnet",
        host_config=HostConfig(
            event_log=log, content_store=store, dispatcher=_dispatcher(log)
        ),
    )
    try:
        tid = client.start(goal="hi").task_id
        client.cancel(tid)
        # A writer that missed the cancel appends its turn end afterwards.
        log.system_emit(
            task_id=tid,
            type="TaskSuspended",
            payload=TaskSuspendedPayload(
                reason="waiting_human",
                wake_on=HumanResponseReceived(handle="next-goal"),
            ),
            actor="stale-writer",
            origin="system",
        )
        log.system_emit(
            task_id=tid,
            type="TaskWoken",
            payload=_woken_payload(),
            actor="stale-writer",
            origin="system",
        )
        assert fold(log, store, tid).status == "terminal"
    finally:
        client.shutdown()


def _dispatcher(log: InMemoryEventLog) -> Any:
    from noeta.storage.memory import InMemoryDispatcher

    disp = InMemoryDispatcher()
    log.bind_lease_registry(disp)
    return disp


def _woken_payload() -> Any:
    from noeta.protocols.events import TaskWokenPayload

    return TaskWokenPayload(wake_event=HumanResponseReceived(handle="next-goal"))


def test_cancel_moves_the_dispatcher_row_to_terminal(tmp_path: Path) -> None:
    client = Client(
        Options(system_prompt="x"),
        provider=FakeLLMProvider(responses=[_text(1)]),
        model="sonnet",
        host_config=HostConfig(storage_path=str(tmp_path / "s.sqlite")),
    )
    try:
        tid = client.start(goal="hi").task_id
        client.cancel(tid)
        disp = client._host.dispatcher  # noqa: SLF001 - the row is the point
        assert disp.task_status(tid) == "terminal"
        assert disp.lease(worker_id="w", task_id=tid) is None
    finally:
        client.shutdown()
