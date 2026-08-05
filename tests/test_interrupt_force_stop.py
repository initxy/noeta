"""``interrupt(force=True)`` — the double-Esc escalation for a wedged step.

A tool that blocks past every cooperative seam wedges the whole step thread:
the gentle interrupt arms the registry mark, but no poll ever runs, so the
turn hangs and the lease stays held. ``force=True`` abandons the step outright
with three existing primitives: ``dispatcher.enqueue`` force-clears the wedged
lease (fencing the abandoned thread — its late writes raise ``InvalidLease``
and land nowhere), a fresh targeted lease runs step-attempt recovery over the
dirty window (``StepAttemptAbandoned`` seal), and the re-drive — under the
still-armed mark — aborts on its first poll and settles the task at the
interrupted next-goal suspend. The conversation resumes by simply typing.

The zombie's own ``InvalidLease`` unwinds the original ``send_goal`` thread;
``_force_terminal_on_lost_lease`` must NOT bulldoze the settled suspend into a
``TaskFailed`` — a durable suspend is a resumable landing (pinned here).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from noeta.client import Client, Options
from noeta.core.fold import fold
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE
from noeta.runtime.worker import STOP_INTERRUPTED_SUSPEND_REASON
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.decorator import tool

_PROMPT = "You are a test agent."

_entered = threading.Event()
_release = threading.Event()


@tool(
    name="wedge",
    version="1",
    risk_level="low",
    input_schema={"type": "object", "additionalProperties": False},
)
def wedge_tool(arguments: dict, ctx: ToolContext) -> ToolResult:
    _entered.set()
    _release.wait(timeout=30.0)
    return ToolResult(success=True, output="finally finished")


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _wedge_call() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id="w-1", tool_name="wedge", arguments={})
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "w-1"},
    )


def _wait(pred, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _types(client: Client, task_id: str) -> list[str]:
    return [e.type for e in client._host.event_log.read(task_id)]


def test_force_interrupt_abandons_a_wedged_tool_and_settles(
    tmp_path: Path,
) -> None:
    _entered.clear()
    _release.clear()
    ws = tmp_path / "ws"
    ws.mkdir()

    provider = FakeLLMProvider(
        responses=[_end("turn1"), _wedge_call(), _end("turn3")]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=(wedge_tool,),
        # Gate-free mode classifies the sealed attempt safe ⇒ recovery
        # re-drives (and the armed mark aborts the re-drive) instead of
        # parking — the deterministic force landing under test.
        permission_mode="bypassPermissions",
    )
    client = Client(
        options,
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
        multi_turn=True,
    )
    try:
        task_id = client.start(goal="turn one").task_id

        # Turn 2 wedges inside the custom tool, on its own thread (the real
        # deployment shape: the drive holds the request thread, Esc arrives on
        # another).
        wedged_error: list[BaseException] = []

        def _drive() -> None:
            try:
                client.send_goal(task_id, goal="turn two")
            except BaseException as exc:  # noqa: BLE001 — asserted below
                wedged_error.append(exc)

        driver_thread = threading.Thread(target=_drive, daemon=True)
        driver_thread.start()
        assert _entered.wait(timeout=10.0), "the wedge tool never started"

        # Gentle interrupt: mark armed, but the wedged tool never reaches a
        # poll — the turn is still running and the thread still stuck.
        client.interrupt(task_id, reason="first Esc")
        time.sleep(0.1)
        host = client._host
        assert fold(host.event_log, host.content_store, task_id).status == "running"
        assert driver_thread.is_alive()

        # Double-Esc: the force path settles the task WHILE the tool is still
        # blocked — the whole point.
        out = client.interrupt(task_id, reason="second Esc", force=True)
        assert not _release.is_set()
        assert out.status == "suspended"
        assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE
        types = _types(client, task_id)
        assert "StepAttemptAbandoned" in types
        suspends = [
            e
            for e in host.event_log.read(task_id)
            if e.type == "TaskSuspended"
        ]
        assert suspends[-1].payload.reason == STOP_INTERRUPTED_SUSPEND_REASON
        assert not host.is_cancelled(task_id)

        # The zombie finishes; its fenced writes must neither corrupt the
        # settled stream nor converge the task to a terminal.
        _release.set()
        assert _wait(lambda: not driver_thread.is_alive())
        after = fold(host.event_log, host.content_store, task_id)
        assert after.status == "suspended"
        assert "TaskFailed" not in _types(client, task_id)

        # And typing again just continues the conversation.
        resumed = client.send_goal(task_id, goal="turn three")
        assert resumed.status == "suspended"
        texts = [
            " ".join(getattr(b, "text", "") for b in m.content)
            for m in fold(
                host.event_log, host.content_store, task_id
            ).runtime.messages
        ]
        assert any("turn3" in t for t in texts)
    finally:
        _release.set()
        client.shutdown()


def test_force_interrupt_on_an_idle_conversation_is_the_gentle_no_op(
    tmp_path: Path,
) -> None:
    """``force`` with nothing wedged degrades to the plain idle landing —
    no enqueue churn, no recovery, conversation untouched and usable."""
    _entered.clear()
    _release.clear()
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(responses=[_end("turn1"), _end("turn2")])
    client = Client(
        Options(system_prompt=_PROMPT, allowed_tools=(wedge_tool,)),
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
        multi_turn=True,
    )
    try:
        task_id = client.start(goal="turn one").task_id

        out = client.interrupt(task_id, reason="nothing running", force=True)
        assert out.status == "suspended"
        assert "StepAttemptAbandoned" not in _types(client, task_id)

        resumed = client.send_goal(task_id, goal="turn two")
        assert resumed.status == "suspended"
        texts = [
            " ".join(getattr(b, "text", "") for b in m.content)
            for m in fold(
                client._host.event_log, client._host.content_store, task_id
            ).runtime.messages
        ]
        assert any("turn2" in t for t in texts)
    finally:
        _release.set()
        client.shutdown()
