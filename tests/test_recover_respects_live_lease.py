"""Background sub-agent recovery leaves a child another Client is driving alone.

``Client.__init__`` runs the background sub-agent recovery scan. On a store
shared by two live Clients, a child the first Client is still running looks
exactly like a crash orphan in the log (started, undelivered, non-terminal);
only its live lease tells them apart. Recovery must skip it — no second drive,
no second delivery — and a drive that loses the child's targeted lease to
another driver must not report a false "did not complete".

The scan also finds its candidates from each stream's fold snapshot plus the
events after it, so building a Client does not read every stream in full.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

from noeta.execution.background_subagent import BackgroundSubagentRegistry
from noeta.execution.subtask_drain import _ChildNotReady
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


def _spawn_background() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="spawn1",
                tool_name="Task",
                arguments={"agent": "explore", "goal": CHILD_GOAL, "background": True},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "spawn1"},
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


def _responder(release: threading.Event, child_calls: list[int]) -> Callable[[Any], LLMResponse]:
    def respond(req: Any) -> LLMResponse:
        text = _text_of(req)
        if CHILD_GOAL in text and PARENT_GOAL not in text:
            child_calls.append(1)
            release.wait(20)
            return _end("REAL CHILD RESULT")
        if "<background-subagent" in text:
            return _end("got notice")
        if "runs concurrently while you keep working" in text:
            return _end("launched")
        return _spawn_background()

    return respond


def _options() -> Options:
    return Options(
        system_prompt="x",
        agents={"explore": AgentDefinition(description="d", prompt="p")},
        permission_mode="bypassPermissions",
    )


def _wait_for(pred: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_second_client_does_not_redrive_a_live_background_child(tmp_path: Path) -> None:
    release = threading.Event()
    child_calls: list[int] = []
    event_log, content_store, dispatcher = build_storage_stack("memory")

    def make_client(queue: str) -> Client:
        return Client(
            _options(),
            provider=FakeLLMProvider(responder=_responder(release, child_calls)),
            workspace_dir=tmp_path,
            model="sonnet",
            host_config=HostConfig(
                event_log=event_log,
                content_store=content_store,
                dispatcher=dispatcher,
                queue=queue,
            ),
        )

    first = make_client("a")
    second = None
    try:
        out = first.start(goal=PARENT_GOAL)
        # The child is mid-step on the first Client (its LLM call is blocked).
        assert _wait_for(lambda: len(child_calls) == 1)

        second = make_client("b")  # construction runs the recovery scan
        assert second._host._background_subagents._inflight == {}  # noqa: SLF001

        release.set()

        def delivered() -> int:
            return [e.type for e in first.events(out.task_id)].count(
                "BackgroundSubagentDelivered"
            )

        assert _wait_for(lambda: delivered() == 1)
        time.sleep(0.3)  # a stray second drive / push would land by now
        assert len(child_calls) == 1, "the child was driven twice"
        assert delivered() == 1
        notices = [
            str(getattr(m, "text", ""))
            for m in first.messages(out.task_id)
            if "<background-subagent" in str(getattr(m, "text", ""))
        ]
        assert len(notices) == 1
        assert "REAL CHILD RESULT" in notices[0]
        assert "did not complete" not in notices[0]
    finally:
        release.set()
        first.shutdown()
        if second is not None:
            second.shutdown()


def _bare_registry(delivered: list[tuple[str, str]]) -> BackgroundSubagentRegistry:
    event_log, content_store, dispatcher = build_storage_stack("memory")
    return BackgroundSubagentRegistry(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        build_host=lambda parent_id: None,  # type: ignore[arg-type,return-value]
        deliver=lambda parent, child: delivered.append((parent, child)),
    )


def test_losing_the_child_lease_delivers_nothing() -> None:
    """A drive whose targeted lease was taken by another driver never ran the
    child; that driver delivers it. Reporting "did not complete" here was a
    false failure notice to the parent."""
    delivered: list[tuple[str, str]] = []
    registry = _bare_registry(delivered)
    registry._inflight["parent"] = {"child"}  # noqa: SLF001
    future: "Future[None]" = Future()
    future.set_exception(_ChildNotReady("child"))
    registry._on_done(future, "parent", "child")  # noqa: SLF001
    assert delivered == []
    assert registry._inflight == {}  # noqa: SLF001 — the cap slot is freed


def test_closed_registry_neither_delivers_nor_cancels() -> None:
    delivered: list[tuple[str, str]] = []
    registry = _bare_registry(delivered)
    registry.close()
    future: "Future[None]" = Future()
    future.set_result(None)
    registry._on_done(future, "parent", "child")  # noqa: SLF001
    assert delivered == []
    assert registry.recover() == []


def test_recover_reads_snapshots_not_whole_streams(tmp_path: Path) -> None:
    """Candidate discovery reads each stream's fold snapshot and the events
    after it; no stream with a snapshot is read from the start."""
    db = str(tmp_path / "store.sqlite")
    options = Options(system_prompt="x")
    with Client(
        options,
        provider=FakeLLMProvider(responder=lambda req: _end("ok")),
        workspace_dir=tmp_path,
        model="sonnet",
        host_config=HostConfig(storage_path=db),
    ) as client:
        for i in range(5):
            out = client.start(goal=f"g{i}")
            client.send_goal(out.task_id, goal="more")

    with Client(
        options,
        provider=FakeLLMProvider(responder=lambda req: _end("ok")),
        workspace_dir=tmp_path,
        model="sonnet",
        host_config=HostConfig(storage_path=db),
    ) as client:
        registry = client._host._background_subagents  # noqa: SLF001
        log = client._host.event_log  # noqa: SLF001
        full_reads: list[str] = []
        real_read = log.read

        def counting_read(task_id: str, *, after_seq: Any = None) -> Any:
            if after_seq is None:
                full_reads.append(task_id)
            return real_read(task_id, after_seq=after_seq)

        log.read = counting_read  # type: ignore[method-assign]
        try:
            assert registry.recover() == []
        finally:
            del log.read
        assert full_reads == []
