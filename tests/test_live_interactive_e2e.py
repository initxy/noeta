"""Real-LLM human-in-the-loop resume, end to end (live marker).

Two suspend→resume handles, driven headless: the model raises the handle, the
driver injects the human side, the model continues. The fake suite pins the
mechanism (``test_code_ask_user_question.py``, ``test_code_approval.py``); what a
real model adds is proof that it actually *chooses* to ask / that its own tool
call parks on the gate, and that the injected reply threads back into its next
turn.

1. **ask_user_question** — the model calls ``AskUserQuestion``; the task suspends
   on a ``question-<id>`` handle; ``driver.answer(...)`` injects a choice; the
   model resumes and its final reply reflects the chosen option.
2. **approval — approve** — with ``Write`` gated, the model's ``Write`` call
   suspends on ``approval-<call_id>`` and the file is NOT yet on disk;
   ``driver.approve(...)`` lets it through and the file lands.
3. **approval — deny** — the same gate, ``driver.deny(...)`` — the file never
   lands and the task finishes terminal with the denial recorded.

The question_id / call_id are chosen at runtime (by the model / engine), so they
are read back from the suspend outcome and the event stream rather than pinned.

Config comes from a git-ignored ``.env`` via ``tests._live_env``. Missing
base/key/model auto-skips; CI never runs these.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.core.fold import fold
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests import _live_env
from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    runner_main_spec,
)

pytestmark = pytest.mark.live

requires_live = _live_env.requires_live


def _model() -> str:
    return _live_env.live_model() or ""


# ---------------------------------------------------------------------------
# Loop 1 — ask_user_question: suspend on a question, driver injects the answer
# ---------------------------------------------------------------------------


@requires_live
def test_live_ask_user_question_answer_resume(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    main = runner_main_spec("main", ask_user_question=True)
    host = make_host(
        make_registry(main),
        workspace_dir=ws,
        provider=_live_env.build_anthropic_provider(),
        model=_model(),
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    driver = make_driver(host)
    out = driver.start(
        goal=(
            "Before doing anything else, use the AskUserQuestion tool to ask me "
            "which deploy target to use. Offer exactly two options: 'Staging' and "
            "'Production'. Once I answer, reply telling me which target I chose."
        ),
        agent="main",
    )
    # The model raised a question and the task parked on its handle.
    assert out.status == "suspended", out.status
    assert out.wake_handle and out.wake_handle.startswith("question-"), out.wake_handle
    requested = [
        e for e in host.event_log.read(out.task_id) if e.type == "UserQuestionRequested"
    ]
    assert requested, "no UserQuestionRequested event"
    question_id = requested[-1].payload.question_id

    # Inject the human answer; the model resumes to a terminal reply.
    result = driver.answer(
        out.task_id,
        question_id=question_id,
        answers={"0": {"selected": ["Staging"]}},
        answered_by="tester",
    )
    assert result.status == "terminal", result.status
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "UserQuestionRequested" in types
    assert "UserQuestionAnswered" in types

    folded = fold(host.event_log, host.content_store, out.task_id)
    assert folded.governance.pending_questions == {}
    assert folded.governance.question_answers[-1]["question_id"] == question_id
    # The answer reached the model: "staging" is only knowable from the choice.
    final_text = " ".join(
        b.text
        for m in folded.runtime.messages
        if m.role == "assistant"
        for b in m.content
        if getattr(b, "text", None)
    ).lower()
    assert "staging" in final_text, f"answer never reflected: {final_text!r}"


# ---------------------------------------------------------------------------
# Approval gate — shared setup
# ---------------------------------------------------------------------------


def _gated_write_session(ws: Path):
    """A one-shot main session that gates ``Write`` for approval, writes applied."""
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=_live_env.build_anthropic_provider(),
        model=_model(),
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        require_approval_tools=("Write",),
    )
    return host, make_driver(host)


def _write_goal() -> str:
    return (
        "Use the Write tool to create a file named gated.txt in the workspace "
        "with the exact contents: ok\nThen reply exactly: done."
    )


def _approval_call_id(host, task_id: str) -> str:
    """The call_id the approval gate parked on, read from the request event."""
    reqs = [
        e
        for e in host.event_log.read(task_id)
        if e.type == "ToolCallApprovalRequested"
    ]
    assert reqs, "no ToolCallApprovalRequested event"
    return reqs[-1].payload.call_id


# ---------------------------------------------------------------------------
# Loop 2 — approval: approve lets the write land
# ---------------------------------------------------------------------------


@requires_live
def test_live_approval_approve_writes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _gated_write_session(ws)
    out = driver.start(goal=_write_goal(), agent="main")
    # The write parked on the approval gate; nothing on disk yet.
    assert out.status == "suspended", out.status
    assert out.wake_handle and out.wake_handle.startswith("approval-"), out.wake_handle
    assert not (ws / "gated.txt").exists()
    call_id = _approval_call_id(host, out.task_id)

    result = driver.approve(out.task_id, call_id=call_id, resolver="host")
    assert result.status == "terminal", result.status
    assert (ws / "gated.txt").is_file(), "approved write never landed"
    assert "ok" in (ws / "gated.txt").read_text(encoding="utf-8")
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "ToolCallApprovalResolved" in types
    assert "ToolResultRecorded" in types


# ---------------------------------------------------------------------------
# Loop 3 — approval: deny blocks the write
# ---------------------------------------------------------------------------


@requires_live
def test_live_approval_deny_blocks(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _gated_write_session(ws)
    out = driver.start(goal=_write_goal(), agent="main")
    assert out.status == "suspended", out.status

    # A real model, handed denial feedback, may retry the Write and re-park on
    # the gate — unlike a scripted provider that ends after one deny. Keep
    # denying (bounded) and assert the invariant that matters at every step:
    # the denied write never lands on disk.
    resolved_any = False
    for _ in range(5):
        if out.status != "suspended":
            break
        assert not (ws / "gated.txt").exists(), "denied write landed"
        call_id = _approval_call_id(host, out.task_id)
        out = driver.deny(
            out.task_id,
            call_id=call_id,
            reason="no writes in this test",
            resolver="host",
        )
        resolved_any = True
    assert resolved_any
    assert not (ws / "gated.txt").exists(), "denied write still landed"
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "ToolCallApprovalResolved" in types
