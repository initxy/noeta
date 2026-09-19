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
    pre-attempt baseline. Recovery re-queues the dead window's markers onto that
    baseline, so the re-driven turn still delivers the injected message exactly
    once."""
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


# ---------------------------------------------------------------------------
# 6. A crash between the drain's consuming append and the next plan
# ---------------------------------------------------------------------------
#
# The drain runs at top-of-loop and the attempt anchor is the LAST
# ``ContextPlanComposed``, so a crash in the gap between them leaves the
# consuming ``MessagesAppended`` *behind* the anchor — inside the window the
# seal abandons. The message is then dead history and its marker was already
# popped, so without the re-queue the user's message survives in neither place.


_INJECTED_TEXT = "sent while the turn was running"
_INJECTION_ID = "inj-window"


class _CrashOnNthCompose:
    """Composer wrapper that raises on the ``n``-th ``compose`` — the simulated
    crash, placed so the interrupted attempt's plan is never written."""

    def __init__(self, inner: Any, nth: int) -> None:
        self._inner = inner
        self._nth = nth
        self._calls = 0

    def compose(self, task: Any) -> View:
        self._calls += 1
        if self._calls == self._nth:
            raise RuntimeError("crash before the next ContextPlanComposed")
        return self._inner.compose(task)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RecoveryRuntime:
    """The narrow ``WorkerRuntime`` shape ``run_leased_task`` reads."""

    def __init__(self, engine: Any, log: Any, cs: Any, disp: Any) -> None:
        self.engine = engine
        self.event_log = log
        self.content_store = cs
        self.dispatcher = disp


class _RequestInjectionThenCrash:
    """Turn 1 parks on the next-goal handle. The second turn's first round
    requests a mid-turn injection (stand-in for the HTTP thread's
    ``inject_goal``: durable marker + inbox poke, no lease) and keeps the loop
    turning with a tool call, so the drain delivers it at the NEXT top-of-loop.
    The crash then lands either in that round's ``compose`` (wired by the
    caller) or in its ``decide``. Whatever runs afterwards records the user
    messages the model can actually see."""

    def __init__(
        self, log: Any, inbox: InjectionInbox, descriptor: dict[str, Any],
        *, crash_in_decide: bool,
    ) -> None:
        self._log = log
        self._inbox = inbox
        self._descriptor = descriptor
        self._crash_in_decide = crash_in_decide
        self._i = 0
        self.seen_at_finish: list[str] = []

    def decide(self, ctx: StepContext, view: View) -> Decision:
        self._i += 1
        if self._i == 1:
            return YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE)
        if self._i == 2:
            self._log.system_emit(
                task_id=ctx.task_id,
                type="InjectionRequested",
                payload=InjectionRequestedPayload(
                    injection_id=_INJECTION_ID,
                    messages_ref=self._descriptor["messages_ref"],
                    count=self._descriptor["count"],
                ),
                actor="test",
                origin="system",
            )
            self._inbox.submit(ctx.task_id, _INJECTION_ID, self._descriptor)
            return ToolCallsDecision(
                calls=[ToolCall(tool_name="noop", arguments={}, call_id="c1")]
            )
        if self._i == 3 and self._crash_in_decide:
            raise RuntimeError("crash after this attempt's ContextPlanComposed")
        self.seen_at_finish = _user_texts(view.rolling_history)
        return FinishDecision(answer="done")


def _drive_until_crash(
    *, crash_in: str
) -> tuple[_RecoveryRuntime, list[float], str, _RequestInjectionThenCrash]:
    """Drive the scenario above up to the crash and return what recovery needs.

    ``crash_in="compose"`` puts the consuming append INSIDE the window the seal
    abandons; ``crash_in="decide"`` lets that round's plan land first, so the
    consume stays behind the anchor as live history (the unchanged case)."""
    clock = [1_000.0]
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher(now=lambda: clock[0])
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    inbox = InjectionInbox()
    policy = _RequestInjectionThenCrash(
        log,
        inbox,
        _put_injection(cs, _INJECTED_TEXT),
        crash_in_decide=crash_in == "decide",
    )
    composer: Any = trivial_three_segment(cs)
    if crash_in == "compose":
        # 1 = turn 1, 2 = turn 2 round 1, 3 = turn 2 round 2 (after its drain).
        composer = _CrashOnNthCompose(composer, 3)
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=composer,
        policy=policy,
        tools={"noop": FakeTool(name="noop", script={(): "ok"})},
        tool_runtime=ToolRuntime(event_log=log, content_store=cs),
        injection_inbox=inbox,
    )

    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    engine.append_user_message(
        task, content=[TextBlock(text="g")], lease_id=lease.lease_id
    )
    task = engine.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "suspended"
    disp.release(lease.lease_id, next_state="suspended", wake_on=task.wake_on)

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

    # The crash killed the process, so a restarted worker's inbox is empty:
    # everything below has to come from the EventLog alone.
    inbox.discard(task.task_id)
    return _RecoveryRuntime(engine, log, cs, disp), clock, task.task_id, policy


def _recover(rt: _RecoveryRuntime, clock: list[float], task_id: str) -> None:
    """Reclaim the stale lease and let the worker seal + re-drive."""
    clock[0] += 100_000.0
    assert task_id in rt.dispatcher.requeue_stale()
    lease = rt.dispatcher.lease(worker_id="w2", task_id=task_id)
    run_leased_task(rt, lease)


def _consume_seqs(log: Any, task_id: str) -> list[int]:
    return [
        e.seq
        for e in log.read(task_id)
        if getattr(e.payload, "consumes_injection", None) == _INJECTION_ID
    ]


def _abandoned_from_seq(log: Any, task_id: str) -> int:
    seals = [e for e in log.read(task_id) if e.type == "StepAttemptAbandoned"]
    assert len(seals) == 1
    return seals[0].payload.abandoned_from_seq


def test_injection_consumed_inside_the_sealed_window_is_re_delivered() -> None:
    """The crash falls between the drain's consuming ``MessagesAppended`` and
    the next ``ContextPlanComposed``, so the seal abandons that consume with the
    rest of the attempt. Recovery must re-queue the marker: the re-driven turn
    sees the user's message exactly once — neither lost nor duplicated."""
    rt, clock, task_id, policy = _drive_until_crash(crash_in="compose")
    log, cs = rt.event_log, rt.content_store

    # The trap, before recovery: the marker is popped AND the message is about
    # to be folded away with the dead window.
    crashed = fold(log, cs, task_id)
    assert crashed.governance.pending_injections == {}
    assert _user_texts(crashed.runtime.messages).count(_INJECTED_TEXT) == 1

    _recover(rt, clock, task_id)

    # The consume really was inside the abandoned window — the defect's
    # precondition, not an incidental detail of the scenario.
    assert _consume_seqs(log, task_id)[0] >= _abandoned_from_seq(log, task_id)

    final = fold(log, cs, task_id)
    assert final.status == "terminal"
    # The end state the user cares about: the re-driven turn saw the message,
    # exactly once, and nothing is left pending.
    assert policy.seen_at_finish.count(_INJECTED_TEXT) == 1
    assert _user_texts(final.runtime.messages).count(_INJECTED_TEXT) == 1
    assert final.governance.pending_injections == {}
    # Two consuming appends sit on the stream — the dead one the seal folded
    # over and the re-drive's — but only one survives the fold.
    assert len(_consume_seqs(log, task_id)) == 2


def test_injection_consumed_before_the_sealed_window_is_untouched() -> None:
    """The same crash one event later: this round's ``ContextPlanComposed``
    landed, so it is the attempt anchor and the consume sits behind it as live
    history. The baseline already carries the message and the popped marker —
    recovery must re-queue nothing, and the stream keeps its single consuming
    append."""
    rt, clock, task_id, policy = _drive_until_crash(crash_in="decide")
    log, cs = rt.event_log, rt.content_store
    _recover(rt, clock, task_id)

    consumes = _consume_seqs(log, task_id)
    assert len(consumes) == 1
    assert consumes[0] < _abandoned_from_seq(log, task_id)  # outside the window

    final = fold(log, cs, task_id)
    assert final.status == "terminal"
    assert policy.seen_at_finish.count(_INJECTED_TEXT) == 1
    assert _user_texts(final.runtime.messages).count(_INJECTED_TEXT) == 1
    assert final.governance.pending_injections == {}


def test_recovered_injection_stream_refolds_identically() -> None:
    """Restart safety: once recovery has re-queued and re-delivered the
    injection, the snapshot-accelerated fold and the from-scratch fold rebuild
    the same state byte for byte — a later restart cannot drift or re-deliver."""
    rt, clock, task_id, _ = _drive_until_crash(crash_in="compose")
    _recover(rt, clock, task_id)

    accelerated = fold(
        rt.event_log, rt.content_store, task_id, ignore_snapshots=False
    )
    scratch = fold(rt.event_log, rt.content_store, task_id, ignore_snapshots=True)
    assert accelerated == scratch
    assert serialize_task_state(accelerated) == serialize_task_state(scratch)
    assert _user_texts(scratch.runtime.messages).count(_INJECTED_TEXT) == 1
    assert scratch.governance.pending_injections == {}
