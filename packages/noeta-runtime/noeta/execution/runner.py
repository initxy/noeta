"""Code-agnostic resident-session orchestration surface.

Hoisted verbatim from :class:`noeta.agent.execution.runner.CodeSessionRunner`.
The three domain seams (prepare / child-engine builder / result builder) are
left as abstract hooks; a coding-product subclass (``CodeSessionRunner``) and
any future product-specific runner fill them in.

Code-agnostic by contract: this module imports only ``noeta.protocols`` /
``noeta.core`` / ``noeta.execution`` / ``noeta.policies`` / ``noeta.runtime`` /
``noeta.tools`` — never ``noeta.agent`` (enforced by the import-linter
``execution-not-code`` contract).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Optional

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.execution.multi_turn import (
    MultiTurnReActPolicy,
    NEXT_GOAL_WAKE_HANDLE,
)
from noeta.execution.subtask_drain import (
    DrainHost,
    drive_pending_subtasks,
)
from noeta.policies.control_tools import (
    load_questions_body,
    normalize_answer_document,
    question_handle,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.messages import TextBlock
from noeta.protocols.wake import HumanResponseReceived
from noeta.runtime.worker import (
    AppendMessagePrelude,
    AnswerUserQuestionPrelude,
    ResolveApprovalPrelude,
    WokenPrelude,
    run_leased_task,
)
from noeta.tools.mcp import McpStdioClient


__all__ = [
    "PreparedSession",
    "ResidentSessionRunner",
]


# ---------------------------------------------------------------------------
# Prepared runtime handle
# ---------------------------------------------------------------------------


@dataclass
class PreparedSession:
    """Internal handle on the live runtime after :meth:`ResidentSessionRunner.prepare`.

    Populated by ``prepare()``; consumed by ``execute()`` and
    ``shutdown()``. Keeps the runner field shape narrow so the public
    properties (``event_log`` / ``content_store``) can be checked
    without exposing the Engine to callers.
    """

    event_log: EventLogFull
    content_store: ContentStore
    dispatcher: Dispatcher
    engine: Engine
    task: Any
    lease_id: str
    observers: list[Any] = field(default_factory=list)
    unsubscribe_child: Optional[Callable[[], None]] = None
    close_storage: Optional[Callable[[], None]] = None
    #: Phase 4.5 F2 — live MCP server connections to reap on shutdown.
    mcp_clients: list[McpStdioClient] = field(default_factory=list)
    #: Set when the session is built in multi-turn mode (Phase 4.5 I3):
    #: the policy handed to the Engine is a ``MultiTurnReActPolicy`` whose
    #: ``final`` flag the runner flips between turns. ``None`` for the
    #: default one-shot session.
    multi_turn_policy: Optional["MultiTurnReActPolicy"] = None
    #: memory v1: the workspace memory store when the
    #: session was prepared with memory enabled. ``resume_with_goal``'s
    #: ``_goal_prelude`` seam reads it to route follow-up goals through the
    #: recall intake (``append_user_message_with_recall``). ``None`` ⇒
    #: plain ``AppendMessagePrelude`` (existing behaviour).
    memory_store: Optional[Any] = None


# ---------------------------------------------------------------------------
# Base runner
# ---------------------------------------------------------------------------


class ResidentSessionRunner:
    """Hoisted resident-session orchestration base.

    The common session-runner logic lives here; concrete subclasses (e.g.
    :class:`~noeta.agent.execution.runner.CodeSessionRunner`) implement the
    three abstract seams below. Designed as a **plain class** (not a
    ``@dataclass``) so a subclass keeps full control of its ``__init__``
    and field shape.

    Semantics preserved byte-for-byte from the original
    ``CodeSessionRunner``: the per-turn ``last_seq`` cursor, the woken-
    command drive machine, the delegation drain, and the observer/storage
    teardown order are all lifted without change.
    """

    worker_id: ClassVar[str] = "noeta-session"
    """Worker identifier used for dispatcher leases during woken-command
    resume. Product subclasses override (coding subclass = ``"noeta-code"``
    to preserve the existing lease identity)."""

    def __init__(self) -> None:
        self._prepared: Optional[PreparedSession] = None

    # -- split lifecycle --------------------------------------------------

    @property
    def event_log(self) -> EventLogFull:
        """Live ``EventLog`` after :meth:`prepare`. Raises before."""
        if self._prepared is None:
            raise RuntimeError(f"{type(self).__name__}.prepare() not called yet")
        return self._prepared.event_log

    @property
    def content_store(self) -> ContentStore:
        """Live ``ContentStore`` after :meth:`prepare`. Raises before."""
        if self._prepared is None:
            raise RuntimeError(f"{type(self).__name__}.prepare() not called yet")
        return self._prepared.content_store

    @property
    def task_id(self) -> str:
        """The leased task's id after :meth:`prepare`. Raises before."""
        if self._prepared is None:
            raise RuntimeError(f"{type(self).__name__}.prepare() not called yet")
        return str(self._prepared.task.task_id)

    @property
    def pending_wake_handle(self) -> Optional[str]:
        """The handle string of the current suspension's
        ``HumanResponseReceived`` wake, or ``None`` when not prepared,
        not suspended, or suspended on a non-``HumanResponseReceived``
        condition (Phase 4.5 F1).

        Read-only adapter accessor: it exposes **only the handle
        string** — never the ``Task`` or the wake object — so a CLI
        front-end (`noeta code repl` / the `--approvals-file` driver) can
        tell a next-goal suspend from an ``approval-{call_id}`` suspend
        without reaching into ``_prepared``.
        """
        if self._prepared is None:
            return None
        task = self._prepared.task
        if getattr(task, "status", None) != "suspended":
            return None
        wake_on = getattr(task, "wake_on", None)
        if isinstance(wake_on, HumanResponseReceived):
            return wake_on.handle
        return None

    # -- abstract seams ---------------------------------------------------

    def prepare(self) -> "ResidentSessionRunner":
        """Open storage, build engine + observers, lease the task, and
        emit seed writes. Returns ``self`` so callers can chain.

        Contract for implementations:
          * Open event_log / content_store / dispatcher storage.
          * Build the product Engine + observers.
          * ``engine.create_task`` → ``dispatcher.enqueue`` →
            ``dispatcher.lease`` for the worker.
          * Emit pre-loop writes (user message, skill activations …).
          * Set ``self._prepared = PreparedSession(...)`` carrying all
            live handles.
          * Return ``self``.
        """
        raise NotImplementedError

    def _build_child_engine(self, child_id: str) -> Engine:
        """Build a child sub-agent's Engine for the delegation drain.

        Contract: receive a child task id, read whatever product-specific
        state is needed (agent identity, config, budget …), and return a
        real ``Engine`` wired against the shared storage/dispatcher.
        """
        raise NotImplementedError

    def _build_result(self, task: Any, p: PreparedSession, *, pre_turn_seq: int) -> Any:
        """Project the terminal task + this turn's event slice into a
        product-specific result object.

        ``pre_turn_seq`` is the event-log ``seq`` cursor **before** the
        turn ran, so implementations can diff ``events with seq >
        pre_turn_seq`` for per-turn read-models.
        """
        raise NotImplementedError

    # -- common surface ---------------------------------------------------

    def set_turn_final(self, final: bool) -> None:
        """Flip the multi-turn wrapper's ``final`` flag (Phase 4.5 I3).

        No-op for one-shot sessions (no wrapper). The CLI ``noeta code
        chat`` loop calls this before each turn so the last turn emits
        a real ``TaskCompleted`` while earlier turns suspend.

        (Hoisted from ``CodeSessionRunner.set_turn_final``.)
        """
        if self._prepared is None:
            raise RuntimeError(f"{type(self).__name__}.prepare() not called yet")
        wrapper = self._prepared.multi_turn_policy
        if wrapper is not None:
            wrapper.set_final(final)

    def execute(self) -> Any:
        """Drive the leased task to terminal / suspend / max-steps and
        return a product result. Must be called after :meth:`prepare`.

        Tracks a per-turn ``last_seq`` cursor so a future
        :meth:`resume_with_goal` call (Phase 4.5 I3 multi-turn) can
        slice read-models to events produced **by that single turn**,
        not the cumulative stream (architect constraint #5).

        (Hoisted from ``CodeSessionRunner.execute``.)
        """
        if self._prepared is None:
            raise RuntimeError(f"{type(self).__name__}.prepare() not called yet")
        p = self._prepared
        pre_turn_seq = self._last_seq(p.event_log, p.task.task_id)
        task = p.engine.run_one_step(p.task, lease_id=p.lease_id)
        # When the policy returns a suspending Decision the Engine
        # sets ``task.wake_on`` via fold; release must carry it so
        # ``dispatcher.wake`` later matches against the right
        # condition (Phase 4.5 I3 architect constraint #2).
        if task.status == "suspended":
            p.dispatcher.release(
                p.lease_id,
                next_state="suspended",
                wake_on=task.wake_on,
            )
        else:
            p.dispatcher.release(p.lease_id, next_state=task.status)
        # Issue C: drive any pending sub-agent delegations to completion
        # (the parent suspended on a SubtaskCompleted wake). Other suspend
        # kinds (multi-turn next-goal, tool approval) are left for their
        # own resume seams. S3a: the drain state machine is host-neutral
        # (``noeta.execution.subtask_drain``); the runner injects the
        # storage/lease seam + its child-engine builder + the root parent
        # engine choice, and keeps its own ``p.lease_id`` update via the
        # ``on_root_release`` callback.
        def _on_root_release(lease_id: str) -> None:
            p.lease_id = lease_id

        host = DrainHost(
            dispatcher=p.dispatcher,
            event_log=p.event_log,
            content_store=p.content_store,
            build_child_engine=self._build_child_engine,
            parent_engine=lambda pid, *, is_root: (
                p.engine if is_root else self._build_child_engine(pid)
            ),
            on_root_release=_on_root_release,
        )
        task = drive_pending_subtasks(host, task)
        p.task = task  # mutable snapshot for shutdown / property reads
        return self._build_result(task, p, pre_turn_seq=pre_turn_seq)

    @staticmethod
    def _last_seq(event_log: EventLogFull, task_id: str) -> int:
        events = event_log.read(task_id)
        return events[-1].seq if events else -1

    def resume_with_goal(self, goal: str) -> Any:
        """Drive a follow-up turn on the same task — Phase 4.5 I3.

        Requires the previous :meth:`execute` to have ended in
        ``status="suspended"`` with a
        ``HumanResponseReceived(handle=…)`` wake. The runner then:

        1. ``dispatcher.wake(task_id, HumanResponseReceived(handle=…))``
        2. targeted ``dispatcher.lease(task_id=task_id, …)`` —
           atomically consumes the matched wake event.
        3. ``engine.note_woken(task, lease_id, wake_event)`` —
           emits ``TaskWoken`` so fold flips status to ``running``.
        4. ``engine.append_user_message(task, goal, lease_id)`` —
           seeds the new turn's first message.
        5. ``engine.run_one_step(task, lease_id)`` — drives the
           ReAct loop until the next suspend / terminal.

        The runner does **not** fabricate the wake event — it walks
        the real dispatcher contract (architect constraint #3).

        (Hoisted from ``CodeSessionRunner.resume_with_goal``.)
        """
        if self._prepared is None:
            raise RuntimeError(f"{type(self).__name__}.prepare() not called yet")
        p = self._prepared
        prior = p.task
        wake_on = getattr(prior, "wake_on", None)
        # Only the multi-turn "next goal" suspension is resumable with a
        # new goal. A session suspended for approval / a subtask / a
        # timer / a different human handle must NOT be satisfied by
        # appending a goal here — that would silently consume an
        # unrelated wake condition. Refuse and emit no TaskWoken.
        if (
            prior.status != "suspended"
            or not isinstance(wake_on, HumanResponseReceived)
            or wake_on.handle != NEXT_GOAL_WAKE_HANDLE
        ):
            raise RuntimeError(
                "resume_with_goal requires the prior turn to have suspended "
                f"on HumanResponseReceived(handle={NEXT_GOAL_WAKE_HANDLE!r}); "
                f"got status={prior.status!r}, wake_on={wake_on!r}"
            )
        return self._drive_woken_command(
            handle=HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE),
            prelude=self._goal_prelude(goal),
        )

    def _goal_prelude(self, goal: str) -> Any:
        """The follow-up goal's intake prelude (seam).

        Default: the plain ``AppendMessagePrelude`` (byte-identical to the
        pre-seam behaviour). A session prepared with a memory store routes
        through :class:`noeta.execution.memory.RecallGoalPrelude` so the D6
        user-message recall intake covers resume turns too — retrieval
        (impure) runs before anything enters the ledger; hits land with
        ``origin="memory"`` through the Engine's sole-writer seam; a resume
        folds the recorded turn and never re-runs retrieval.
        """
        p = self._prepared
        store = getattr(p, "memory_store", None) if p is not None else None
        if store is not None:
            from noeta.execution.memory import RecallGoalPrelude

            return RecallGoalPrelude(content=[TextBlock(text=goal)], store=store)
        return AppendMessagePrelude(content=[TextBlock(text=goal)])

    def resolve_tool_approval(
        self,
        *,
        call_id: str,
        approved: bool,
        reason: Optional[str] = None,
        resolver: str = "cli",
    ) -> Any:
        """Resolve a pending tool-call approval and resume — Phase 4.5
        Issue A.

        The **in-process, testable seam** (architect reminder #1): the CLI
        ``--approvals-file`` adapter and any future API surface call this;
        no approve/deny logic is buried in CLI-only code. Requires the
        prior turn to have suspended on
        ``HumanResponseReceived(handle="approval-{call_id}")``.

        Walks the real dispatcher contract — ``wake`` → targeted ``lease``
        → ``engine.note_woken`` → ``engine.resolve_tool_approval`` (which
        runs the approved call or appends denial feedback) → one
        ``run_one_step`` to continue the ReAct loop to the next suspend /
        terminal. The runner does not fabricate the wake event.

        (Hoisted from ``CodeSessionRunner.resolve_tool_approval``.)
        """
        if self._prepared is None:
            raise RuntimeError(f"{type(self).__name__}.prepare() not called yet")
        p = self._prepared
        prior = p.task
        handle = f"approval-{call_id}"
        wake_on = getattr(prior, "wake_on", None)
        if (
            prior.status != "suspended"
            or not isinstance(wake_on, HumanResponseReceived)
            or wake_on.handle != handle
        ):
            raise RuntimeError(
                "resolve_tool_approval requires the prior turn to have "
                f"suspended on HumanResponseReceived(handle={handle!r}); "
                f"got status={prior.status!r}, wake_on={wake_on!r}"
            )
        return self._drive_woken_command(
            handle=HumanResponseReceived(handle=handle),
            prelude=ResolveApprovalPrelude(
                call_id=call_id,
                approved=approved,
                reason=reason,
                resolver=resolver,
            ),
        )

    def answer_user_question(
        self,
        *,
        question_id: str,
        answers: dict[str, Any],
        answered_by: str = "cli",
    ) -> Any:
        """Answer a pending `ask_user_question` suspend and resume.

        (Hoisted from ``CodeSessionRunner.answer_user_question``.)
        """
        if self._prepared is None:
            raise RuntimeError(f"{type(self).__name__}.prepare() not called yet")
        p = self._prepared
        prior = p.task
        handle = question_handle(question_id)
        wake_on = getattr(prior, "wake_on", None)
        if (
            prior.status != "suspended"
            or not isinstance(wake_on, HumanResponseReceived)
            or wake_on.handle != handle
        ):
            raise RuntimeError(
                "answer_user_question requires the prior turn to have "
                f"suspended on HumanResponseReceived(handle={handle!r}); "
                f"got status={prior.status!r}, wake_on={wake_on!r}"
            )
        pending = prior.governance.pending_questions.get(question_id)
        if pending is None:
            raise RuntimeError(
                f"task {prior.task_id!r} is suspended on {handle!r} but has "
                "no matching pending question"
            )
        questions = load_questions_body(p.content_store, pending["questions_ref"])
        normalized = normalize_answer_document({"answers": answers}, questions)
        return self._drive_woken_command(
            handle=HumanResponseReceived(handle=handle),
            prelude=AnswerUserQuestionPrelude(
                question_id=question_id,
                answers=normalized,
                answered_by=answered_by,
            ),
        )

    def _drive_woken_command(
        self, *, handle: HumanResponseReceived, prelude: WokenPrelude
    ) -> Any:
        """Shared woken-command machine for ``resume_with_goal`` /
        ``resolve_tool_approval`` / ``answer_user_question`` (D4).

        The runner is the **degenerate in-process, single-task host** over
        the canonical drive primitive :func:`run_leased_task`: it walks the
        real dispatcher contract (``wake`` → targeted ``lease`` consuming the
        matched wake), then hands the lease to ``run_leased_task`` with a
        typed woken-command-prelude — so the H2 ``consumed_wake_event``
        release discipline (D2) and the
        ``note_woken → <prelude> → run_one_step`` ordering are NOT re-inlined
        here but ride the one shared machine the daemon worker also uses.

        Callers validate that ``p.task`` is suspended on the right
        ``HumanResponseReceived`` handle before calling. The runner does not
        fabricate the wake event.

        (Hoisted from ``CodeSessionRunner._drive_woken_command``.)
        """
        p = self._prepared
        assert p is not None
        prior = p.task
        # Wake with the canonical condition (not whatever the task carried)
        # so the match is explicit and auditable.
        p.dispatcher.wake(prior.task_id, handle)
        lease = p.dispatcher.lease(
            worker_id=self.worker_id, lease_seconds=600.0, task_id=prior.task_id
        )
        if lease is None or lease.wake_event is None:
            raise RuntimeError(
                "dispatcher did not hand out a wake event for the resumed "
                f"task {prior.task_id!r}"
            )
        p.lease_id = lease.lease_id
        pre_turn_seq = self._last_seq(p.event_log, prior.task_id)
        # Single drive primitive: note_woken → prelude → run_one_step →
        # release(consumed_wake_event). No second resume machine.
        run_leased_task(p, lease, prelude=prelude)
        task = fold(p.event_log, p.content_store, prior.task_id)
        p.task = task
        return self._build_result(task, p, pre_turn_seq=pre_turn_seq)

    def shutdown(self) -> None:
        """Stop observers + close storage. Idempotent.

        (Hoisted from ``CodeSessionRunner.shutdown``.)
        """
        if self._prepared is None:
            return
        self._teardown(self._prepared)
        self._prepared = None

    # -- one-shot convenience --------------------------------------------

    def run(self) -> Any:
        """``prepare`` + ``execute`` + ``shutdown`` in one call.

        (Hoisted from ``CodeSessionRunner.run``.)
        """
        self.prepare()
        try:
            return self.execute()
        finally:
            self.shutdown()

    @staticmethod
    def _teardown(state: PreparedSession) -> None:
        for obs in reversed(state.observers):
            with contextlib.suppress(Exception):
                obs.stop()
        if state.unsubscribe_child is not None:
            with contextlib.suppress(Exception):
                state.unsubscribe_child()
        # F2: reap live MCP server processes (bounded terminate→kill).
        for mcp_client in state.mcp_clients:
            with contextlib.suppress(Exception):
                mcp_client.shutdown()
        if state.close_storage is not None:
            with contextlib.suppress(Exception):
                state.close_storage()
