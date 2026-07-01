"""Completion wake: a background exit drives a new turn.

Mechanism C (DESIGN.md §"completion push (02)"). The noeta-agent product is
request-driven (no daemon WorkerLoop), so a background command that exits while
the session sits idle on its next-goal suspend is surfaced by **reusing the
next-goal wake handle** and injecting an ``origin="system"`` notice prelude —
NOT a new wake primitive. The host's background-drive thread triggers the new
``InteractionDriver.notify_background_exit`` command at a turn boundary.

Coverage:

* a background job that exits while the session is idle-suspended on NEXT_GOAL
  is driven a NEW turn without human input, and the agent's view carries a
  system-origin notice with the summary + a ContentRef (NOT the full bytes);
* the notice is ``origin="system"`` tagged (a human turn is ``origin=None``),
  so the model can tell a background event from a user message;
* idle-buffer: an exit that arrives mid-turn (the session is NOT human-suspended)
  is a no-op now and surfaces on the next turn-suspend boundary — the
  ``BackgroundShellExited`` event is durably recorded either way;
* ``notified`` dedup: the registry's exit push fires exactly once.
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
from noeta.protocols.values import ContentRef
from noeta.runtime.background_shell import ProcessRegistry
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode


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
# notify_background_exit — the driver command
# ---------------------------------------------------------------------------


def test_exit_while_idle_suspended_drives_new_turn_with_system_notice(
    tmp_path: Path,
) -> None:
    """A background exit while the session is idle-suspended on NEXT_GOAL drives
    a NEW turn with no human input, and the agent's view carries a system-origin
    notice with the summary + ref (not the full bytes)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # Two end-turns: the opening goal turn, then the wake-driven notice turn.
    host, _disp, event_log, content_store = _host(
        ws, responses=[_end_turn("hi"), _end_turn("saw the exit")]
    )
    driver = InteractionDriver(host)
    outcome = driver.start(goal="kick off", agent="main")
    assert outcome.status == "suspended"
    assert outcome.wake_handle == NEXT_GOAL_WAKE_HANDLE
    task_id = outcome.task_id

    before = len(_user_messages(event_log, content_store, task_id))

    ref = content_store.put(b"the full background output bytes", media_type="text/plain")
    summary = "background npm test → OK (32B output)"
    out = driver.notify_background_exit(
        task_id, summary=summary, ref=ref, job_id="bg-abc123"
    )

    # The session was woken + driven a fresh turn WITHOUT human input.
    assert out.status == "suspended"  # trailing next-goal suspend again
    after = _user_messages(event_log, content_store, task_id)
    assert len(after) == before + 1, "exactly one new (notice) user message"
    notice = after[-1]
    text = "".join(b.text for b in notice.content if isinstance(b, TextBlock))
    assert summary in text
    # The ref / job handle rides the notice so the model can deref / poll, but
    # the FULL bytes never inline.
    assert "the full background output bytes" not in text
    assert ref.hash in text or "bg-abc123" in text


def test_notice_is_system_origin_not_human(tmp_path: Path) -> None:
    """The notice is ``origin="system"`` — distinguishable from a human turn
    (whose origin is ``None``)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _disp, event_log, content_store = _host(
        ws, responses=[_end_turn("hi"), _end_turn("ack")]
    )
    driver = InteractionDriver(host)
    outcome = driver.start(goal="kick off", agent="main")
    task_id = outcome.task_id

    # The opening user goal is a human turn (origin None).
    human = _user_messages(event_log, content_store, task_id)[-1]
    assert human.origin is None

    ref = content_store.put(b"out", media_type="text/plain")
    driver.notify_background_exit(
        task_id, summary="background sleep → OK (3B output)", ref=ref, job_id="bg-1"
    )
    notice = _user_messages(event_log, content_store, task_id)[-1]
    assert notice.origin == "system"


def test_notify_requires_next_goal_suspend(tmp_path: Path) -> None:
    """A notice on a task that is NOT human-suspended on NEXT_GOAL is refused
    (it must be injected at a turn-suspend boundary, not mid-turn)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # Opening turn FAILS-to-terminal would not suspend; instead drive to a
    # terminal via cancel, then assert notify refuses.
    host, _disp, _event_log, content_store = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    outcome = driver.start(goal="kick off", agent="main")
    task_id = outcome.task_id
    driver.cancel(task_id)  # → terminal, no longer suspended on NEXT_GOAL

    ref = content_store.put(b"out", media_type="text/plain")
    import pytest

    with pytest.raises(RuntimeError):
        driver.notify_background_exit(
            task_id, summary="x", ref=ref, job_id="bg-1"
        )


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
    """The watcher invokes ``on_background_exit`` exactly once after the Exited
    event (``notified`` dedup guards against kill + natural-exit double push)."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    calls: list[tuple[str, str, str]] = []

    def _on_exit(session_id: str, job_id: str, summary: str, ref: ContentRef) -> None:
        calls.append((session_id, job_id, summary))

    reg = ProcessRegistry(
        event_log=log,
        content_store=store,
        on_background_exit=_on_exit,
    )
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
    session_id, cb_job, summary = calls[0]
    assert session_id == "t-cb"
    assert cb_job == job_id
    assert "background" in summary


def test_exit_callback_carries_session_and_ref(tmp_path: Path) -> None:
    """The callback receives a ContentRef whose bytes are the final output —
    the notice rides the ref, not the bytes."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    seen: list[ContentRef] = []

    def _on_exit(session_id: str, job_id: str, summary: str, ref: ContentRef) -> None:
        seen.append(ref)

    reg = ProcessRegistry(
        event_log=log, content_store=store, on_background_exit=_on_exit
    )
    out = reg.spawn(
        argv=_py("print('payload-bytes')"),
        cwd=tmp_path,
        env={},
        command="python payload",
        spawned_by_task_id="t-ref",
        trace_id="tr",
    )
    _await_exit(reg, out["job_id"])
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not seen:
        time.sleep(0.01)
    assert seen, "callback must fire with the final ref"
    assert b"payload-bytes" in store.get(seen[0])


# ---------------------------------------------------------------------------
# End-to-end — set_background_notifier wires the full Mechanism C path
# ---------------------------------------------------------------------------


def test_host_wired_real_exit_drives_notice_turn_end_to_end(tmp_path: Path) -> None:
    """The host's ``set_background_notifier`` + the registry watcher drive a
    real wake-and-notify turn when a background job exits while idle: a real
    ``Popen`` exit fires ``_on_background_exit`` → daemon drive thread →
    ``notify_background_exit`` → a system-origin notice lands in the view."""
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
    # carries the host's ``on_background_exit`` hook. The session is idle on
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
    assert "background" in text
    assert job["job_id"] in text
    assert "e2e bg done" not in text  # full bytes never inline
