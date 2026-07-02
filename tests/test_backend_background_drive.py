"""Item 2 — non-blocking backend command endpoints.

``EngineRoom``'s turn-driving verbs used to call the synchronous
``Client`` verbs, blocking the whole HTTP request for the turn's duration
(and any concurrent command 409'd against the running fold). With
``background_drive=True`` the verbs run the driver's ``seed_*`` half on
the caller thread — every durable, validated step, so typed rejections
still raise synchronously — and hand the ``SeededTurn`` to
``Client.drive_seeded`` on a daemon thread. The ack returns immediately;
progress rides the committed event stream, per the T5 contract
("commands return 202 + an ack only").
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from noeta.agent.backend import EngineRoom
from noeta.execution.driver import NotResumableError
from noeta.protocols.messages import LLMRequest, LLMResponse, TextBlock, Usage
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _options() -> Options:
    return Options(
        system_prompt="you finish immediately",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
    )


def _room(
    workspace: Path, provider: FakeLLMProvider, *, background: bool = True
) -> EngineRoom:
    return EngineRoom(
        _options(),
        provider=provider,
        workspace_dir=workspace,
        background_drive=background,
    )


def test_background_start_acks_before_the_turn_finishes(tmp_path: Path) -> None:
    """The command returns the task_id while the LLM round-trip is still in
    flight; the durable seed (TaskCreated + the goal append) is already on
    the stream for SSE consumers; join_drives settles the turn."""
    release = threading.Event()

    def _blocking_responder(request: LLMRequest) -> LLMResponse:
        assert release.wait(timeout=10.0), "test released the provider"
        return _end("done late")

    provider = FakeLLMProvider(responder=_blocking_responder)
    room = _room(tmp_path, provider)
    try:
        task_id = room.start(goal="hello")
        assert task_id
        # The provider has NOT answered yet, but the seed is durable.
        types = [e.type for e in room.events(task_id)]
        assert "TaskCreated" in types
        assert "MessagesAppended" in types  # the goal append
        assert "TaskSuspended" not in types  # turn still in flight

        release.set()
        assert room.join_drives(timeout=10.0)
        types = [e.type for e in room.events(task_id)]
        # The scripted reply landed and the multi-turn wrapper suspended at
        # the next-goal handle.
        assert "TaskSuspended" in types
        assert any(
            "done late" in str(getattr(m, "text", "")) for m in room.messages(task_id)
        )
    finally:
        release.set()
        room.shutdown()


def test_background_typed_rejections_still_raise_synchronously(
    tmp_path: Path,
) -> None:
    """The seed half runs on the caller thread, so the 4xx contract is
    unchanged: approving a task that is not awaiting approval raises
    ``NotResumableError`` immediately (the HTTP layer maps it to 409)."""
    provider = FakeLLMProvider(responses=[_end("done")])
    room = _room(tmp_path, provider)
    try:
        task_id = room.start(goal="hello")
        assert room.join_drives(timeout=10.0)
        with pytest.raises(NotResumableError):
            room.approve(task_id, call_id="nope")
    finally:
        room.shutdown()


def test_background_cancel_interleaves_mid_turn(tmp_path: Path) -> None:
    """The review finding: a command arriving mid-turn used to be impossible
    (the request thread was blocked) — now a cancel lands WHILE the turn is
    in flight and the ReAct loop abandons at the next boundary."""
    release = threading.Event()
    in_llm = threading.Event()

    def _blocking_responder(request: LLMRequest) -> LLMResponse:
        in_llm.set()
        assert release.wait(timeout=10.0), "test released the provider"
        return _end("too late")

    provider = FakeLLMProvider(responder=_blocking_responder)
    room = _room(tmp_path, provider)
    try:
        task_id = room.start(goal="hello")
        assert in_llm.wait(timeout=10.0)
        # Mid-turn command: no 409, no blocked socket.
        room.cancel(task_id, reason="user changed their mind")
        release.set()
        assert room.join_drives(timeout=10.0)
        types = [e.type for e in room.events(task_id)]
        assert "TaskCancelled" in types
    finally:
        release.set()
        room.shutdown()


def test_synchronous_default_is_unchanged(tmp_path: Path) -> None:
    """``background_drive`` defaults off: the verbs settle the turn before
    returning (the embedded / historical contract)."""
    provider = FakeLLMProvider(responses=[_end("done")])
    room = _room(tmp_path, provider, background=False)
    try:
        task_id = room.start(goal="hello")
        types = [e.type for e in room.events(task_id)]
        assert "TaskSuspended" in types  # settled before start() returned
    finally:
        room.shutdown()


def test_background_send_goal_second_turn(tmp_path: Path) -> None:
    provider = FakeLLMProvider(responses=[_end("turn one"), _end("turn two")])
    room = _room(tmp_path, provider)
    try:
        task_id = room.start(goal="first")
        assert room.join_drives(timeout=10.0)
        room.send_goal(task_id, goal="second")
        assert room.join_drives(timeout=10.0)
        texts = [str(getattr(m, "text", "")) for m in room.messages(task_id)]
        assert any("turn two" in t for t in texts)
    finally:
        room.shutdown()
