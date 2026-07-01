"""Phase 4.5 I3 — multi-turn chat session.

Grouped acceptance:

* **MultiTurnReActPolicy unit tests** — wrapper preserves ``state_patch`` and
  ``assistant_message`` on non-final turns and passes other Decision shapes
  through.
* **End-to-end two-turn run via the SDK driver** — the EventLog carries the
  turn boundary (``TaskSuspended``/``TaskWoken``) and the workspace edit from
  turn 1 survives into turn 2's compose.
* **Per-turn ``CodeSessionResult`` slicing** — turn-2's projected report covers
  only the second turn's events (windowed past the turn-1 cursor).

Driven through the production
``InteractionDriver`` (``start`` → ``send_goal``). Unlike the deleted runner's
``set_turn_final`` (which synthesised a ``TaskCompleted`` on the "final" turn),
the SDK multi-turn conversation **never self-terminates** — every turn rests on
the next-goal suspend and the conversation is ended out-of-band via the control
plane (``close``). The lifecycle assertions reflect that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.core.fold import fold
from noeta.execution.driver import NotResumableError
from noeta.execution.multi_turn import (
    NEXT_GOAL_WAKE_HANDLE,
    MultiTurnReActPolicy,
)
from noeta.protocols.decisions import (
    FailDecision,
    FinishDecision,
    TaskStatePatch,
    ToolCallsDecision,
    YieldForHumanDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View
from noeta.protocols.wake import HumanResponseReceived
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    runner_main_spec,
    session_result,
)


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.py").write_text("foo\n")
    return ws


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


# ---------------------------------------------------------------------------
# MultiTurnReActPolicy unit
# ---------------------------------------------------------------------------


class _FakeInnerPolicy:
    """A Policy that returns whatever Decision we hand it next."""

    def __init__(self) -> None:
        self.next_decision: Any = None
        self.received: list[tuple[StepContext, View]] = []

    def decide(self, ctx: StepContext, view: View) -> Any:
        self.received.append((ctx, view))
        return self.next_decision


def _ctx() -> StepContext:
    return StepContext(task_id="t1", lease_id="l1", trace_id="tr1")


def _view() -> View:
    """A view shape any wrapper-level test can ignore."""
    return View(plan_ref=None, segments=(), provider_tool_schemas=[])


def test_wrapper_passes_through_when_final_true() -> None:
    inner = _FakeInnerPolicy()
    wrapper = MultiTurnReActPolicy(inner, final=True)
    decision = FinishDecision(
        answer="done",
        state_patch=TaskStatePatch(set_phase="closed"),
        assistant_message=Message(role="assistant", content=[TextBlock(text="bye")]),
    )
    inner.next_decision = decision
    assert wrapper.decide(_ctx(), _view()) is decision


def test_wrapper_translates_finish_to_yield_when_final_false() -> None:
    """Architect constraint #1: preserve state_patch and
    assistant_message verbatim from the inner FinishDecision."""
    inner = _FakeInnerPolicy()
    wrapper = MultiTurnReActPolicy(inner, final=False)
    patch = TaskStatePatch(set_phase="turn1-done")
    msg = Message(
        role="assistant",
        content=[TextBlock(text="turn1 closing")],
    )
    inner.next_decision = FinishDecision(
        answer="ok", state_patch=patch, assistant_message=msg
    )
    result = wrapper.decide(_ctx(), _view())
    assert isinstance(result, YieldForHumanDecision)
    assert result.prompt == NEXT_GOAL_WAKE_HANDLE
    assert result.state_patch is patch
    assert result.assistant_message is msg


def test_wrapper_passes_non_finish_decisions_through_in_either_mode() -> None:
    inner = _FakeInnerPolicy()
    inner.next_decision = ToolCallsDecision(calls=[])
    for final in (False, True):
        wrapper = MultiTurnReActPolicy(inner, final=final)
        assert wrapper.decide(_ctx(), _view()) is inner.next_decision

    inner.next_decision = FailDecision(reason="boom")
    for final in (False, True):
        wrapper = MultiTurnReActPolicy(inner, final=final)
        assert wrapper.decide(_ctx(), _view()) is inner.next_decision


def test_wrapper_set_final_is_mutable_post_construction() -> None:
    """Architect constraint #1: the runner flips `final` between
    turns without rebuilding the Engine/Policy."""
    inner = _FakeInnerPolicy()
    wrapper = MultiTurnReActPolicy(inner, final=False)
    inner.next_decision = FinishDecision(answer="x")
    assert isinstance(wrapper.decide(_ctx(), _view()), YieldForHumanDecision)
    wrapper.set_final(True)
    inner.next_decision = FinishDecision(answer="y")
    assert isinstance(wrapper.decide(_ctx(), _view()), FinishDecision)


def test_wrapper_state_patch_lands_on_engine_event_log(tmp_path: Path) -> None:
    """Architect constraint #1 integration pin: state_patch the inner
    policy attached to FinishDecision must show up as a
    TaskStatePatched event after the wrapper translates the call to
    a YieldForHumanDecision and the Engine processes it."""
    from noeta.core.engine import Engine
    from noeta.storage.memory import (
        InMemoryContentStore,
        InMemoryDispatcher,
        InMemoryEventLog,
    )
    from noeta.testing.composer import trivial_three_segment

    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    cs = InMemoryContentStore()

    class _FakePolicyOneShot:
        def decide(self, ctx: StepContext, view: View) -> Any:
            return FinishDecision(
                answer="ok",
                state_patch=TaskStatePatch(set_phase="turn1-done"),
                assistant_message=Message(
                    role="assistant",
                    content=[TextBlock(text="turn1 closing")],
                ),
            )

    wrapper = MultiTurnReActPolicy(_FakePolicyOneShot(), final=False)
    engine = Engine(
        event_log=event_log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=wrapper,
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w", lease_seconds=60.0)
    assert lease is not None
    engine.append_user_message(task, content=[TextBlock(text="g")], lease_id=lease.lease_id)
    task = engine.run_one_step(task, lease_id=lease.lease_id)

    assert task.status == "suspended"
    assert isinstance(task.wake_on, HumanResponseReceived)
    assert task.wake_on.handle == NEXT_GOAL_WAKE_HANDLE
    # state_patch survived: fold-derived state.phase + recorded event payload.
    assert task.state.phase == "turn1-done"
    events = event_log.read(task.task_id)
    patched = [env for env in events if env.type == "TaskStatePatched"]
    assert any(
        env.payload.patch.get("set_phase") == "turn1-done" for env in patched
    )
    # assistant_message lands in runtime.messages so the next compose sees it.
    assistant_texts: list[str] = []
    for msg in task.runtime.messages:
        if msg.role == "assistant":
            for block in msg.content:
                if isinstance(block, TextBlock):
                    assistant_texts.append(block.text)
    assert "turn1 closing" in assistant_texts


# ---------------------------------------------------------------------------
# End-to-end via the SDK driver — two-turn run
# ---------------------------------------------------------------------------


def _two_turn_responses() -> list[LLMResponse]:
    """Turn 1: replace foo→bar then end_turn (→ next-goal suspend).
       Turn 2: read x.py then end_turn (→ next-goal suspend)."""
    return [
        # Turn 1
        _tool_call(
            "t1-r1",
            "edit",
            {"path": "x.py", "old": "foo", "new": "bar"},
        ),
        _end_turn("turn1 done"),
        # Turn 2
        _tool_call("t2-r1", "read_file", {"path": "x.py"}),
        _end_turn("turn2 done"),
    ]


def _multi_turn_session(
    workspace: Path,
    responses: list[LLMResponse],
    *,
    write_mode: FsWriteMode = FsWriteMode.APPLY,
    shell_mode: ShellMode = ShellMode.OFF,
):
    """An interactive (multi_turn=True) SDK host + driver. ``require_approval_tools=()``
    so the host's default permission gate does not pause the edit family."""
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        multi_turn=True,
        write_mode=write_mode,
        shell_mode=shell_mode,
        require_approval_tools=(),
    )
    return host, make_driver(host)


def _last_seq(host, task_id: str) -> int:
    return host.event_log.read(task_id)[-1].seq


def test_two_turn_run_lifecycle_emits_expected_events(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    host, driver = _multi_turn_session(workspace, _two_turn_responses())
    # Turn 1 → rests on the next-goal suspend; the file edit applied.
    first = driver.start(goal="rename foo to bar", agent="main")
    assert first.status == "suspended"
    assert first.wake_handle == NEXT_GOAL_WAKE_HANDLE
    assert (workspace / "x.py").read_text() == "bar\n"

    # Turn 2 → also rests on the next-goal suspend (the SDK conversation never
    # self-terminates; it is closed via the control plane, not a "final" turn).
    second = driver.send_goal(first.task_id, goal="now read x.py back")
    assert second.status == "suspended"
    assert second.wake_handle == NEXT_GOAL_WAKE_HANDLE
    events = host.event_log.read(first.task_id)

    types = [env.type for env in events]
    # TaskStarted once (the wake does not re-emit it); the conversation never
    # completes — the SDK multi-turn path rests on the next-goal suspend.
    assert types.count("TaskStarted") == 1
    assert "TaskCompleted" not in types
    # Intermediate turn boundary.
    assert "TaskSuspended" in types
    assert "TaskWoken" in types
    # `TaskWoken`'s payload carries the next-goal wake the send_goal delivered.
    woken = next(env for env in events if env.type == "TaskWoken")
    assert isinstance(woken.payload.wake_event, HumanResponseReceived)
    assert woken.payload.wake_event.handle == NEXT_GOAL_WAKE_HANDLE


def test_per_turn_result_slices_only_that_turn(tmp_path: Path) -> None:
    """Per-turn ``CodeSessionResult`` reports only the events produced by that
    turn (windowed past the turn-1 cursor), not the cumulative stream."""
    workspace = _make_workspace(tmp_path)
    host, driver = _multi_turn_session(workspace, _two_turn_responses())
    first = driver.start(goal="rename foo to bar", agent="main")
    cursor = _last_seq(host, first.task_id)
    second = driver.send_goal(first.task_id, goal="now read x.py back")
    # Turn 1 wrote x.py once.
    first_result = session_result(host, first)
    assert [c["path"] for c in first_result.files_changed] == ["x.py"]
    # Turn 2 only read — windowing past the turn-1 cursor, files_changed is empty
    # (NOT carrying turn-1's row forward).
    second_result = session_result(host, second, after_seq=cursor)
    assert second_result.files_changed == ()


def test_per_turn_last_shell_is_not_cumulative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-turn ``last_shell`` reflects only the current turn's shell call, not
    the previous turn's (windowed projection)."""
    workspace = _make_workspace(tmp_path)

    # Distinguishable git_status output per turn via a monkeypatched
    # subprocess.run keyed on a mutable turn marker.
    import subprocess

    state = {"turn": 1}

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        # `shell_run` records returncode in its output; vary it per turn
        # so the per-turn last_shell is distinguishable.
        rc = 0 if state["turn"] == 1 else 3
        return subprocess.CompletedProcess(
            args=argv, returncode=rc, stdout=b"pytest output\n", stderr=b""
        )

    monkeypatch.setattr("noeta.tools.fs.shell.subprocess.run", fake_run)

    responses = [
        # Turn 1: pytest (rc=0) then end_turn → suspend.
        _tool_call("t1-s", "shell_run", {"command": "pytest -q"}),
        _end_turn("turn 1"),
        # Turn 2: pytest (rc=3) then end_turn → suspend.
        _tool_call("t2-s", "shell_run", {"command": "pytest -q"}),
        _end_turn("turn 2"),
    ]
    host, driver = _multi_turn_session(
        workspace, responses,
        write_mode=FsWriteMode.DRY_RUN, shell_mode=ShellMode.ALLOWLIST,
    )
    first = driver.start(goal="check status", agent="main")
    assert first.status == "suspended"
    first_result = session_result(host, first)
    assert first_result.last_shell is not None
    assert first_result.last_shell["returncode"] == 0
    cursor = _last_seq(host, first.task_id)
    state["turn"] = 2
    second = driver.send_goal(first.task_id, goal="check status again")
    assert second.status == "suspended"
    # Turn-2 last_shell is the rc=3 call, NOT carrying turn-1's rc=0.
    second_result = session_result(host, second, after_seq=cursor)
    assert second_result.last_shell is not None
    assert second_result.last_shell["returncode"] == 3


_SKILL_BODY = (
    "---\n"
    "name: tidy\n"
    "description: keep edits minimal\n"
    "---\n"
    "Make the smallest change that satisfies the goal.\n"
)


def test_skill_carries_into_turn_two_without_reactivation(tmp_path: Path) -> None:
    """An activated workspace skill is recorded once via a durable
    TaskStatePatched before turn 1 and remains selected on turn 2 — NOT
    re-emitted per turn."""
    workspace = _make_workspace(tmp_path)
    skills_dir = workspace / ".noeta" / "skills" / "tidy"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(_SKILL_BODY)

    responses = [
        _tool_call("t1-r", "read_file", {"path": "x.py"}),
        _end_turn("turn 1"),
        _tool_call("t2-r", "read_file", {"path": "x.py"}),
        _end_turn("turn 2"),
    ]
    host, driver = _multi_turn_session(
        workspace, responses,
        write_mode=FsWriteMode.DRY_RUN, shell_mode=ShellMode.OFF,
    )
    first = driver.start(
        goal="look at x.py", agent="main", activations=("tidy",)
    )
    second = driver.send_goal(first.task_id, goal="look again")
    events = host.event_log.read(first.task_id)
    # Skill selected on BOTH turns (the resident activation persists).
    assert "tidy" in session_result(host, first).selected_skills
    assert "tidy" in session_result(host, second).selected_skills
    # Activation is durable + ONE-shot: a single TaskStatePatched, not
    # re-emitted per turn.
    patched = [e for e in events if e.type == "TaskStatePatched"]
    assert len(patched) == 1
    assert patched[0].payload.patch["activate_skills"] == ["tidy"]


def test_resume_with_goal_refuses_non_next_goal_wake(tmp_path: Path) -> None:
    """``send_goal`` must only satisfy the ``noeta-code-next-goal`` human handle.
    A task suspended on a different wake condition (here: an approval gate) must
    raise and emit no ``TaskWoken``."""
    workspace = _make_workspace(tmp_path)
    # Gate the edit so turn 1 suspends on an approval handle, NOT next-goal.
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=_two_turn_responses()),
        model="gpt-test",
        multi_turn=True,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        require_approval_tools=("edit",),
    )
    driver = make_driver(host)
    first = driver.start(goal="rename foo to bar", agent="main")
    assert first.status == "suspended"
    assert first.wake_handle == "approval-t1-r1"  # NOT next-goal
    events_before = len(host.event_log.read(first.task_id))

    with pytest.raises(NotResumableError, match="noeta-code-next-goal"):
        driver.send_goal(first.task_id, goal="should be refused")
    # No new events (no TaskWoken) emitted by the refused send_goal.
    assert len(host.event_log.read(first.task_id)) == events_before


def test_assistant_message_carries_into_turn_two_compose(tmp_path: Path) -> None:
    """The next turn's compose builds on a history that includes turn 1's
    closing assistant turn (MessagesAppended grows across the send_goal)."""
    workspace = _make_workspace(tmp_path)
    host, driver = _multi_turn_session(workspace, _two_turn_responses())
    first = driver.start(goal="rename foo to bar", agent="main")
    # Snapshot the recorded MessagesAppended events between turns.
    msg_events_before = [
        env for env in host.event_log.read(first.task_id)
        if env.type == "MessagesAppended"
    ]
    driver.send_goal(first.task_id, goal="now read x.py back")
    msg_events_after = [
        env for env in host.event_log.read(first.task_id)
        if env.type == "MessagesAppended"
    ]
    # Turn 1 appended the assistant `end_turn` message → that
    # MessagesAppended is in the pre-T2 snapshot and survives into T2.
    assert len(msg_events_after) > len(msg_events_before)
