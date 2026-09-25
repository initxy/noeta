"""``Client.shutdown()`` owns the Client's background work and storage.

* Background shells the Client started are killed (and recorded Killed).
* In-flight background sub-agents stop and drive no parent turn afterwards;
  the stopped child stays started-and-undelivered, not cancelled, so the next
  Client on the same store resumes it.
* Storage adapters the Client opened from ``HostConfig.storage_path`` are
  closed, so building a Client per request does not leak file descriptors.

Plus the delivery re-scan: a finished background result whose push was
deferred past its window lands at the next settled turn of its session,
without a Client restart.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from noeta.execution.background_delivery import BackgroundDelivery
from noeta.sdk import (
    AgentDefinition,
    Client,
    HostConfig,
    LLMResponse,
    Options,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.sdk.storage import build_storage_stack
from noeta.sdk.testing import FakeLLMProvider


CHILD_GOAL = "CHILDGOAL research omega"
PARENT_GOAL = "PARENTGOAL kick it off"


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _tool(name: str, args: dict[str, Any], call_id: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _text_of(req: Any) -> str:
    parts: list[str] = []
    for message in req.messages:
        for block in message.content:
            text = getattr(block, "text", None)
            output = getattr(block, "output", None)
            if text:
                parts.append(text)
            elif isinstance(output, str):
                parts.append(output)
    return "\n".join(parts)


def _wait_for(pred: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _bg_options() -> Options:
    return Options(
        system_prompt="x",
        agents={"explore": AgentDefinition(description="d", prompt="p")},
        permission_mode="bypassPermissions",
    )


def _bg_responder(
    release: threading.Event, calls: list[float]
) -> Callable[[Any], LLMResponse]:
    def respond(req: Any) -> LLMResponse:
        calls.append(time.monotonic())
        text = _text_of(req)
        if CHILD_GOAL in text and PARENT_GOAL not in text:
            release.wait(20)
            return _end("CHILD RESULT")
        if "<background-subagent" in text:
            return _end("got notice")
        if "runs concurrently while you keep working" in text:
            return _end("launched")
        if "more" in text.split(PARENT_GOAL)[-1]:
            return _end("second turn")
        return _tool(
            "Task", {"agent": "explore", "goal": CHILD_GOAL, "background": True}, "spawn1"
        )

    return respond


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_shutdown_kills_the_clients_background_shells(tmp_path: Path) -> None:
    marker = f"sleep {os.getpid() % 1000 + 3000}"
    calls = {"n": 0}

    def respond(req: Any) -> LLMResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return _tool("Bash", {"command": marker, "run_in_background": True}, "b1")
        return _end("done")

    client = Client(
        Options(system_prompt="x", permission_mode="bypassPermissions"),
        provider=FakeLLMProvider(responder=respond),
        workspace_dir=tmp_path,
        model="sonnet",
    )
    out = client.start(goal="run it")
    assert "BackgroundShellStarted" in [e.type for e in client.events(out.task_id)]
    client.shutdown()

    def alive() -> bool:
        found = subprocess.run(
            ["pgrep", "-f", marker], capture_output=True, text=True
        ).stdout.strip()
        return bool(found)

    assert _wait_for(lambda: not alive(), timeout=5.0), "background shell outlived shutdown"
    assert "BackgroundShellKilled" in [e.type for e in client.events(out.task_id)]


def test_shutdown_stops_background_subagents_without_cancelling(tmp_path: Path) -> None:
    release = threading.Event()
    calls: list[float] = []
    event_log, content_store, dispatcher = build_storage_stack("memory")
    config = HostConfig(event_log=event_log, content_store=content_store, dispatcher=dispatcher)
    client = Client(
        _bg_options(),
        provider=FakeLLMProvider(responder=_bg_responder(release, calls)),
        workspace_dir=tmp_path,
        model="sonnet",
        host_config=config,
    )
    try:
        out = client.start(goal=PARENT_GOAL)
        child_id = next(s.task_id for s in client.task_streams() if s.task_id != out.task_id)
        assert _wait_for(lambda: len(calls) >= 3)  # spawn, "launched", child call
        client.shutdown()
        stopped_at = time.monotonic()
        parent_events = len(client.events(out.task_id))
        release.set()
        time.sleep(1.0)
        assert [t for t in calls if t > stopped_at] == [], "an LLM call ran after shutdown"
        assert len(client.events(out.task_id)) == parent_events
        child_types = [e.type for e in client.events(child_id)]
        assert "TaskCancelled" not in child_types
    finally:
        release.set()
        client.shutdown()

    # The next Client on the same store picks the undelivered child up.
    calls.clear()
    with Client(
        _bg_options(),
        provider=FakeLLMProvider(responder=_bg_responder(release, calls)),
        workspace_dir=tmp_path,
        model="sonnet",
        host_config=config,
    ) as again:
        assert _wait_for(
            lambda: "BackgroundSubagentDelivered"
            in [e.type for e in again.events(out.task_id)]
        )


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc fd table")
def test_shutdown_closes_storage_opened_from_storage_path(tmp_path: Path) -> None:
    db = str(tmp_path / "store.sqlite")

    def fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    def one_client() -> None:
        with Client(
            Options(system_prompt="x"),
            provider=FakeLLMProvider(responder=lambda req: _end("ok")),
            workspace_dir=tmp_path,
            model="sonnet",
            host_config=HostConfig(storage_path=db),
        ) as client:
            client.start(goal="hi")

    one_client()  # warm imports / lazily opened process-wide handles
    before = fds()
    for _ in range(5):
        one_client()
    assert fds() - before <= 1


def test_injected_storage_is_not_closed(tmp_path: Path) -> None:
    event_log, content_store, dispatcher = build_storage_stack(
        "sqlite", path=str(tmp_path / "store.sqlite")
    )
    config = HostConfig(event_log=event_log, content_store=content_store, dispatcher=dispatcher)
    with Client(
        Options(system_prompt="x"),
        provider=FakeLLMProvider(responder=lambda req: _end("ok")),
        workspace_dir=tmp_path,
        model="sonnet",
        host_config=config,
    ) as client:
        out = client.start(goal="hi")
    assert event_log.read(out.task_id)  # still open: the caller owns it
    assert content_store.get(event_log.find_latest_snapshot(out.task_id).payload.state_ref)
    event_log.close()
    getattr(content_store, "_inner", content_store).close()
    dispatcher.close()


def test_deferred_background_result_lands_at_the_next_turn(tmp_path: Path) -> None:
    """A result whose push gave up (the parent stayed busy past the window)
    is re-pushed when the session's next turn settles, not only at the next
    Client construction."""
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
    # Simulate a push that was dropped: the finished child's hand-off is lost.
    dropped: list[str] = []
    real_on_exit = host._delivery.on_exit  # noqa: SLF001

    def lose_it(**kw: Any) -> None:
        dropped.append(kw.get("key") or "")

    host._delivery.on_exit = lose_it  # type: ignore[method-assign]  # noqa: SLF001
    try:
        out = client.start(goal=PARENT_GOAL)
        child_id = next(s.task_id for s in client.task_streams() if s.task_id != out.task_id)
        assert _wait_for(lambda: bool(dropped))
        assert client.task_status(child_id).status == "terminal"
        assert "BackgroundSubagentDelivered" not in [
            e.type for e in client.events(out.task_id)
        ]
        host._delivery.on_exit = real_on_exit  # type: ignore[method-assign]  # noqa: SLF001

        client.send_goal(out.task_id, goal="more")
        assert _wait_for(
            lambda: "BackgroundSubagentDelivered"
            in [e.type for e in client.events(out.task_id)]
        )
        types = [e.type for e in client.events(out.task_id)]
        assert types.count("BackgroundSubagentDelivered") == 1
    finally:
        client.shutdown()


def test_delivery_hand_off_is_deduplicated_per_key() -> None:
    event_log, content_store, _ = build_storage_stack("memory")
    delivery = BackgroundDelivery(event_log=event_log, content_store=content_store)
    gate = threading.Event()
    planned: list[int] = []

    def plan() -> None:
        planned.append(1)
        gate.wait(5)
        return None

    delivery.set_notifier(object())
    delivery.on_exit(task_id="t", plan=plan, thread_name="a", key="k")
    assert _wait_for(lambda: delivery.is_pending("k"))
    delivery.on_exit(task_id="t", plan=plan, thread_name="b", key="k")
    gate.set()
    assert _wait_for(lambda: not delivery.is_pending("k"))
    assert planned == [1]

    delivery.close()
    delivery.on_exit(task_id="t", plan=plan, thread_name="c", key="k")
    time.sleep(0.1)
    assert planned == [1]
