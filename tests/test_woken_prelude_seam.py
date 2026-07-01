"""The woken-command-prelude seam on ``run_leased_task``.

``run_leased_task`` is the single drive primitive for every surface (daemon
worker loop, ``noeta resume``, the in-process ``AgentSessionRunner``). Its
woken branch is ``note_woken → run_one_step``; the real product commands
inject a step **between** the two — ``send_goal`` appends the new turn's
user message, an approval resolution runs/denies the pending tool call.

These tests pin the seam's three states directly on the primitive:

* ``None``                    — daemon worker-loop plain woken branch:
                                no extra step between note_woken and run.
* ``AppendMessagePrelude``    — ``send_goal``: append_user_message lands
                                AFTER TaskWoken, BEFORE the step.
* ``ResolveApprovalPrelude``  — approval: resolve_tool_approval runs in the
                                same window.

Each asserts ordering (the prelude's events sit between ``TaskWoken`` and
the step's events) and the H2 ``consumed_wake_event`` release discipline
(exactly one ``TaskWoken``; the wake is consumed, not re-delivered).
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.protocols.decisions import FinishDecision, YieldForHumanDecision
from noeta.protocols.messages import TextBlock
from noeta.protocols.wake import HumanResponseReceived
from noeta.policies.stub import StubScriptedPolicy
from noeta.runtime.worker import (
    AnswerUserQuestionPrelude,
    AppendMessagePrelude,
    ResolveApprovalPrelude,
    WokenPrelude,
    run_leased_task,
)
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment


# ---------------------------------------------------------------------------
# Real-engine harness — drive a task to a YieldForHuman suspend, then wake it
# ---------------------------------------------------------------------------


class _RT:
    """Concrete `WorkerRuntime` (the Protocol cannot be instantiated)."""

    def __init__(self, engine: Any, log: Any, cs: Any, dispatcher: Any) -> None:
        self.engine = engine
        self.event_log = log
        self.content_store = cs
        self.dispatcher = dispatcher


def _stack() -> tuple[Any, Any, Any]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    wire_default_observers(event_log, dispatcher)
    return event_log, content_store, dispatcher


def _suspended_engine(
    decisions: list[Any],
) -> tuple[Any, Any, Any, Any, str, str]:
    """Build an engine, drive to the first YieldForHuman suspend, release.
    Returns (engine, event_log, content_store, dispatcher, task_id, handle)."""
    event_log, content_store, dispatcher = _stack()
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=StubScriptedPolicy(decisions),
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    engine.append_user_message(task, content=[TextBlock(text="g")], lease_id=lease.lease_id)
    task = engine.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "suspended"
    dispatcher.release(
        lease.lease_id, next_state="suspended", wake_on=task.wake_on
    )
    return engine, event_log, content_store, dispatcher, task.task_id, task.wake_on.handle


def _wake_and_lease(dispatcher: Any, tid: str, handle: str) -> Any:
    assert dispatcher.wake(tid, HumanResponseReceived(handle=handle)) is True
    lease = dispatcher.lease(worker_id="w", task_id=tid)
    assert lease is not None and lease.wake_event is not None
    return lease


def _types(log: Any, tid: str) -> list[str]:
    return [e.type for e in log.read(tid)]


# ---------------------------------------------------------------------------
# State 1 — None: plain daemon woken branch (no prelude step)
# ---------------------------------------------------------------------------


def test_prelude_none_no_step_between_woken_and_run() -> None:
    engine, log, cs, dispatcher, tid, handle = _suspended_engine(
        [YieldForHumanDecision(prompt="a"), FinishDecision(answer="done")]
    )
    pre = _types(log, tid)
    lease = _wake_and_lease(dispatcher, tid, handle)
    outcome = run_leased_task(_RT(engine, log, cs, dispatcher), lease)
    assert outcome == "woken"
    types = _types(log, tid)
    # Exactly one TaskWoken; H2 consumed the wake (not re-delivered).
    assert types.count("TaskWoken") == 1
    assert dispatcher.wake(tid, HumanResponseReceived(handle=handle)) is False
    # The step ran (TaskCompleted). With no prelude, the woken window is
    # exactly the step: TaskWoken is IMMEDIATELY followed by the step's
    # ContextPlanComposed — no append-message lands before the compose (an
    # AppendMessagePrelude would, see the next test).
    window = types[len(pre):]
    assert window[0] == "TaskWoken"
    assert window[1] == "ContextPlanComposed"
    assert "TaskCompleted" in window


# ---------------------------------------------------------------------------
# State 2 — AppendMessagePrelude: send_goal
# ---------------------------------------------------------------------------


def test_append_message_prelude_lands_between_woken_and_step() -> None:
    engine, log, cs, dispatcher, tid, handle = _suspended_engine(
        [YieldForHumanDecision(prompt="a"), FinishDecision(answer="done")]
    )
    pre_len = len(log.read(tid))
    lease = _wake_and_lease(dispatcher, tid, handle)
    outcome = run_leased_task(
        _RT(engine, log, cs, dispatcher),
        lease,
        prelude=AppendMessagePrelude(content=[TextBlock(text="follow-up goal")]),
    )
    assert outcome == "woken"
    types = _types(log, tid)
    assert types.count("TaskWoken") == 1
    # H2: the wake was consumed by this resume (no stale matched left).
    assert dispatcher.wake(tid, HumanResponseReceived(handle=handle)) is False
    # Ordering: in this wake's window, the prelude's MessagesAppended sits
    # AFTER TaskWoken and BEFORE the step's compose (ContextPlanComposed) —
    # i.e. note_woken → append → run_one_step, exactly the old inline path.
    window = types[pre_len:]
    assert window[0] == "TaskWoken"
    appended = window.index("MessagesAppended")
    composed = window.index("ContextPlanComposed")
    assert 0 < appended < composed


# ---------------------------------------------------------------------------
# State 3 — ResolveApprovalPrelude: approval resolution
# ---------------------------------------------------------------------------


class _FakeWokenTask:
    """Minimal task stand-in folded from the fake engine's recorded steps."""

    def __init__(self, status: str, wake_on: Any) -> None:
        self.task_id = "t-approval"
        self.status = status
        self.wake_on = wake_on


class _FakeEngine:
    """Records the exact call order so the seam wiring (note_woken → prelude
    → run_one_step) and the prelude's forwarded arguments are asserted
    without standing up a full approval-suspending policy."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def note_woken(self, task: Any, *, lease_id: str, wake_event: Any) -> Any:
        self.calls.append(("note_woken", {"lease_id": lease_id}))
        return _FakeWokenTask("running", None)

    def resolve_tool_approval(
        self,
        task: Any,
        *,
        call_id: str,
        approved: bool,
        reason: Any,
        resolver: str,
        lease_id: str,
    ) -> Any:
        self.calls.append(
            (
                "resolve_tool_approval",
                {
                    "call_id": call_id,
                    "approved": approved,
                    "reason": reason,
                    "resolver": resolver,
                    "lease_id": lease_id,
                },
            )
        )
        return _FakeWokenTask("running", None)

    def run_one_step(
        self, task: Any, *, lease_id: str, cancelled: Any = None
    ) -> Any:
        # ``cancelled`` is the cooperative-stop poll ``run_leased_task`` now
        # threads in (top-level turn parity with the delegation drain); this
        # fake never trips it.
        self.calls.append(("run_one_step", {"lease_id": lease_id}))
        return _FakeWokenTask("terminal", None)


class _FakeLog:
    """Fold/read stub: a single matching TaskSuspended → TaskWoken window so
    ``run_leased_task`` takes the first-consume (case 1) branch."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def read(self, _task_id: str) -> list[Any]:
        return self._events


class _Env:
    def __init__(self, type_: str, payload: Any) -> None:
        self.type = type_
        self.payload = payload


class _Susp:
    def __init__(self, wake_on: Any) -> None:
        self.wake_on = wake_on


class _FakeDispatcher:
    def __init__(self) -> None:
        self.released: list[dict[str, Any]] = []

    def release(self, lease_id: str, **kw: Any) -> None:
        self.released.append({"lease_id": lease_id, **kw})


class _FakeLease:
    def __init__(self, wake_event: Any) -> None:
        self.task_id = "t-approval"
        self.lease_id = "L1"
        self.wake_event = wake_event


class _FakeRT:
    def __init__(self, engine: Any, log: Any, dispatcher: Any) -> None:
        self.engine = engine
        self.event_log = log
        self.content_store = object()
        self.dispatcher = dispatcher


def _fake_fold(monkeypatch: Any, status: str) -> None:
    """Patch worker.fold so case-1 (status==suspended at entry) is taken."""
    import noeta.runtime.worker as worker_mod

    monkeypatch.setattr(
        worker_mod,
        "fold",
        lambda _log, _cs, _tid: _FakeWokenTask(status, None),
    )


def test_resolve_approval_prelude_runs_between_woken_and_step(
    monkeypatch: Any,
) -> None:
    wake = HumanResponseReceived(handle="approval-c1")
    log = _FakeLog([_Env("TaskSuspended", _Susp(wake))])  # no TaskWoken yet
    engine = _FakeEngine()
    dispatcher = _FakeDispatcher()
    _fake_fold(monkeypatch, status="suspended")
    outcome = run_leased_task(
        _FakeRT(engine, log, dispatcher),
        _FakeLease(wake),
        prelude=ResolveApprovalPrelude(
            call_id="c1", approved=False, reason="nope", resolver="host"
        ),
    )
    assert outcome == "woken"
    # Exact ordering: note_woken → resolve_tool_approval → run_one_step.
    assert [name for name, _ in engine.calls] == [
        "note_woken",
        "resolve_tool_approval",
        "run_one_step",
    ]
    # The prelude forwarded its typed arguments verbatim.
    _, approval_kw = engine.calls[1]
    assert approval_kw == {
        "call_id": "c1",
        "approved": False,
        "reason": "nope",
        "resolver": "host",
        "lease_id": "L1",
    }
    # H2: the consuming release presents the wake.
    assert dispatcher.released[-1]["consumed_wake_event"] is wake


def test_append_message_prelude_runs_between_woken_and_step_fake(
    monkeypatch: Any,
) -> None:
    """Same ordering proof for the append-message prelude at the seam: the
    prelude is the only per-command variation on the one machine."""
    wake = HumanResponseReceived(handle="noeta-code-next-goal")

    class _AppendEngine(_FakeEngine):
        def append_user_message(
            self, task: Any, *, content: Any, lease_id: str, origin: Any = None
        ) -> Any:
            self.calls.append(
                (
                    "append_user_message",
                    {"content": content, "lease_id": lease_id, "origin": origin},
                )
            )
            return _FakeWokenTask("running", None)

    log = _FakeLog([_Env("TaskSuspended", _Susp(wake))])
    engine = _AppendEngine()
    dispatcher = _FakeDispatcher()
    _fake_fold(monkeypatch, status="suspended")
    outcome = run_leased_task(
        _FakeRT(engine, log, dispatcher),
        _FakeLease(wake),
        prelude=AppendMessagePrelude(content=[TextBlock(text="next")]),
    )
    assert outcome == "woken"
    assert [name for name, _ in engine.calls] == [
        "note_woken",
        "append_user_message",
        "run_one_step",
    ]
    assert engine.calls[1][1] == {
        "content": [TextBlock(text="next")],
        "lease_id": "L1",
        # send_goal stays byte-identical — origin defaults
        # to None (only the background completion-push passes "system").
        "origin": None,
    }
    assert dispatcher.released[-1]["consumed_wake_event"] is wake


def test_prelude_not_run_on_redelivered_wake(monkeypatch: Any) -> None:
    """H2 cases 2–4: a re-delivered wake whose TaskWoken is already durable
    reconciles by folded status and MUST NOT re-run the prelude (the
    command's bytes are already recorded). Here the fold reports terminal
    (case 3) and the log already has a matching TaskWoken."""
    wake = HumanResponseReceived(handle="approval-c1")
    log = _FakeLog(
        [
            _Env("TaskSuspended", _Susp(wake)),
            _Env("TaskWoken", _Woke(wake)),
        ]
    )
    engine = _FakeEngine()
    dispatcher = _FakeDispatcher()
    _fake_fold(monkeypatch, status="terminal")
    ran: list[str] = []

    class _SpyPrelude:
        def __call__(self, _engine: Any, task: Any, *, lease_id: str) -> Any:
            ran.append(lease_id)
            return task

    outcome = run_leased_task(
        _FakeRT(engine, log, dispatcher), _FakeLease(wake), prelude=_SpyPrelude()
    )
    assert outcome == "woken"
    # No engine work at all on reconcile, and the prelude never ran.
    assert engine.calls == []
    assert ran == []
    assert dispatcher.released[-1]["consumed_wake_event"] is wake


def test_case2_crash_after_taskwoken_runs_bare_step_dropping_prelude(
    monkeypatch: Any,
) -> None:
    """H2 case 2 — crash *after* ``TaskWoken`` is durable but
    *before* the prelude/step. The re-delivered wake folds to ``running`` with
    ``TaskWoken`` as the LAST event, so ``run_leased_task`` runs the bare
    ``run_one_step`` and does NOT re-run the prelude.

    This pins the documented contract limitation: the
    woken-command prelude is durable-safe only inside the synchronous
    first-consume call. Across a crash in that window, prelude-less
    re-delivery — the daemon / ``noeta resume`` always pass ``prelude=None`` —
    drives the bare step, so a pending ``send_goal`` / approval whose prelude
    never reached durability is LOST. Case 2 is also the *legitimate* recovery
    path for non-command wakes (timer / subtask-completion), where the bare
    step is exactly right; it cannot tell the two apart without durable command
    intent. Full crash-safety needs that (tracked separately) — until then the
    loss is conscious and tested, not accidental.
    """
    wake = HumanResponseReceived(handle="approval-c1")
    log = _FakeLog(
        [
            _Env("TaskSuspended", _Susp(wake)),
            _Env("TaskWoken", _Woke(wake)),  # last event → case 2 (no post-wake step)
        ]
    )
    engine = _FakeEngine()
    dispatcher = _FakeDispatcher()
    _fake_fold(monkeypatch, status="running")
    ran: list[str] = []

    class _SpyPrelude:
        def __call__(self, _engine: Any, task: Any, *, lease_id: str) -> Any:
            ran.append(lease_id)
            return task

    outcome = run_leased_task(
        _FakeRT(engine, log, dispatcher), _FakeLease(wake), prelude=_SpyPrelude()
    )
    assert outcome == "woken"
    # Bare recovery step: TaskWoken already durable so note_woken is NOT
    # re-issued, the prelude does NOT re-run, only run_one_step advances.
    assert [name for name, _ in engine.calls] == ["run_one_step"]
    assert ran == []
    assert dispatcher.released[-1]["consumed_wake_event"] is wake


class _Woke:
    def __init__(self, wake_event: Any) -> None:
        self.wake_event = wake_event


def test_prelude_protocol_accepts_dataclass_preludes() -> None:
    """``WokenPrelude`` is structural — the typed dataclasses satisfy it."""
    appended: WokenPrelude = AppendMessagePrelude(content=[TextBlock(text="x")])
    approval: WokenPrelude = ResolveApprovalPrelude(call_id="c1", approved=True)
    assert callable(appended) and callable(approval)


def test_prelude_provenance_defaults_are_host_neutral() -> None:
    """The operator CLI was deleted, so the neutral default
    provenance label is ``"host"`` (not the stale ``"cli"``) on both
    woken preludes — and it flows verbatim into the recorded
    resolver / answered_by audit fields."""
    assert ResolveApprovalPrelude(call_id="c1", approved=True).resolver == "host"
    assert (
        AnswerUserQuestionPrelude(question_id="q1", answers={}).answered_by
        == "host"
    )


def test_prelude_provenance_flows_into_engine_calls(monkeypatch: Any) -> None:
    """The default ``"host"`` provenance is forwarded verbatim into the
    Engine seams that write it to the EventLog audit records."""
    wake = HumanResponseReceived(handle="approval-c1")
    log = _FakeLog([_Env("TaskSuspended", _Susp(wake))])
    engine = _FakeEngine()
    dispatcher = _FakeDispatcher()
    _fake_fold(monkeypatch, status="suspended")
    run_leased_task(
        _FakeRT(engine, log, dispatcher),
        _FakeLease(wake),
        prelude=ResolveApprovalPrelude(call_id="c1", approved=True),
    )
    _, approval_kw = engine.calls[1]
    assert approval_kw["resolver"] == "host"
