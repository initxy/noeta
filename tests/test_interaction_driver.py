"""Issue 05 — the shared ``InteractionDriver``.

Drives the five conversation commands (``start`` / ``send_goal`` /
``approve`` / ``deny`` / ``cancel``) against a real resident
:class:`noeta.agent.resolver.SdkHost` host + an in-memory runtime,
and asserts:

* the four turn-driving commands route through the SHARED
  :func:`noeta.runtime.worker.run_leased_task` primitive + the issue-01
  woken-command-prelude seam (no second runtime) — proven by the recorded
  envelope shape (``TaskWoken`` → prelude events → step) rather than a mock;
* a created Task is driven by **its own Agent's Engine** (the issue-02
  resolver), with the chosen ``agent_name`` recorded on ``TaskCreated``;
* interactive turns run ``final=False`` — a normally-finishing turn ends in
  a trailing next-goal suspend, while a **fail** turn still terminates;
* the model selector is validated against the stub allowlist;
* ``cancel`` writes the pre-existing L0 ``TaskCancelled`` event (no new
  schema) and folds the Task to terminal.
"""

from __future__ import annotations

from tests._sdk_session import official_registry as official_agent_registry
from pathlib import Path
from typing import Any

import pytest

from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.execution.driver import (
    InteractionDriver,
    ModelSelectorError,
    multi_turn_policy_wrapper,
)
from noeta.client import SdkHost
from noeta.agent.registry import UnknownAgentError
from noeta.core.fold import fold, messages_from_appended
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.values import LOCAL_PRINCIPAL
from noeta.protocols.wake import HumanResponseReceived
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.tools.fs import FsWriteMode, ShellMode


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


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
        raw={"id": "end-" + text},
    )


def _host(
    workspace: Path,
    *,
    responses: list[LLMResponse],
    require_approval_tools: tuple[str, ...] = (),
) -> tuple[SdkHost, InMemoryDispatcher, InMemoryEventLog]:
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
        require_approval_tools=require_approval_tools,
        # Interactive surface: a normally-finishing turn suspends, not
        # completes.
        policy_wrapper=multi_turn_policy_wrapper,
    
        registry=official_agent_registry(),
        aliases={"default": "main"},)
    return host, dispatcher, event_log


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_creates_task_with_chosen_agent_and_suspends(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)

    outcome = driver.start(goal="hello", agent="main")

    # Interactive turn final=False → a normally-finishing first turn ends in a
    # trailing next-goal suspend, NOT a terminal.
    assert outcome.status == "suspended"
    assert outcome.wake_handle == NEXT_GOAL_WAKE_HANDLE

    events = event_log.read(outcome.task_id)
    created = next(e for e in events if e.type == "TaskCreated")
    # The chosen agent is the authoritative recorded agent_name.
    # Passing the name "main" records the canonical name "main"; passing the
    # alias "default" also records the canonical name after internal resolution.
    assert created.payload.agent_name == "main"
    # No control-plane terminal manufactured.
    assert "TaskCompleted" not in [e.type for e in events]


def test_start_unknown_agent_hard_errors(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, _ = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(host)
    with pytest.raises(UnknownAgentError):
        driver.start(goal="x", agent="no-such-agent")


def test_start_validates_model_selector(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, _ = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(host)
    with pytest.raises(ModelSelectorError):
        driver.start(goal="x", agent="main", model_selector="gpt-9000")
    # An allowlisted selector is accepted (and otherwise unused this slice).
    out = driver.start(goal="x", agent="main", model_selector="sonnet")
    assert out.status == "suspended"


def test_start_threads_effort_to_llm_request(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, _ = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)

    out = driver.start(goal="hello", agent="main", effort="high")

    assert out.status == "suspended"
    provider = host.default_provider_instance
    assert isinstance(provider, FakeLLMProvider)
    assert provider.received_requests[-1].effort == "high"


def test_send_goal_threads_effort_per_turn(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, _ = _host(
        ws, responses=[_end_turn("turn1"), _end_turn("turn2")]
    )
    driver = InteractionDriver(host)

    started = driver.start(goal="first", agent="main", effort="low")
    out = driver.send_goal(started.task_id, goal="second", effort="high")

    assert out.status == "suspended"
    provider = host.default_provider_instance
    assert isinstance(provider, FakeLLMProvider)
    assert [r.effort for r in provider.received_requests] == ["low", "high"]


def test_spawned_child_inherits_turn_effort(tmp_path: Path) -> None:
    """A delegated child runs on the spawning turn's reasoning effort — the
    whole delegation tree shares the root session's per-turn override, same as
    permission_mode / provider. Without inheritance the child fell back to
    effort None, which on the Responses provider used to also drop the
    reasoning-ciphertext include and broke the child's prompt-cache prefix."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # Wired by hand (not via _host): without the ChildLifecycleObserver the
    # spawned child is never enqueued and the parent's SubtaskCompleted wake
    # never fires.
    from noeta.core.wiring import wire_default_observers

    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    host = SdkHost(
        event_log=event_log,
        content_store=InMemoryContentStore(),
        dispatcher=dispatcher,
        provider=FakeLLMProvider(
            responses=[
                _tool_call(
                    "s1", "spawn_subagent", {"agent": "explore", "goal": "scout"}
                ),
                _end_turn("scouted"),
                _end_turn("done"),
            ]
        ),
        model="gpt-test",
        workspace_dir=ws,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        require_approval_tools=(),
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
    )
    driver = InteractionDriver(host)

    out = driver.start(goal="delegate then finish", agent="main", effort="xhigh")

    assert out.status == "suspended"
    provider = host.default_provider_instance
    assert isinstance(provider, FakeLLMProvider)
    # [0] parent spawn turn; [1] the child's turn; [2] the resumed parent.
    assert [r.effort for r in provider.received_requests] == [
        "xhigh", "xhigh", "xhigh",
    ]


# ---------------------------------------------------------------------------
# send_goal — append-message prelude, shared primitive
# ---------------------------------------------------------------------------


def test_send_goal_drives_followup_turn_through_prelude(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws, responses=[_end_turn("turn1"), _end_turn("turn2")]
    )
    driver = InteractionDriver(host)

    started = driver.start(goal="first", agent="main")
    assert started.status == "suspended"

    pre = event_log.read(started.task_id)
    out = driver.send_goal(started.task_id, goal="second")
    # Still interactive → suspends again on the next-goal handle.
    assert out.status == "suspended"
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE

    # The woken-command-prelude seam (issue 01): the new turn's
    # MessagesAppended (the appended "second" user goal) lands AFTER the
    # TaskWoken and BEFORE the step's ContextPlanComposed — proving the goal
    # rode run_leased_task's first-consume window, not a second runtime.
    new_events = event_log.read(started.task_id)[len(pre):]
    types = [e.type for e in new_events]
    assert types[0] == "TaskWoken"
    woken_idx = 0
    appended_idx = next(
        i for i, e in enumerate(new_events) if e.type == "MessagesAppended"
    )
    plan_idx = next(
        i for i, e in enumerate(new_events) if e.type == "ContextPlanComposed"
    )
    assert woken_idx < appended_idx < plan_idx


# ---------------------------------------------------------------------------
# activations — web-path parity (deterministic /command skill pin)
# ---------------------------------------------------------------------------


def _activate_patches(events: list[Any]) -> list[list[str]]:
    """All ``activate_skills`` lists carried by ``TaskStatePatched`` events."""
    return [
        e.payload.patch.get("activate_skills")
        for e in events
        if e.type == "TaskStatePatched"
    ]


def test_start_activations_pin_skill_after_goal(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("ok")])
    driver = InteractionDriver(host)

    out = driver.start(
        goal="since main", agent="general-purpose", activations=("review",)
    )
    assert out.status == "suspended"

    events = event_log.read(out.task_id)
    # The deterministic activation rode the seed turn: a TaskStatePatched carrying
    # activate_skills=["review"] is emitted, mirroring the resident-runner pre-loop
    # activate_skills (so the composer pins the body for this turn onward).
    assert ["review"] in _activate_patches(events), [
        e.payload.patch for e in events if e.type == "TaskStatePatched"
    ]
    # Goal-then-patch order: the patch lands AFTER the opening user goal message.
    types = [e.type for e in events]
    assert types.index("MessagesAppended") < types.index("TaskStatePatched")


def test_start_without_activations_emits_no_patch(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("ok")])
    driver = InteractionDriver(host)

    out = driver.start(goal="hello", agent="main")
    # No activations ⇒ byte-identical to the pre-parity path: no operator-side
    # TaskStatePatched (the FakeLLM end-turn emits none either).
    assert _activate_patches(event_log.read(out.task_id)) == []


def test_send_goal_activations_pin_skill_in_woken_window(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws, responses=[_end_turn("t1"), _end_turn("t2")]
    )
    driver = InteractionDriver(host)

    started = driver.start(goal="first", agent="main")
    pre = event_log.read(started.task_id)
    out = driver.send_goal(
        started.task_id, goal="since main", activations=("review",)
    )
    assert out.status == "suspended"

    new_events = event_log.read(started.task_id)[len(pre):]
    # The prelude appended the goal THEN emitted the activation, both inside the
    # post-TaskWoken first-consume window (goal-then-patch order).
    appended_idx = next(
        i for i, e in enumerate(new_events) if e.type == "MessagesAppended"
    )
    patched_idx = next(
        i for i, e in enumerate(new_events)
        if e.type == "TaskStatePatched"
        and e.payload.patch.get("activate_skills") == ["review"]
    )
    assert appended_idx < patched_idx


def test_send_goal_refuses_when_not_next_goal_suspended(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, _ = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(host)
    # A fresh (unknown) task id is not suspended on the next-goal handle.
    with pytest.raises(RuntimeError):
        driver.send_goal("ghost-task", goal="x")


# ---------------------------------------------------------------------------
# approve / deny — gated tool through the resolve-approval prelude
# ---------------------------------------------------------------------------


def test_approve_runs_gated_tool_then_suspends(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws,
        responses=[
            _tool_call("c1", "write", {"path": "a.py", "content": "x\n"}),
            _end_turn("wrote it"),
        ],
        require_approval_tools=("write",),
    )
    driver = InteractionDriver(host)

    started = driver.start(goal="write a.py", agent="main")
    # Gated tool → suspended on the approval handle, file not yet written.
    assert started.status == "suspended"
    assert started.wake_handle == "approval-c1"
    assert not (ws / "a.py").exists()

    out = driver.approve(started.task_id, call_id="c1")
    # Approved write ran; the finishing turn then suspends on next-goal
    # (interactive final=False).
    assert (ws / "a.py").read_text() == "x\n"
    assert out.status == "suspended"
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE
    types = [e.type for e in event_log.read(started.task_id)]
    assert "ToolCallApprovalResolved" in types
    assert "ToolResultRecorded" in types


def test_deny_skips_gated_tool_then_suspends(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws,
        responses=[
            _tool_call("c1", "write", {"path": "a.py", "content": "x\n"}),
            _end_turn("ok, skipped"),
        ],
        require_approval_tools=("write",),
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="write a.py", agent="main")
    assert started.wake_handle == "approval-c1"

    out = driver.deny(started.task_id, call_id="c1", reason="no")
    assert not (ws / "a.py").exists()
    assert out.status == "suspended"
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE


# ---------------------------------------------------------------------------
# cancel — L0 TaskCancelled, no new schema
# ---------------------------------------------------------------------------


def test_cancel_writes_task_cancelled_and_terminates(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(host)
    started = driver.start(goal="x", agent="main")
    assert started.status == "suspended"

    out = driver.cancel(started.task_id, reason="user-cancel")
    assert out.status == "terminal"
    cancelled = [
        e for e in event_log.read(started.task_id) if e.type == "TaskCancelled"
    ]
    assert len(cancelled) == 1
    assert cancelled[0].payload.reason == "user-cancel"
    # fold treats it as terminal.
    task = fold(event_log, host.content_store, started.task_id)
    assert task.status == "terminal"


def test_cancel_refuses_already_terminal(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, _ = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(host)
    started = driver.start(goal="x", agent="main")
    driver.cancel(started.task_id)
    with pytest.raises(RuntimeError):
        driver.cancel(started.task_id)


# ---------------------------------------------------------------------------
# A fail turn still terminates (native semantics preserved)
# ---------------------------------------------------------------------------


def test_fail_turn_still_terminates(tmp_path: Path) -> None:
    """``_multi_turn_policy`` only rewrites a FinishDecision — a failing turn
    (here: max-steps exhaustion → TaskFailed) keeps its native terminal
    semantics."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # A provider that loops a non-finishing tool call forever; with
    # max_steps=1 the ReAct loop fails the turn rather than finishing.
    looping = [_tool_call(f"c{i}", "read_file", {"path": "x.py"}) for i in range(10)]
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=looping),
        model="gpt-test",
        workspace_dir=ws,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.ALLOWLIST,
        max_steps=1,
        policy_wrapper=multi_turn_policy_wrapper,
    
        registry=official_agent_registry(),
        aliases={"default": "main"},
        require_approval_tools=(),)
    driver = InteractionDriver(host)
    out = driver.start(goal="loop", agent="main")
    assert out.status == "terminal"
    types = [e.type for e in event_log.read(out.task_id)]
    assert "TaskFailed" in types
    # A fail turn does NOT suspend on the next-goal handle.
    assert out.wake_handle is None


# ---------------------------------------------------------------------------
# close / reopen — L0 ConversationClosed lifecycle (issue 08)
# ---------------------------------------------------------------------------


def test_close_writes_conversation_closed_and_keeps_suspended(
    tmp_path: Path,
) -> None:
    """``close`` writes the L0 ``ConversationClosed`` event (writer Engine),
    folds ``GovernanceState.closed = True`` for the sessions-list/inspect hot
    path, and — per "No synthesized terminal" — leaves
    ``task.status`` = ``suspended`` (NO manufactured ``TaskCompleted``)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    started = driver.start(goal="hello", agent="main")
    assert started.status == "suspended"

    out = driver.close(started.task_id, closed_by="leo", reason="done for now")

    # Status is UNTOUCHED — closed is orthogonal to status.
    assert out.status == "suspended"
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE
    events = event_log.read(started.task_id)
    closed = [e for e in events if e.type == "ConversationClosed"]
    assert len(closed) == 1
    assert closed[0].payload.closed_by == "leo"
    assert closed[0].payload.reason == "done for now"
    # Writer is the Engine (NOT an Observer / not a policy Decision).
    assert closed[0].origin == "engine"
    # No control-plane terminal manufactured.
    assert "TaskCompleted" not in [e.type for e in events]

    # Queryable by fold (NOT via an Observer): closed=True, status=suspended.
    task = fold(event_log, host.content_store, started.task_id)
    assert task.governance.closed is True
    assert task.governance.closed_by == "leo"
    assert task.governance.close_reason == "done for now"
    assert task.status == "suspended"
    assert task.governance.conversation_lifecycle == [
        {"event": "closed", "by": "leo", "reason": "done for now"}
    ]


def test_close_refuses_already_terminal(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, _ = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(host)
    started = driver.start(goal="x", agent="main")
    driver.cancel(started.task_id)
    with pytest.raises(RuntimeError):
        driver.close(started.task_id)


def test_new_goal_reopens_closed_conversation(tmp_path: Path) -> None:
    """CW2 — close is **advisory**: a new goal on a closed+suspended Task drives
    the next turn AND implicitly reopens it. ``send_goal`` emits
    ``ConversationReopened`` in the suspend window (before ``TaskWoken``), so the
    folded ``closed`` flag clears and the lifecycle audit records the reopen."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws, responses=[_end_turn("turn1"), _end_turn("turn2")]
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    driver.close(started.task_id, closed_by="leo")
    assert (
        fold(event_log, host.content_store, started.task_id).governance.closed
        is True
    )

    out = driver.send_goal(started.task_id, goal="second")
    assert out.status == "suspended"
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE

    types = [e.type for e in event_log.read(started.task_id)]
    # Exactly one reopen, and it sits in the suspend window: BEFORE the
    # second turn's TaskWoken and BEFORE its MessagesAppended (new goal).
    assert types.count("ConversationReopened") == 1
    reopen_idx = types.index("ConversationReopened")
    # the TaskWoken / MessagesAppended of the reopened turn follow the reopen
    woken_after = [
        i for i, t in enumerate(types) if t == "TaskWoken" and i > reopen_idx
    ]
    appended_after = [
        i
        for i, t in enumerate(types)
        if t == "MessagesAppended" and i > reopen_idx
    ]
    assert woken_after, types
    assert appended_after, types
    assert reopen_idx < woken_after[0] < appended_after[0]

    task = fold(event_log, host.content_store, started.task_id)
    assert task.governance.closed is False
    # implicit reopen records the acting principal (CLI ⊤ LOCAL_PRINCIPAL) +
    # the "new goal" reason — the lifecycle audit keeps both close and reopen.
    assert task.governance.conversation_lifecycle == [
        {"event": "closed", "by": "leo", "reason": None},
        {"event": "reopened", "by": LOCAL_PRINCIPAL.identity,
         "reason": "new goal"},
    ]


def test_send_goal_reopens_only_once_across_turns(tmp_path: Path) -> None:
    """A second goal on the (now-open) conversation does NOT re-reopen: exactly
    one ``ConversationReopened`` across two follow-up turns."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws, responses=[_end_turn("t1"), _end_turn("t2"), _end_turn("t3")]
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    driver.close(started.task_id, closed_by="leo")
    driver.send_goal(started.task_id, goal="second")  # reopens
    driver.send_goal(started.task_id, goal="third")  # already open → no reopen
    types = [e.type for e in event_log.read(started.task_id)]
    assert types.count("ConversationReopened") == 1


def test_send_goal_never_closed_emits_no_reopen(tmp_path: Path) -> None:
    """The common path: a new goal on a conversation that was never closed
    writes ZERO ``ConversationReopened`` — so old recordings drift nowhere."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws, responses=[_end_turn("t1"), _end_turn("t2")]
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    driver.send_goal(started.task_id, goal="second")
    types = [e.type for e in event_log.read(started.task_id)]
    assert "ConversationReopened" not in types


def test_send_goal_rejected_selector_writes_nothing_when_closed(
    tmp_path: Path,
) -> None:
    """CW2 — a closed conversation + a new goal carrying an UNAUTHORIZED model
    selector leaves ZERO durable write. The selector is validated BEFORE the
    reopen emission (driver.py send_goal), so the rejection produces no
    ``ConversationReopened`` / no new ``ModelBound`` / no new goal
    ``MessagesAppended`` — the stream is untouched and the conversation stays
    closed. Pins the reopen-before-wake branch's zero-write guarantee (the
    pre-existing selector test only covers ``start``/open)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("t1")])
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    driver.close(started.task_id, closed_by="leo")
    before = [e.type for e in event_log.read(started.task_id)]
    assert before.count("ConversationClosed") == 1

    with pytest.raises(ModelSelectorError):
        driver.send_goal(
            started.task_id, goal="second", model_selector="gpt-9000"
        )

    after = [e.type for e in event_log.read(started.task_id)]
    assert after == before  # no new bytes at all on rejection
    assert "ConversationReopened" not in after
    task = fold(event_log, host.content_store, started.task_id)
    assert task.governance.closed is True


def test_explicit_reopen_is_idempotent_when_open(tmp_path: Path) -> None:
    """CW2 — explicit ``reopen`` on a conversation that is not closed is a
    no-op: it writes no ``ConversationReopened`` (no spurious audit entry)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    started = driver.start(goal="hello", agent="main")
    # never closed → reopen is a no-op
    out = driver.reopen(started.task_id, reopened_by="leo")
    assert out.status == "suspended"
    types = [e.type for e in event_log.read(started.task_id)]
    assert "ConversationReopened" not in types
    # closing then double-reopen records exactly one reopen
    driver.close(started.task_id, closed_by="leo")
    driver.reopen(started.task_id, reopened_by="leo")  # closed → emits
    driver.reopen(started.task_id, reopened_by="leo")  # open → no-op
    types = [e.type for e in event_log.read(started.task_id)]
    assert types.count("ConversationReopened") == 1


def test_explicit_reopen_clears_closed_flag(tmp_path: Path) -> None:
    """``reopen`` writes the audit-symmetric ``ConversationReopened``, folding
    ``closed = False`` without touching ``task.status``."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    started = driver.start(goal="hello", agent="main")
    driver.close(started.task_id, closed_by="leo", reason="bye")

    out = driver.reopen(started.task_id, reopened_by="leo", reason="back")
    assert out.status == "suspended"
    events = event_log.read(started.task_id)
    reopened = [e for e in events if e.type == "ConversationReopened"]
    assert len(reopened) == 1
    assert reopened[0].payload.reopened_by == "leo"
    assert reopened[0].origin == "engine"

    task = fold(event_log, host.content_store, started.task_id)
    assert task.governance.closed is False
    assert task.governance.closed_by is None
    assert task.governance.close_reason is None
    # The full lifecycle audit trail (close THEN reopen).
    assert task.governance.conversation_lifecycle == [
        {"event": "closed", "by": "leo", "reason": "bye"},
        {"event": "reopened", "by": "leo", "reason": "back"},
    ]


def test_close_then_reopen_then_close_again(tmp_path: Path) -> None:
    """The flag is the latest fold of the lifecycle stream — close→reopen→close
    ends ``closed = True`` (the audit list keeps all three)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    started = driver.start(goal="hello", agent="main")
    driver.close(started.task_id, closed_by="a")
    driver.reopen(started.task_id, reopened_by="b")
    driver.close(started.task_id, closed_by="c", reason="final")

    task = fold(event_log, host.content_store, started.task_id)
    assert task.governance.closed is True
    assert task.governance.closed_by == "c"
    assert task.governance.close_reason == "final"
    assert task.status == "suspended"
    assert len(task.governance.conversation_lifecycle) == 3


# ---------------------------------------------------------------------------
# rewind (conversation half): TaskRewound marker re-bases fold
# ---------------------------------------------------------------------------


def _user_goal_seqs(event_log: Any, content_store: Any, task_id: str) -> list[int]:
    """Seqs of the user-goal MessagesAppended events (one per turn's goal).

    Role lives in the dereferenced message body, not the envelope, so we deref
    each ``MessagesAppended`` and keep the ones whose first message is a user
    turn (assistant / tool appends share the type but are not rewind anchors)."""
    seqs: list[int] = []
    for e in event_log.read(task_id):
        if e.type != "MessagesAppended":
            continue
        msgs = messages_from_appended(e, content_store)
        if msgs and msgs[0].role == "user":
            seqs.append(e.seq)
    return seqs


def _text_of(message: Any) -> str:
    """Flatten a folded Message's text blocks into one string."""
    return " ".join(
        getattr(block, "text", "") for block in getattr(message, "content", [])
    )


def test_rewind_appends_marker_without_touching_old_events(
    tmp_path: Path,
) -> None:
    """append-only: rewind only APPENDS a TaskRewound; every prior event is
    byte-identical to before the rewind."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws, responses=[_end_turn("turn1"), _end_turn("turn2")]
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    driver.send_goal(started.task_id, goal="second")

    before = list(event_log.read(started.task_id))
    last_seq = before[-1].seq
    # Rewind the SECOND user goal: the conversation rests where turn-1 settled.
    second_goal_seq = _user_goal_seqs(event_log, host.content_store, started.task_id)[1]
    driver.rewind(started.task_id, message_seq=second_goal_seq)

    after = list(event_log.read(started.task_id))
    # Nothing rewritten / deleted: the whole pre-rewind prefix is identical.
    assert after[: len(before)] == before
    # Exactly one new event, a TaskRewound naming the kept-through boundary.
    assert len(after) == len(before) + 1
    marker = after[-1]
    assert marker.type == "TaskRewound"
    # Kept through the turn-1 next-goal suspend (just before the TaskWoken that
    # consumed the second goal).
    assert marker.payload.target_seq < second_goal_seq
    assert marker.seq == last_seq + 1


def test_rewind_folds_state_back_to_target_seq(tmp_path: Path) -> None:
    """fold of the rewound task == fold of the stream truncated at target_seq:
    the second turn's user/assistant messages are gone, turn-1 survives."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws, responses=[_end_turn("turn1"), _end_turn("turn2")]
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    driver.send_goal(started.task_id, goal="second")

    second_goal_seq = _user_goal_seqs(event_log, host.content_store, started.task_id)[1]
    # Sanity: the live (pre-rewind) fold DOES carry the second goal.
    live_msgs = [
        _text_of(m)
        for m in fold(event_log, host.content_store, started.task_id).runtime.messages
    ]
    assert any("second" in m for m in live_msgs)

    driver.rewind(started.task_id, message_seq=second_goal_seq)
    rewound = fold(event_log, host.content_store, started.task_id)
    rewound_msgs = [_text_of(m) for m in rewound.runtime.messages]
    assert any("first" in m for m in rewound_msgs)
    assert not any("second" in m for m in rewound_msgs)
    # Re-based to a clean turn boundary: suspended on next-goal, live again.
    assert rewound.status == "suspended"


def test_rewind_then_send_goal_continues_conversation(tmp_path: Path) -> None:
    """After a rewind the conversation is live again: the next send_goal drives
    a fresh turn whose history starts from the rewound baseline (the abandoned
    second turn never reappears)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws,
        responses=[_end_turn("turn1"), _end_turn("turn2"), _end_turn("turn3")],
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    driver.send_goal(started.task_id, goal="second")

    second_goal_seq = _user_goal_seqs(event_log, host.content_store, started.task_id)[1]
    driver.rewind(started.task_id, message_seq=second_goal_seq)

    out = driver.send_goal(started.task_id, goal="third")
    assert out.status == "suspended"
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE
    task = fold(event_log, host.content_store, started.task_id)
    texts = [_text_of(m) for m in task.runtime.messages]
    assert any("first" in t for t in texts)
    assert any("third" in t for t in texts)
    # The abandoned second turn is dead history — not in the live fold.
    assert not any("second" in t for t in texts)


def test_rewind_after_cancel_repairs_dispatcher_for_next_goal(
    tmp_path: Path,
) -> None:
    """Running-turn rewind first writes TaskCancelled to stop the old turn.

    The TaskRewound marker re-bases fold to the prior suspended boundary; the
    dispatcher must be repaired to that same boundary or the next send_goal sees
    a folded-suspended task backed by a dispatcher-terminal row.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    host, dispatcher, event_log = _host(
        ws,
        responses=[_end_turn("turn1"), _end_turn("turn2"), _end_turn("turn3")],
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    driver.send_goal(started.task_id, goal="second")

    second_goal_seq = _user_goal_seqs(event_log, host.content_store, started.task_id)[1]
    driver.cancel(
        started.task_id,
        reason="rewind: stop in-flight turn",
        cascade=True,
    )
    dispatcher.restore_task(
        started.task_id,
        status="terminal",
        suspend_reason="cancelled",
    )
    assert dispatcher.task_status(started.task_id) == "terminal"

    driver.rewind(started.task_id, message_seq=second_goal_seq)
    assert fold(event_log, host.content_store, started.task_id).status == "suspended"
    assert dispatcher.task_status(started.task_id) == "suspended"

    out = driver.send_goal(started.task_id, goal="third")
    assert out.status == "suspended"
    assert out.wake_handle == NEXT_GOAL_WAKE_HANDLE


def test_send_goal_repairs_stale_dispatcher_mismatch_before_resuming(
    tmp_path: Path,
) -> None:
    """A stale pre-fix session can fold as suspended while dispatcher is terminal.

    The driver should rebuild the dispatcher row from the folded EventLog state
    and continue, instead of surfacing the low-level wake/lease mismatch.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    host, dispatcher, _event_log = _host(
        ws, responses=[_end_turn("turn1"), _end_turn("turn2")]
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    assert started.status == "suspended"
    dispatcher.restore_task(
        started.task_id,
        status="terminal",
        suspend_reason="cancelled",
    )

    out = driver.send_goal(started.task_id, goal="second")
    assert out.status == "suspended"
    assert dispatcher.task_status(started.task_id) == "suspended"


def test_send_goal_repair_ignores_stale_pending_wake(
    tmp_path: Path,
) -> None:
    """A previous failed resume may have buffered a next-goal wake.

    Rebuilding the dispatcher row should discard that stale buffered wake and
    consume only the wake for the command currently being seeded.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    host, dispatcher, _event_log = _host(
        ws, responses=[_end_turn("turn1"), _end_turn("turn2")]
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    dispatcher.restore_task(
        started.task_id,
        status="terminal",
        suspend_reason="cancelled",
    )
    assert dispatcher.wake(
        started.task_id,
        HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE),
    ) is False

    out = driver.send_goal(started.task_id, goal="second")
    assert out.status == "suspended"
    assert dispatcher.task_status(started.task_id) == "suspended"
    assert dispatcher.lease(worker_id="probe", task_id=started.task_id) is None


def test_rewind_rejects_non_user_message_target(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    started = driver.start(goal="hello", agent="main")
    events = event_log.read(started.task_id)
    # A seq that is not a user-message event (e.g. the genesis TaskCreated).
    created_seq = next(e.seq for e in events if e.type == "TaskCreated")
    with pytest.raises(RuntimeError):
        driver.rewind(started.task_id, message_seq=created_seq)
    # Past the end of the stream — no such event.
    with pytest.raises(RuntimeError):
        driver.rewind(started.task_id, message_seq=events[-1].seq + 5)


def test_rewind_first_user_message_rebases_to_empty(tmp_path: Path) -> None:
    """Rewinding the OPENING user message re-bases to the genesis header — the
    conversation has no surviving turns (the opening goal itself is dropped)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws, responses=[_end_turn("turn1"), _end_turn("turn2")]
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    first_goal_seq = _user_goal_seqs(event_log, host.content_store, started.task_id)[0]
    driver.rewind(started.task_id, message_seq=first_goal_seq)
    rewound = fold(event_log, host.content_store, started.task_id)
    assert [_text_of(m) for m in rewound.runtime.messages] == []
