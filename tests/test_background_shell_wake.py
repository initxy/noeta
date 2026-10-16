"""Completion wake: a background exit to a parked session drives a new turn.

The notice is queued as a pending injection and, when no running turn will
take it in, the parked session is woken on its **next-goal wake handle** — no
new wake primitive — for a turn whose first drain appends it as an
``origin="system"`` message, distinguishable from a human turn (whose origin
is ``None``). It fires exactly once even when kill and natural exit race.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tests._sdk_session import official_registry as official_agent_registry
from noeta.client import SdkHost
from noeta.core.fold import fold
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.runtime.background_shell import ProcessRegistry
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode


# ---------------------------------------------------------------------------
# Helpers
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
) -> tuple[SdkHost, InMemoryDispatcher, InMemoryEventLog, InMemoryContentStore]:
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
    return host, dispatcher, event_log, content_store


def _user_messages(event_log: Any, content_store: Any, task_id: str) -> list[Any]:
    """The folded conversation's user-channel messages."""
    task = fold(event_log, content_store, task_id)
    return [m for m in task.runtime.messages if m.role == "user"]


# ---------------------------------------------------------------------------
# inject_notice + deliver_injections — the driver commands
# ---------------------------------------------------------------------------


def test_notice_to_a_parked_session_drives_a_turn_with_a_system_notice(
    tmp_path: Path,
) -> None:
    """A notice queued while the session is parked on NEXT_GOAL is taken in by
    a NEW turn driven with no human input, as an ``origin="system"`` message."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _disp, event_log, content_store = _host(
        ws, responses=[_end_turn("hi"), _end_turn("saw the exit")]
    )
    driver = InteractionDriver(host)
    outcome = driver.start(goal="kick off", agent="main")
    assert outcome.status == "suspended"
    assert outcome.wake_handle == NEXT_GOAL_WAKE_HANDLE
    task_id = outcome.task_id
    human = _user_messages(event_log, content_store, task_id)[-1]
    assert human.origin is None

    notice = "<task-notification>\n<task-id>bg-1</task-id>\n</task-notification>"
    driver.inject_notice(task_id, notice=notice)
    pending = fold(event_log, content_store, task_id).governance.pending_injections
    assert len(pending) == 1
    out = driver.deliver_injections(task_id)

    assert out.status == "suspended"  # trailing next-goal suspend again
    task = fold(event_log, content_store, task_id)
    assert task.governance.pending_injections == {}
    last = _user_messages(event_log, content_store, task_id)[-1]
    assert last.origin == "system"
    assert [b.text for b in last.content if isinstance(b, TextBlock)] == [notice]


def test_deliver_injections_requires_next_goal_suspend(tmp_path: Path) -> None:
    """Delivery drives a turn only for a session parked on NEXT_GOAL; anything
    else is refused so the caller waits for the running turn to take it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _disp, _event_log, _content_store = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    task_id = driver.start(goal="kick off", agent="main").task_id
    driver.cancel(task_id)  # → terminal, so not suspended on NEXT_GOAL

    import pytest

    with pytest.raises(RuntimeError):
        driver.deliver_injections(task_id)


# ---------------------------------------------------------------------------
# ProcessRegistry — observer origin + exit callback + notified dedup
# ---------------------------------------------------------------------------


def _py(code: str) -> list[str]:
    import sys

    return [sys.executable, "-c", code]


def _await_exit(reg: ProcessRegistry, job_id: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if reg.poll(job_id)["status"] == "exited":
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not exit within {timeout_s}s")


def test_lifecycle_events_are_observer_origin(tmp_path: Path) -> None:
    """Started/Polled/Exited are ``origin="observer"`` (ChildLifecycleObserver
    precedent) so the post-wake resume segment re-injects an Exited that
    lands while the session is suspended."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    reg = ProcessRegistry(event_log=log, content_store=store)
    out = reg.spawn(
        argv=_py("print('hi')"),
        cwd=tmp_path,
        env={},
        command="python hi",
        spawned_by_task_id="t-obs",
        trace_id="tr",
    )
    job_id = out["job_id"]
    _await_exit(reg, job_id)
    reg.poll(job_id)
    by_type = {e.type: e for e in log.read("t-obs")}
    assert by_type["BackgroundShellStarted"].origin == "observer"
    assert by_type["BackgroundShellPolled"].origin == "observer"
    assert by_type["BackgroundShellExited"].origin == "observer"


def test_exit_invokes_host_callback_once(tmp_path: Path) -> None:
    """The watcher hands the end notice to ``on_notice`` exactly once after the
    Exited event (``notified`` dedup guards against kill + natural-exit double
    push)."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    calls: list[tuple[str, str, str]] = []

    def _on_exit(root_task_id: str, job_id: str, notice: str) -> None:
        calls.append((root_task_id, job_id, notice))

    reg = ProcessRegistry(event_log=log, content_store=store, on_notice=_on_exit)
    out = reg.spawn(
        argv=_py("print('bye')"),
        cwd=tmp_path,
        env={},
        command="python bye",
        spawned_by_task_id="t-cb",
        trace_id="tr",
    )
    job_id = out["job_id"]
    _await_exit(reg, job_id)
    # Give the watcher a beat to fire the callback after reaping.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not calls:
        time.sleep(0.01)
    assert len(calls) == 1
    root_task_id, cb_job, notice = calls[0]
    assert root_task_id == "t-cb"
    assert cb_job == job_id
    assert notice == (
        "<task-notification>\n"
        f"<task-id>{job_id}</task-id>\n"
        "<status>completed</status>\n"
        '<summary>Background command "python bye" completed (exit code 0)</summary>\n'
        "</task-notification>\nLast output:\nbye"
    )


def test_end_notice_carries_the_output_and_the_launch_description(
    tmp_path: Path,
) -> None:
    """The end notice names the job by the launch's ``description``, carries
    the tool call that started it and the output's tail; the final snapshot
    stays on the Exited event for audit."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    seen: list[str] = []

    def _on_exit(root_task_id: str, job_id: str, notice: str) -> None:
        seen.append(notice)

    reg = ProcessRegistry(event_log=log, content_store=store, on_notice=_on_exit)
    out = reg.spawn(
        argv=_py("import sys; print('payload-bytes'); sys.exit(3)"),
        cwd=tmp_path,
        env={},
        command="python payload",
        spawned_by_task_id="t-ref",
        trace_id="tr",
        description="Write the payload",
        tool_use_id="toolu_1",
    )
    _await_exit(reg, out["job_id"])
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not seen:
        time.sleep(0.01)
    assert seen, "callback must fire with the end notice"
    assert "<tool-use-id>toolu_1</tool-use-id>" in seen[0]
    assert "<status>failed</status>" in seen[0]
    assert (
        '<summary>Background command "Write the payload" failed with exit code '
        "3</summary>"
    ) in seen[0]
    assert seen[0].endswith("Last output:\npayload-bytes")
    exited = next(e for e in log.read("t-ref") if e.type == "BackgroundShellExited")
    assert b"payload-bytes" in store.get(exited.payload.final_ref)


# ---------------------------------------------------------------------------
# End-to-end — set_background_notifier wires the full Mechanism C path
# ---------------------------------------------------------------------------


def test_host_wired_real_exit_drives_notice_turn_end_to_end(tmp_path: Path) -> None:
    """The host's ``set_background_notifier`` + the registry watcher drive a
    real wake-and-notify turn when a background job exits while idle: a real
    ``Popen`` exit queues the notice (``inject_notice``) and the settle thread
    drives the parked session a turn (``deliver_injections``) that takes it in."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _disp, event_log, content_store = _host(
        ws, responses=[_end_turn("hi"), _end_turn("saw exit")]
    )
    driver = InteractionDriver(host)
    host.set_background_notifier(driver)

    outcome = driver.start(goal="kick off", agent="main")
    assert outcome.status == "suspended"
    task_id = outcome.task_id
    before = len(_user_messages(event_log, content_store, task_id))

    # Spawn a real (fast) background job ON THE HOST'S registry so its watcher
    # carries the host's notice hook. The session is idle on
    # NEXT_GOAL, so the exit must drive a fresh notice turn with no human input.
    reg = host._process_registry  # noqa: SLF001 — test reaches the wired registry
    assert reg is not None
    job = reg.spawn(
        argv=_py("print('e2e bg done')"),
        cwd=ws,
        env={},
        command="python e2e",
        spawned_by_task_id=task_id,
        trace_id="tr-e2e",
    )

    # Wait for the drive thread to inject the notice turn.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        msgs = _user_messages(event_log, content_store, task_id)
        if len(msgs) > before:
            break
        time.sleep(0.02)
    msgs = _user_messages(event_log, content_store, task_id)
    assert len(msgs) == before + 1, "the background exit drove a notice turn"
    notice = msgs[-1]
    assert notice.origin == "system"
    text = "".join(b.text for b in notice.content if isinstance(b, TextBlock))
    assert text.startswith("<task-notification>")
    assert f"<task-id>{job['job_id']}</task-id>" in text
    assert "e2e bg done" in text  # the actual output rides inline
    assert fold(event_log, content_store, task_id).governance.pending_injections == {}
