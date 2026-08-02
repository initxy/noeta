"""Mid-turn goal injection — deliver a user message while a turn is running.

The engine seam: at each top-of-loop boundary ``run_one_step`` drains pending
injections (union of the process-local ``InjectionInbox`` and the durable
``governance.pending_injections`` folded from the log) and delivers each as a
real ``MessagesAppended`` carrying ``consumes_injection=id``. Fold appends the
message and pops the pending marker in one reduction, so delivery is
exactly-once and crash-safe, and an injected ``user`` message can never split a
``tool_use`` / ``tool_result`` pair because the drain runs only at the boundary
where the prior iteration's tool results are already appended.

The driver verb ``inject_goal`` is status-dispatched (running → durable
``InjectionRequested`` + inbox poke; next-goal suspended → ``send_goal``; else
``NotResumableError``); ``tests/test_client_inject_goal.py`` covers that surface.
This file proves the engine-level guarantees on the raw ``run_one_step`` loop.
"""

from __future__ import annotations

from typing import Any

from noeta.core._decision_handlers import put_messages
from noeta.core.engine import Engine
from noeta.core.fold import fold, messages_from_appended
from noeta.core.snapshot import serialize_task_state
from noeta.core.wiring import wire_default_observers
from noeta.protocols.decisions import (
    Decision,
    FinishDecision,
    ToolCall,
    ToolCallsDecision,
    YieldForHumanDecision,
)
from noeta.protocols.events import InjectionRequestedPayload
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View
from noeta.protocols.wake import HumanResponseReceived, NEXT_GOAL_WAKE_HANDLE
from noeta.runtime.injection import InjectionInbox
from noeta.runtime.tool import ToolRuntime
from noeta.runtime.worker import run_leased_task
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.tools.fake import FakeTool

import pytest


def _put_injection(content_store: InMemoryContentStore, text: str) -> dict[str, Any]:
    """Spill a one-message injection to the ContentStore and return the
    ``{messages_ref, count}`` descriptor the inbox / event carries."""
    payload = put_messages(
        content_store, [Message(role="user", content=[TextBlock(text=text)])]
    )
    return {"messages_ref": payload.messages_ref, "count": payload.count}


def test_injection_inbox_unit_semantics() -> None:
    """The process-local inbox: submit / snapshot / consume / discard, plus the
    two edge branches (consume on an unknown task, consume leaving a non-empty
    bucket)."""
    inbox = InjectionInbox()
    assert inbox.snapshot("t") == {}
    # consume on an unknown task is a no-op (early return).
    inbox.consume("t", "nope")

    inbox.submit("t", "a", {"messages_ref": "ra", "count": 1})
    inbox.submit("t", "b", {"messages_ref": "rb", "count": 2})
    snap = inbox.snapshot("t")
    assert list(snap) == ["a", "b"]  # arrival order preserved
    # snapshot is a copy — mutating it does not touch the inbox.
    snap["a"]["count"] = 99
    assert inbox.snapshot("t")["a"]["count"] == 1

    # consume one, the other bucket entry survives (bucket stays non-empty).
    inbox.consume("t", "a")
    assert list(inbox.snapshot("t")) == ["b"]
    # consume the last, the task bucket is dropped.
    inbox.consume("t", "b")
    assert inbox.snapshot("t") == {}

    # discard drops everything for a task (idempotent).
    inbox.submit("u", "c", {"messages_ref": "rc", "count": 1})
    inbox.discard("u")
    inbox.discard("u")
    assert inbox.snapshot("u") == {}


def _user_texts(messages: list[Message]) -> list[str]:
    return [
        b.text
        for m in messages
        if m.role == "user"
        for b in m.content
        if isinstance(b, TextBlock)
    ]


def _ledger_messages(log, cs, task_id) -> list[Message]:
    out: list[Message] = []
    for env in log.read(task_id):
        if env.type == "MessagesAppended":
            out.extend(messages_from_appended(env, cs))
    return out


def _build(policy, *, inbox=None, clock=None):
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    tool = FakeTool(name="noop", script={(): "ok"})
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=policy,
        tools={"noop": tool},
        tool_runtime=ToolRuntime(event_log=log, content_store=cs),
        injection_inbox=inbox,
        clock=clock,
    )
    task = engine.create_task(goal="original goal", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None
    return engine, log, cs, lease.lease_id, task


# ---------------------------------------------------------------------------
# 1. Sees it same-turn
# ---------------------------------------------------------------------------


class _InjectThenObservePolicy:
    """Round 0: submit an injection to the inbox (stand-in for the HTTP thread)
    and keep the loop turning with a tool call. Round 1: record the user
    messages now visible in the raw rolling history — the injected one must be
    there — and finish."""

    def __init__(self, inbox: InjectionInbox, text: str) -> None:
        self._inbox = inbox
        self._text = text
        self._i = 0
        self.seen_at_finish: list[str] = []
        self._descriptor: dict[str, Any] | None = None

    def arm(self, descriptor: dict[str, Any]) -> None:
        self._descriptor = descriptor

    def decide(self, ctx: StepContext, view: View) -> Decision:
        if self._i == 0:
            self._i += 1
            assert self._descriptor is not None
            self._inbox.submit(ctx.task_id, "inj-1", self._descriptor)
            return ToolCallsDecision(
                calls=[ToolCall(tool_name="noop", arguments={}, call_id="c1")]
            )
        self.seen_at_finish = _user_texts(view.rolling_history)
        return FinishDecision(answer="done")


def test_injection_seen_within_same_turn() -> None:
    inbox = InjectionInbox()
    policy = _InjectThenObservePolicy(inbox, "injected mid-turn")
    engine, log, cs, lease_id, task = _build(policy, inbox=inbox)
    policy.arm(_put_injection(cs, "injected mid-turn"))

    finished = engine.run_one_step(task, lease_id=lease_id)
    assert finished.status == "terminal"

    # The policy saw the injected message on its second decide — same turn, no
    # intervening suspend/wake.
    assert "injected mid-turn" in policy.seen_at_finish
    types = [e.type for e in log.read(task.task_id)]
    assert "TaskSuspended" not in types and "TaskWoken" not in types
    # Exactly one consuming MessagesAppended carried the injection id.
    consume = [
        e
        for e in log.read(task.task_id)
        if e.type == "MessagesAppended"
        and getattr(e.payload, "consumes_injection", None) == "inj-1"
    ]
    assert len(consume) == 1
    assert inbox.snapshot(task.task_id) == {}


# ---------------------------------------------------------------------------
# 2. Exactly-once across a from-scratch refold (the resume path)
# ---------------------------------------------------------------------------


class _FinishNow:
    def decide(self, ctx: StepContext, view: View) -> Decision:  # noqa: ARG002
        return FinishDecision(answer="done")


def test_injection_consumed_exactly_once_under_refold() -> None:
    """Pre-seed a durable ``InjectionRequested`` (as ``inject_goal`` would on a
    running task), then drive one step: the drain delivers it, the consume marker
    pops it, and a from-scratch fold (resume) reproduces identical state without
    re-delivering. The two fold paths are byte-equal."""
    inbox = InjectionInbox()
    engine, log, cs, lease_id, task = _build(
        _FinishNow(), inbox=inbox, clock=lambda: 1_000.0
    )
    descriptor = _put_injection(cs, "hello injected")
    log.system_emit(
        task_id=task.task_id,
        type="InjectionRequested",
        payload=InjectionRequestedPayload(
            injection_id="inj-42",
            messages_ref=descriptor["messages_ref"],
            count=descriptor["count"],
        ),
        actor="test",
        origin="system",
    )
    inbox.submit(task.task_id, "inj-42", descriptor)

    finished = engine.run_one_step(task, lease_id=lease_id)
    assert finished.status == "terminal"

    accelerated = fold(log, cs, task.task_id, ignore_snapshots=False)
    scratch = fold(log, cs, task.task_id, ignore_snapshots=True)
    assert accelerated == scratch
    assert serialize_task_state(accelerated) == serialize_task_state(scratch)

    injected = [t for t in _user_texts(scratch.runtime.messages) if t == "hello injected"]
    assert len(injected) == 1
    assert scratch.governance.pending_injections == {}


def test_pending_injection_survives_a_fold_when_undelivered() -> None:
    """An ``InjectionRequested`` with no consuming append yet folds into
    ``pending_injections`` — the durable anchor a resumed turn's drain reads."""
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    engine = Engine(
        event_log=log, content_store=cs, composer=trivial_three_segment(cs),
        policy=_FinishNow(),
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    descriptor = _put_injection(cs, "queued")
    log.system_emit(
        task_id=task.task_id,
        type="InjectionRequested",
        payload=InjectionRequestedPayload(
            injection_id="inj-q",
            messages_ref=descriptor["messages_ref"],
            count=descriptor["count"],
        ),
        actor="test",
        origin="system",
    )
    folded = fold(log, cs, task.task_id)
    assert "inj-q" in folded.governance.pending_injections
    assert folded.governance.pending_injections["inj-q"]["count"] == 1


# ---------------------------------------------------------------------------
# 3. No ordering corruption — never between tool_use and tool_result
# ---------------------------------------------------------------------------


class _ToolThenInjectPolicy:
    """Round 0: inject WHILE a tool call is outstanding, then keep the loop
    turning. Round 1: finish."""

    def __init__(self, inbox: InjectionInbox, text: str) -> None:
        self._inbox = inbox
        self._text = text
        self._i = 0
        self._descriptor: dict[str, Any] | None = None

    def arm(self, descriptor: dict[str, Any]) -> None:
        self._descriptor = descriptor

    def decide(self, ctx: StepContext, view: View) -> Decision:  # noqa: ARG002
        if self._i == 0:
            self._i += 1
            assert self._descriptor is not None
            self._inbox.submit(ctx.task_id, "inj-mid", self._descriptor)
            return ToolCallsDecision(
                calls=[ToolCall(tool_name="noop", arguments={}, call_id="c1")]
            )
        return FinishDecision(answer="done")


def test_injection_never_splits_tool_use_result_pair() -> None:
    inbox = InjectionInbox()
    policy = _ToolThenInjectPolicy(inbox, "mid tool round")
    engine, log, cs, lease_id, task = _build(policy, inbox=inbox)
    policy.arm(_put_injection(cs, "mid tool round"))

    engine.run_one_step(task, lease_id=lease_id)

    # The injected user message must land AFTER the tool result that answers the
    # outstanding call, never between the assistant tool_use and that result.
    msgs = _ledger_messages(log, cs, task.task_id)
    roles = [m.role for m in msgs]
    assert "tool" in roles
    tool_idx = roles.index("tool")
    injected_idx = next(
        i
        for i, m in enumerate(msgs)
        if m.role == "user"
        and any(isinstance(b, TextBlock) and b.text == "mid tool round" for b in m.content)
    )
    assert injected_idx > tool_idx


# ---------------------------------------------------------------------------
# 4. Nothing pending ⇒ byte-identical to a turn with no inbox
# ---------------------------------------------------------------------------


def test_no_injection_is_byte_identical() -> None:
    engine_a, log_a, cs_a, lease_a, task_a = _build(
        _FinishNow(), inbox=InjectionInbox(), clock=lambda: 1_000.0
    )
    engine_b, log_b, cs_b, lease_b, task_b = _build(
        _FinishNow(), inbox=None, clock=lambda: 1_000.0
    )
    engine_a.run_one_step(task_a, lease_id=lease_a)
    engine_b.run_one_step(task_b, lease_id=lease_b)

    assert [e.type for e in log_a.read(task_a.task_id)] == [
        e.type for e in log_b.read(task_b.task_id)
    ]
    # No consume-marker MessagesAppended was written on the empty-inbox turn.
    assert not [
        e
        for e in log_a.read(task_a.task_id)
        if getattr(e.payload, "consumes_injection", None) is not None
    ]


# ---------------------------------------------------------------------------
# 5. An injection arriving mid-crashed-attempt survives the seal (recovery)
# ---------------------------------------------------------------------------


def test_injection_mid_attempt_survives_seal_and_redrive() -> None:
    """An ``InjectionRequested`` written DURING an attempt that then crashes
    (seq >= attempt_start_seq) would be re-based into dead history by the seal's
    pre-attempt baseline. The recovery carries the full-stream
    ``pending_injections`` onto the sealed baseline, so the re-driven turn still
    delivers the injected message exactly once."""
    clock = [1_000.0]
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher(now=lambda: clock[0])
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    descriptor = _put_injection(cs, "arrived mid-crash")

    class _EmitInjectionThenCrash:
        """Opening turn parks on the next-goal handle; the second turn's decide
        emits an ``InjectionRequested`` (mid-attempt, after this attempt's
        ContextPlanComposed) then raises — the simulated crash. The re-driven
        decide finishes."""

        def __init__(self) -> None:
            self._i = 0

        def decide(self, ctx: StepContext, view: View) -> Decision:  # noqa: ARG002
            self._i += 1
            if self._i == 1:
                return YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE)
            if self._i == 2:
                log.system_emit(
                    task_id=ctx.task_id,
                    type="InjectionRequested",
                    payload=InjectionRequestedPayload(
                        injection_id="inj-crash",
                        messages_ref=descriptor["messages_ref"],
                        count=descriptor["count"],
                    ),
                    actor="test",
                    origin="system",
                )
                raise RuntimeError("crash after injection landed mid-attempt")
            return FinishDecision(answer="done")

    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=_EmitInjectionThenCrash(),
    )

    class _RT:
        def __init__(self, engine, log, cs, disp) -> None:
            self.engine = engine
            self.event_log = log
            self.content_store = cs
            self.dispatcher = disp

    # Opening turn → next-goal suspend.
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    engine.append_user_message(
        task, content=[TextBlock(text="g")], lease_id=lease.lease_id
    )
    task = engine.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "suspended"
    disp.release(lease.lease_id, next_state="suspended", wake_on=task.wake_on)

    # Second turn: wake, lease, drive until the mid-attempt crash.
    assert disp.wake(
        task.task_id, HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
    ) is True
    lease = disp.lease(worker_id="w", task_id=task.task_id)
    task = fold(log, cs, task.task_id)
    task = engine.note_woken(
        task, lease_id=lease.lease_id, wake_event=lease.wake_event
    )
    task = engine.append_user_message(
        task, content=[TextBlock(text="turn 2")], lease_id=lease.lease_id
    )
    with pytest.raises(RuntimeError):
        engine.run_one_step(task, lease_id=lease.lease_id)

    # The injection is durably pending (folds from the log despite the crash).
    assert "inj-crash" in fold(log, cs, task.task_id).governance.pending_injections

    # Reclaim the stale lease and let recovery seal + re-drive.
    clock[0] += 100_000.0
    assert task.task_id in disp.requeue_stale()
    lease = disp.lease(worker_id="w", task_id=task.task_id)
    run_leased_task(_RT(engine, log, cs, disp), lease)

    # The re-driven turn delivered the injection exactly once, despite the seal
    # re-basing to the pre-attempt baseline.
    final = fold(log, cs, task.task_id)
    injected = [t for t in _user_texts(final.runtime.messages) if t == "arrived mid-crash"]
    assert len(injected) == 1
    assert final.governance.pending_injections == {}
