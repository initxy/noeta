"""L2 hosting layer — the resident worker that drives leased Tasks.

This is the run layer of the Hosted Single-Host Runtime: the
resident equivalent of a single-task resume drain loop. It deliberately
lives in L2 (not a higher layer) so the SDK / embedding / external
platforms can drive a runtime without reverse-depending on the code
runner.

Three pieces:

* :class:`WorkerRuntime` — the narrow structural Protocol the worker
  needs: ``engine`` / ``event_log`` / ``content_store`` / ``dispatcher``.
  ``noeta.testing.profile.RuntimeBundle`` (the test seam) and the live
  ``noeta.agent.resolver.CodeEngineResolver`` satisfy it structurally; L2
  never imports those higher-layer types.
* :func:`run_leased_task` — the canonical 3-state resume machine
  (woken / drained / skipped), the single implementation shared by every
  resume surface. ``noeta.agent.driver`` / ``noeta.agent.session`` re-import it.
* :class:`WorkerLoop` — the drain loop + worker exception policy (a
  daemon must never crash on one poisoned task).

Module capabilities: the worker loop (lease → run → release), the
per-step heartbeat side-thread, the periodic stale-sweep, and the
best-effort signal-driven graceful shutdown all live here.
The code runner (``python -m noeta.agent``) is the concrete host that
wraps this module. Single-worker only; no ``--workers``.

Graceful shutdown is **bounded process-shutdown** (H1): on SIGTERM /
SIGINT the loop stops leasing new tasks and waits up to
``shutdown_grace_s`` for the in-flight step (run on a daemon **step
thread**) to finish, then releases and exits. If the grace elapses the
loop **abandons** the step — stops its heartbeat so the lease expires,
emits a ``shutdown_abandoned`` :class:`ReliabilityEvent`, sets
:attr:`WorkerLoop.abandoned`, and returns without touching the lease.
There is still NO in-process interrupt (Python cannot kill the thread):
abandon is **process-shutdown only** — the host MUST exit the process,
the abandoned thread may still run + write the EventLog, and the
expired lease is reclaimed by ``requeue_stale`` after the process dies.
``shutdown_grace_s=None`` / ``<= 0`` restores the old unbounded wait.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Iterator, Literal, Optional, Protocol

from noeta.core.engine import abandon_step_attempt, suspend_on_human_handle
from noeta.core.fold import BoundedEventLog, fold
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.errors import InvalidLease, TaskCancellationRequested
from noeta.protocols.messages import (
    Block,
    MessageOrigin,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.wake import (
    NEXT_GOAL_WAKE_HANDLE,
    HumanResponseReceived,
    SubtaskCompleted,
    SubtaskGroupCompleted,
    matches_wake,
)
from noeta.runtime.attempt import (
    ABANDON_CAP,
    InterruptedAttempt,
    PendingPark,
    classify_attempt,
    scan_interrupted_attempt,
    scan_pending_park,
)


__all__ = [
    "DEFAULT_SHUTDOWN_GRACE_S",
    "AppendMessagePrelude",
    "AnswerUserQuestionPrelude",
    "ReliabilityEvent",
    "ReliabilityKind",
    "ReliabilitySink",
    "ResolveApprovalPrelude",
    "WokenPrelude",
    "WorkerLoop",
    "WorkerOutcome",
    "WorkerRuntime",
    "install_stop_signals",
    "keep_lease_alive",
    "resolve_engine",
    "run_leased_task",
]


_log = logging.getLogger(__name__)

DEFAULT_SHUTDOWN_GRACE_S = 30.0


# ---------------------------------------------------------------------------
# Reliability events (H1) — process-local observability ONLY.
# ---------------------------------------------------------------------------
#
# These are deliberately NOT EventLog events: they are not persisted, not
# folded, and not resumed. They live inside
# ``noeta.runtime.worker`` as a plain dataclass + an injectable sink so an
# operator (or a future external trace-export slice) can observe daemon
# reliability moments without touching the L0 / resume contract.
#
# Every ``kind`` is named for what the worker can ACTUALLY prove from the
# Dispatcher seam — never a root cause it cannot observe:
#
# * ``stale_requeued``          — a ``requeue_stale()`` sweep returned ≥1 lease.
# * ``suspended_without_wake``  — a leased suspended task had no wake_event
#                                 (the wake-loss *symptom*; cause unprovable).
# * ``step_failed_retryable``   — the loop caught a step exception and called
#                                 ``dispatcher.fail(retryable=True)``. Does NOT
#                                 claim the task went terminal (the Dispatcher
#                                 decides requeue-vs-terminal; ``fail`` returns
#                                 nothing).
# * ``heartbeat_invalid_lease`` — a heartbeat got ``InvalidLease`` (symptom;
#                                 cause may be cap/expired/requeued/released).
# * ``shutdown_abandoned``      — the shutdown grace elapsed with a step still
#                                 in flight (process-shutdown only — see
#                                 ``WorkerLoop`` docstring).
# * ``timers_fired``            — the timer poll delivered ``TimerFired``
#                                 wakes to due ``wait_timer`` suspends.
# * ``attempt_abandoned``       — crash recovery sealed an interrupted
#                                 attempt and re-drove the step automatically
#                                 (classified side-effect-safe).
# * ``attempt_parked``          — crash recovery sealed an interrupted
#                                 attempt and parked the task for a human
#                                 (unsafe tool activity, or the abandon cap).
ReliabilityKind = Literal[
    "stale_requeued",
    "suspended_without_wake",
    "step_failed_retryable",
    "heartbeat_invalid_lease",
    "shutdown_abandoned",
    "timers_fired",
    "attempt_abandoned",
    "attempt_parked",
]


@dataclass(frozen=True, slots=True)
class ReliabilityEvent:
    """A process-local daemon reliability signal (NOT an EventLog event)."""

    kind: ReliabilityKind
    task_id: Optional[str] = None
    lease_id: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)


ReliabilitySink = Callable[[ReliabilityEvent], None]


def _default_reliability_sink(event: ReliabilityEvent) -> None:
    _log.warning(
        "worker reliability: kind=%s task=%s lease=%s detail=%s",
        event.kind,
        event.task_id,
        event.lease_id,
        event.detail,
    )


# The lifecycle outcome of advancing one leased task. A Literal so mypy
# catches a mistyped tag at the call site (architect I1 non-blocking).
#   ``"cancelled"`` / ``"stopped"`` — a human cancel/close landed mid-turn and
#   the in-flight result was abandoned (see :func:`_settle_stopped_turn`):
#   ``"cancelled"`` left the task terminal, ``"stopped"`` left it reopenable.
WorkerOutcome = Literal["woken", "drained", "skipped", "cancelled", "stopped"]


class WakeRecoveryError(Exception):
    """H2 (D4 case 6) — a woken lease's wake cannot be reconciled
    against the task's folded state (no matching suspension, or an
    unexpected status). The worker fails loud rather than silently
    continue."""


def _find_matching_woken_index(events: list[Any], wake_event: Any) -> Optional[int]:
    """D4 / P1.2 — within the **current suspend-window** (after the
    latest ``TaskSuspended`` whose ``wake_on`` matches ``wake_event`` **or the
    latest ``TaskRewound`` re-base, whichever is later**), the index of a
    ``TaskWoken`` whose ``wake_event`` equals it. Returns the boundary-less
    sentinel via the caller; raises is the caller's job.

    Returns ``(boundary, matching_idx)`` semantics are split: here we return
    only ``matching_idx`` (or ``None``); ``boundary is None`` is signalled by
    a sentinel of ``-2``.
    """
    boundary = -1
    for i, e in enumerate(events):
        if e.type == "TaskSuspended":
            wake_on = getattr(e.payload, "wake_on", None)
            if wake_on is not None and matches_wake(wake_on, wake_event):
                boundary = i
        elif e.type == "TaskRewound":
            # A TaskRewound re-bases the stream
            # — fold treats it as a snapshot baseline and everything before it is
            # dead history. A ``TaskWoken`` stranded before this marker (a turn
            # that woke, ran, and was then undone by a conversation rewind) is
            # therefore NOT a prior consumption of THIS wake. Advance the window
            # past the marker so the next genuine wake is a fresh first-consume
            # (case 1) rather than a phantom already-consumed duplicate (case 4)
            # that would silently drop the new goal — the rewind baseline is a
            # next-goal suspend, so the conversation must be live again.
            boundary = i
    if boundary < 0:
        return -2  # no matching suspension → caller raises WakeRecoveryError
    matching: Optional[int] = None
    for i in range(boundary + 1, len(events)):
        e = events[i]
        if (
            e.type == "TaskWoken"
            and getattr(e.payload, "wake_event", None) == wake_event
        ):
            matching = i
    return matching


class WorkerRuntime(Protocol):
    """Narrow structural view of a runtime the worker drives.

    Only the components the step needs are declared, as **read-only
    properties** so both a mutable-attribute object and a frozen
    dataclass (e.g. the higher-layer ``noeta.testing.profile.RuntimeBundle``)
    satisfy it. L2 never imports those higher layers — the match is purely
    structural.

    ``engine`` is the single-Engine view (one host = one Agent). A
    resident host that drives many Agents instead implements
    ``resolve_engine(task) → Engine`` (D1): the per-task agent→
    engine resolver. :func:`resolve_engine` (below) is the L2 seam that
    picks between them — it prefers ``rt.resolve_engine(task)`` when the
    runtime provides it, else falls back to the single ``rt.engine``. The
    agent-lookup logic itself lives in the host (L3 ``noeta.agent``), so L2
    never reverse-depends on the Agent registry.
    """

    @property
    def engine(self) -> Any: ...

    @property
    def event_log(self) -> Any: ...

    @property
    def content_store(self) -> Any: ...

    @property
    def dispatcher(self) -> Any: ...


def resolve_engine(rt: WorkerRuntime, task: Any) -> Any:
    """The per-task agent→engine seam (D1).

    Returns the Engine that drives ``task``. If the runtime supplies a
    ``resolve_engine(task)`` method (a resident multi-Agent host), defer to
    it — that is where the ``TaskCreated.agent_name`` → ``get_agent`` →
    ``build_engine_for_agent`` fold lives (in L3, so L2 never imports the
    Agent registry). An **unknown** agent raises there at lease time — a
    hard error, never a silent no-op (D2). A single-Agent host
    (the degenerate ``CodeSessionRunner`` / the daemon over one Agent) has
    no ``resolve_engine`` and falls back to its single ``rt.engine``.
    """
    resolver = getattr(rt, "resolve_engine", None)
    if resolver is not None:
        return resolver(task)
    return rt.engine


# ---------------------------------------------------------------------------
# Woken-command-prelude seam (D4)
# ---------------------------------------------------------------------------
#
# ``run_leased_task``'s woken branch is *only* ``note_woken → run_one_step``.
# But the real product commands inject a step **between** ``note_woken`` and
# ``run_one_step``: ``send_goal`` appends the new turn's user message; an
# approval resolution runs/denies the pending tool call. Without a seam, the
# CLI ``CodeSessionRunner`` re-implemented the whole lease→note_woken→<prelude>
# →run_one_step→release machine inline (including the H2
# ``consumed_wake_event`` release discipline) — the CLI/web divergence source.
#
# A ``WokenPrelude`` is a typed, byte-pure step run inside the H2 case-1
# (first-consume) window, after ``TaskWoken`` is durable and before the step.
# It MUST be a no-op-or-append over the SAME engine/lease so the recorded
# bytes are identical to the old inline path:
#
#   note_woken → <prelude events> → run_one_step → release(consumed_wake_event)
#
# Three states: append-message / resolve-approval / ``None`` (the daemon
# worker-loop's plain woken branch). The prelude is the ONLY per-command
# variation; every surface shares this one machine.
#
# ``durable_at_seed`` (D6, docs/adr/step-attempt-recovery.md) marks the
# preludes that only APPEND durable events (message / answer / ModelBound):
# the driver's seed applies these synchronously on the request thread —
# ``note_woken`` + prelude land BEFORE the command is acked, so an acked
# ``send_goal`` / ``answer`` can never lose the user's input to a crash. The
# drive then enters the woken machine prelude-less and runs the bare step
# (case 2′). ``ResolveApprovalPrelude`` stays drive-side (it EXECUTES the
# approved tool and must not block the request thread); its narrower loss
# mode is benign — the task re-suspends on the same approval.


class WokenPrelude(Protocol):
    """A post-``note_woken`` / pre-``run_one_step`` step on the woken task.

    Called with the woken task (status ``running`` after ``TaskWoken``) and
    the active ``lease_id``; returns the (possibly-advanced) task to feed
    into ``run_one_step``. Implementations MUST only append durable events
    over the given engine + lease — they ride the H2 first-consume window,
    so their bytes land between ``TaskWoken`` and the step, exactly as the
    old inline CLI path recorded them.

    ``durable_at_seed`` (a class attribute, read via ``getattr`` with a
    ``False`` default) opts an append-only prelude into seed-time
    application — see the D6 note above.
    """

    def __call__(self, engine: Any, task: Any, *, lease_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class AppendMessagePrelude:
    """``send_goal`` prelude — seed the new turn's first user message.

    Mirrors ``engine.append_user_message(task, content, lease_id)`` (the
    step formerly inlined in ``CodeSessionRunner.resume_with_goal``).
    D5: carries the typed ``content: list[Block]`` (a text-only
    follow-up goal passes ``[TextBlock(goal)]``; images ride along).

    ``origin`` is the optional ``Message.origin`` tag the
    append seam stamps. ``None`` keeps ``send_goal`` byte-identical (a human
    turn carries no origin); the background completion-push
    (``InteractionDriver.notify_background_exit``) passes ``"system"`` so the
    model sees a source-tagged "background event" notice, not a user message.

    ``attachment_texts`` are the unified ``@`` mention snapshots
    (workspace files + MCP static resources) read host-side at send time. Each is
    appended as its OWN ``origin="system"`` user message BEFORE the goal message,
    so the transcript attributes the host-injected reference material distinctly
    from the human goal (a Message carries a single origin, so they cannot share
    one). Empty (the default) keeps ``send_goal`` byte-identical to the no-mention
    path. Being ordinary recorded messages, resume reads them back and never
    re-reads the file / resource.

    ``activate_skills`` are the built-in skill names a slash command resolved to
    (the ``/review``-style deterministic activation, mirroring Claude Code's
    ``/skill-name``): after the goal message is appended, this emits a
    ``TaskStatePatched(activate_skills=[...])`` in the SAME first-consume window
    (goal-then-patch order, matching ``seed_start``), so the composer pins the
    skill bodies for THIS turn onward. The Engine's ``apply_state_patch`` records
    the per-skill content provenance itself (fold-guarded, first-only). Empty
    (the default) keeps ``send_goal`` byte-identical to the no-skill path."""

    content: list[Block]
    origin: Optional[MessageOrigin] = None
    attachment_texts: tuple[str, ...] = ()
    activate_skills: tuple[str, ...] = ()

    #: Pure appends — safe (and required, D6) to run at seed time.
    durable_at_seed: ClassVar[bool] = True

    def __call__(self, engine: Any, task: Any, *, lease_id: str) -> Any:
        for text in self.attachment_texts:
            engine.append_user_message(
                task,
                content=[TextBlock(text=text)],
                lease_id=lease_id,
                origin="system",
            )
        task = engine.append_user_message(
            task, content=self.content, lease_id=lease_id, origin=self.origin
        )
        if self.activate_skills:
            task = engine.apply_state_patch(
                task,
                patch=TaskStatePatch(activate_skills=list(self.activate_skills)),
                lease_id=lease_id,
            )
        return task


@dataclass(frozen=True, slots=True)
class ResolveApprovalPrelude:
    """Approval prelude — run the approved tool call or append the denial.

    Mirrors ``engine.resolve_tool_approval(...)`` (the step formerly inlined
    in ``CodeSessionRunner.resolve_tool_approval``). NOT ``durable_at_seed``:
    it executes the approved tool, which must not block the command's
    request thread."""

    call_id: str
    approved: bool
    reason: Optional[str] = None
    resolver: str = "host"

    durable_at_seed: ClassVar[bool] = False

    def __call__(self, engine: Any, task: Any, *, lease_id: str) -> Any:
        return engine.resolve_tool_approval(
            task,
            call_id=self.call_id,
            approved=self.approved,
            reason=self.reason,
            resolver=self.resolver,
            lease_id=lease_id,
        )


@dataclass(frozen=True, slots=True)
class AnswerUserQuestionPrelude:
    """Question-answer prelude — append answer audit and paired tool result."""

    question_id: str
    answers: dict[str, dict[str, Any]]
    answered_by: str = "host"

    #: Pure appends (answer audit + paired tool result) — seed-time safe.
    durable_at_seed: ClassVar[bool] = True

    def __call__(self, engine: Any, task: Any, *, lease_id: str) -> Any:
        return engine.answer_user_question(
            task,
            question_id=self.question_id,
            answers=self.answers,
            answered_by=self.answered_by,
            lease_id=lease_id,
        )


# ---------------------------------------------------------------------------
# Subtask wake rendering — mirror of subtask_drain._resume_parent_leased.
#
# When a parent task is woken by a SubtaskCompleted / SubtaskGroupCompleted
# (delivered by the ChildLifecycleObserver), the child result(s) must be
# rendered as paired tool_result messages between note_woken and
# run_one_step, exactly as the in-request subtask_drain does. The composer
# does NOT project these from SubtaskCompleted events — they are
# governance-side state that only becomes a Message via the Engine's
# append_subagent_result_* seams. Without this prelude a worker-loop-driven
# parent would compose with a dangling spawn_subagent tool_use and providers
# would reject the request.
# ---------------------------------------------------------------------------

_SPAWN_TOOL_NAMES = frozenset({"spawn_subagent", "run_workflow"})


def _render_subtask_wake(
    engine: Any, task: Any, wake_event: Any, *, lease_id: str
) -> Any:
    """Render paired tool_result(s) for a subtask-completion wake, if needed.

    Returns the (possibly-advanced) task. A non-subtask wake is a no-op so
    callers can invoke this unconditionally at the top of the first-consume
    window. Idempotent: if the tool_result for this spawn is already
    present on the transcript, nothing is appended — safe to call on the
    bare re-drive (case 2′) after a crash between note_woken and the step.

    Pairing correctness (reviewer finding 1): both the single-child and
    the fan-out-group branches match tool_result to its originating
    ``spawn_subagent`` tool_use positionally, reading unpaired spawn
    tool_uses forward across assistant messages and pending
    ``SubtaskSpawned`` events in event-log order. The result for a
    single-child wake comes from ``wake_event.result`` (carried on the
    wake by :class:`ChildLifecycleObserver`), NOT from
    ``governance.subtask_results[-1]`` — using ``[-1]`` mis-pairs when a
    parent has multiple children and they complete out of spawn order
    (the last-completed result gets attached to whichever spawn the
    reversed scan hits first).
    """
    if isinstance(wake_event, SubtaskCompleted):
        call_id = _call_id_for_subtask(engine, task, wake_event.subtask_id)
        if call_id is None:
            return task  # already paired (idempotent)
        result = wake_event.result
        if result is None:
            # Defensive: wake event arrived without a result attached.
            # Fall back to looking up the result keyed by subtask_id on
            # the parent stream (same map the group branch builds),
            # instead of blindly taking subtask_results[-1].
            result = _lookup_result(engine, task, wake_event.subtask_id)
        return engine.append_subagent_result_message(
            task,
            call_id=call_id,
            output=result.output,
            success=result.status == "completed",
            error=result.error,
            lease_id=lease_id,
        )
    if isinstance(wake_event, SubtaskGroupCompleted):
        call_ids = _pending_spawn_call_ids(task, len(wake_event.subtask_ids))
        if not call_ids:
            return task  # already paired
        return engine.append_subagent_group_result_messages(
            task, wake_event, call_ids, lease_id=lease_id
        )
    return task


def _call_id_for_subtask(
    engine: Any, task: Any, subtask_id: str
) -> Optional[str]:
    """Return the unpaired spawn ``call_id`` that corresponds to
    ``subtask_id``, or ``None`` when every pending spawn is already
    paired (idempotent re-entry).

    Pairs forward-ordered unpaired spawn tool_uses with forward-ordered
    pending ``SubtaskSpawned`` events (foreground spawns only — background
    sub-agents emit ``BackgroundSubagentStarted`` and never reach this
    wake path). The target subtask's index into the spawned-ids list
    selects the call_id; this matches the positional pairing the group
    branch and the in-request :func:`subtask_drain._resume_parent_leased`
    already rely on, but keyed on the specific ``subtask_id`` so result
    delivery out of spawn order cannot mis-pair.
    """
    unpaired = _all_unpaired_spawn_call_ids(task)
    if not unpaired:
        return None
    # If there's exactly one unpaired spawn, that's it (the common case).
    if len(unpaired) == 1:
        return unpaired[0]
    # Multiple unpaired spawns (defensive: e.g. a parent recovering after
    # a crash between note_woken and append, or a future policy that fans
    # out single-spawn decisions without going through
    # SpawnSubtasksDecision): order pending foreground SubtaskSpawned
    # events by sequence and pick the call_id at the same index as
    # subtask_id.
    spawned_ids = _pending_subtask_spawn_ids(engine, task, len(unpaired))
    try:
        idx = spawned_ids.index(subtask_id)
    except ValueError:
        return None
    if idx >= len(unpaired):
        return None
    return unpaired[idx]


def _all_unpaired_spawn_call_ids(task: Any) -> list[str]:
    """All unpaired spawn-subagent/run_workflow call_ids on ``task`` in
    forward (member/spawn) order — no length check. Used by the single-
    subtask wake path so we can detect the number of pending spawns
    without raising when counts don't match exactly."""
    resolved: set[str] = set()
    for msg in task.runtime.messages:
        if msg.role == "tool":
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    resolved.add(block.call_id)
    unpaired: list[str] = []
    for msg in task.runtime.messages:
        if msg.role != "assistant":
            continue
        for block in msg.content:
            if (
                isinstance(block, ToolUseBlock)
                and block.tool_name in _SPAWN_TOOL_NAMES
                and block.call_id not in resolved
            ):
                unpaired.extend([block.call_id] * _spawn_member_count(block))
    return unpaired


def _pending_subtask_spawn_ids(
    engine: Any, task: Any, n_pending: int
) -> list[str]:
    """The last ``n_pending`` foreground ``SubtaskSpawned`` subtask_ids
    for ``task``, in forward event order.

    We reconstruct this from the event log because
    ``GovernanceState`` only tracks a ``spawned_subtasks`` counter + flat
    ``subtask_results`` list — neither is keyed by subtask_id in a way
    that survives out-of-order completion. We deliberately do NOT add a
    new persistent mapping to GovernanceState; the event log is the
    source of truth and this helper reads it the same way
    :meth:`Engine.append_subagent_group_result_messages` does.
    """
    event_log = getattr(engine, "_event_log", None)
    if event_log is None:
        return []
    spawned: list[str] = []
    for env in event_log.read(task.task_id):
        if env.type == "SubtaskSpawned":
            spawned.append(env.payload.subtask_id)
    if n_pending >= len(spawned):
        return spawned
    return spawned[-n_pending:]


def _lookup_result(engine: Any, task: Any, subtask_id: str) -> Any:
    """Find a SubtaskResult by subtask_id on the parent stream.

    Used only as a defensive fallback when ``wake_event.result`` is None;
    the ChildLifecycleObserver always attaches the result, so this path
    is exercised only by tests that synthesise wake events or by future
    wake producers that omit it. Mirrors the result map
    :meth:`Engine.append_subagent_group_result_messages` builds.
    """
    event_log = getattr(engine, "_event_log", None)
    if event_log is not None:
        for env in event_log.read(task.task_id):
            if (
                env.type == "SubtaskCompleted"
                and env.payload.subtask_id == subtask_id
            ):
                return env.payload.result
    # Fallback: scan governance.subtask_results. Must NOT use [-1]
    # blindly. SubtaskResult does not carry the child's subtask_id
    # (protocol limitation), so we cannot key-match here; return the
    # last result as a last resort, but document the risk.
    results = getattr(task.governance, "subtask_results", [])
    if results:
        return results[-1]
    raise RuntimeError(
        f"worker: no SubtaskResult found for subtask {subtask_id!r}"
    )


def _pending_spawn_call_id(task: Any) -> Optional[str]:
    """The ``call_id`` of the most recent unpaired spawn tool_use, or
    ``None`` if every spawn on the transcript is already paired (idempotent
    re-entry after a crash between note_woken and append)."""
    resolved: set[str] = set()
    for msg in task.runtime.messages:
        if msg.role == "tool":
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    resolved.add(block.call_id)
    for msg in reversed(task.runtime.messages):
        if msg.role != "assistant":
            continue
        for block in msg.content:
            if (
                isinstance(block, ToolUseBlock)
                and block.tool_name in _SPAWN_TOOL_NAMES
                and block.call_id not in resolved
            ):
                return block.call_id
    return None


def _spawn_member_count(block: Any) -> int:
    arguments = getattr(block, "arguments", None)
    if isinstance(arguments, dict):
        raw = arguments.get("spawns")
        if isinstance(raw, (list, tuple)) and raw:
            return len(raw)
    return 1


def _pending_spawn_call_ids(task: Any, n: int) -> list[str]:
    """The ``n`` unpaired spawn member call_ids, in member order. Returns
    the empty list when all ``n`` members are already paired (idempotent
    re-entry)."""
    resolved: set[str] = set()
    for msg in task.runtime.messages:
        if msg.role == "tool":
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    resolved.add(block.call_id)
    unpaired: list[str] = []
    for msg in task.runtime.messages:
        if msg.role != "assistant":
            continue
        for block in msg.content:
            if (
                isinstance(block, ToolUseBlock)
                and block.tool_name in _SPAWN_TOOL_NAMES
                and block.call_id not in resolved
            ):
                unpaired.extend([block.call_id] * _spawn_member_count(block))
    if len(unpaired) < n:
        return []  # already paired
    if len(unpaired) != n:
        raise RuntimeError(
            f"worker: expected {n} unpaired spawn call_ids on parent, "
            f"found {len(unpaired)}"
        )
    return unpaired


def run_leased_task(
    rt: WorkerRuntime,
    lease: Any,
    *,
    prelude: Optional[WokenPrelude] = None,
    next_goal_handle: Optional[str] = None,
    reliability_sink: Optional[ReliabilitySink] = None,
    engine: Optional[Any] = None,
) -> WorkerOutcome:
    """Advance one already-leased task by one step (the 3-state machine).

    * ``"woken"`` — the lease carried a ``wake_event`` →
      ``engine.note_woken`` → optional ``prelude`` → ``run_one_step``, then
      release (consuming the wake).
    * ``"skipped"`` — folded task is suspended but no ``wake_event``
      arrived (a diagnostic symptom: the task is simply still waiting; H2
      makes wake delivery exactly-once, so this is no longer a loss path)
      → re-release ``suspended`` preserving ``wake_on``.
    * ``"drained"`` — pending / running → ``run_one_step``, then release.
      A ``running`` drain first scans for an interrupted attempt (an
      opening-turn crash leaves one with no ``TaskWoken`` at all) and
      routes it through the same seal + re-drive-or-park recovery as the
      woken path — never a silent re-drive on a dirty window.
    * ``"stopped"`` also covers a recovery **park**: the interrupted
      attempt was sealed and the task rests suspended on the next-goal
      handle with an ``origin="system"`` notice — typing resumes it.

    ``reliability_sink`` (the WorkerLoop threads its own) observes the
    recovery moments (``attempt_abandoned`` / ``attempt_parked``); ``None``
    (driver / test callers) degrades to logs.

    ``engine`` overrides the per-task resolve: the driver's seed passes the
    Engine it resolved BEFORE applying a seed-time prelude (D6), so a
    seed-written ``ModelBound`` keeps its drive-the-next-turn semantics.
    ``None`` (every other caller) resolves as before.

    ``prelude`` (D4) is the typed woken-command-prelude seam: a
    step run **after** ``note_woken`` and **before** ``run_one_step`` (the
    H2 first-consume window). ``None`` is the daemon worker-loop's plain
    woken branch; ``AppendMessagePrelude`` / ``ResolveApprovalPrelude`` are
    the CLI/web ``send_goal`` / approval commands. The prelude only runs on
    the first-consume case — a re-delivered wake whose ``TaskWoken`` is
    already durable (H2 cases 2–4) reconciles by folded status and never
    re-runs the prelude (the command's bytes are already recorded).

    Single source of truth for the resume machine: the daemon
    :class:`WorkerLoop` and the in-process ``CodeSessionRunner`` both route
    through here so their semantics cannot drift.
    """
    task = fold(rt.event_log, rt.content_store, lease.task_id)
    # D1: drive ``task`` with ITS OWN Agent's Engine, not a fixed
    # ``rt.engine``. The resolver folds ``TaskCreated.agent_name`` (hard
    # error on an unknown agent at lease time); a single-Agent host returns
    # its one Engine. A seed-pinned ``engine`` (see above) wins.
    if engine is None:
        engine = resolve_engine(rt, task)
    # Human stop, top-level turn: poll the host's process-local cancel
    # registry at every turn boundary so a cancel/close that lands while THIS
    # session's ReAct loop is mid-flight abandons the in-flight result (the
    # same cooperative-cancel the delegation drain already binds for children).
    # Only the SDK host exposes ``is_cancelled``; a bare WorkerRuntime double
    # ⇒ ``None`` ⇒ no poll, byte-identical to before. ``lease.task_id`` IS the
    # tree root on this top-level path, matching what ``cancel``/``close`` mark.
    cancelled = _cancel_predicate(rt, lease.task_id)
    try:
        if lease.wake_event is not None:
            return _run_woken(
                rt,
                lease,
                task,
                engine,
                prelude=prelude,
                cancelled=cancelled,
                reliability_sink=reliability_sink,
            )
        if task.status == "suspended":
            rt.dispatcher.release(
                lease.lease_id, next_state="suspended", wake_on=task.wake_on
            )
            return "skipped"
        if task.status == "running":
            # An opening-turn crash (no ``TaskWoken`` exists yet) leaves an
            # interrupted attempt on a wake-less lease. Scan before
            # stepping — running the step directly on the dirty window
            # would silently re-drive it. A trailing park-reason seal is a
            # crashed park: its "do not re-drive" decision is durable, so
            # complete the park instead of stepping over it.
            events = rt.event_log.read(lease.task_id)
            pending = scan_pending_park(events)
            if pending is not None:
                return _complete_pending_park(
                    rt,
                    lease,
                    engine,
                    events,
                    pending,
                    consumed=None,
                    reliability_sink=reliability_sink,
                )
            attempt = scan_interrupted_attempt(events)
            if attempt is not None:
                return _recover_interrupted_attempt(
                    rt,
                    lease,
                    engine,
                    events,
                    attempt,
                    cancelled=cancelled,
                    consumed=None,
                    outcome="drained",
                    reliability_sink=reliability_sink,
                )
        # Subtask goal seeding: a foreground child claimed by a worker before
        # the parent's delegation drain (_descend_to_child) could seed its
        # goal has an empty runtime.messages. Without this the child sends
        # an empty ``input`` to the provider and gets a 400. Detect the
        # condition (has parent, no messages yet, carries a goal) and inject
        # the goal as the opening user message — mirroring what
        # _descend_to_child does, so the child is well-formed regardless of
        # which worker picks it up.
        if (
            task.parent_task_id is not None
            and not task.runtime.messages
            and task.state.goal
        ):
            task = engine.append_user_message(
                task,
                content=[TextBlock(text=task.state.goal)],
                lease_id=lease.lease_id,
            )
        task = engine.run_one_step(task, lease_id=lease.lease_id, cancelled=cancelled)
        rt.dispatcher.release(
            lease.lease_id, next_state=task.status, wake_on=task.wake_on
        )
        return "drained"
    except TaskCancellationRequested:
        return _settle_stopped_turn(
            rt, lease, engine, next_goal_handle=next_goal_handle
        )


def _cancel_predicate(rt: WorkerRuntime, task_id: str) -> Optional[Callable[[], bool]]:
    """Bind a cooperative-cancel poll off the host's cancel registry.

    Returns ``None`` when the host has no ``is_cancelled`` seam (a bare
    ``WorkerRuntime`` test double), so the Engine never polls and recordings
    stay byte-identical to the pre-stop path.
    """
    is_cancelled = getattr(rt, "is_cancelled", None)
    if not callable(is_cancelled):
        return None
    return lambda: bool(is_cancelled(task_id))


def _settle_stopped_turn(
    rt: WorkerRuntime,
    lease: Any,
    engine: Any,
    *,
    next_goal_handle: Optional[str],
) -> WorkerOutcome:
    """Land a top-level turn whose ReAct loop a human stopped mid-flight.

    The loop raised :class:`TaskCancellationRequested` (a cancel/close marked
    the registry; the in-flight result is abandoned with no assistant message
    / no tool run). Re-fold to read which control event the human action wrote
    and settle accordingly:

    * ``terminal`` — ``cancel`` already wrote ``TaskCancelled``; release the
      lease terminal. The conversation is dead (not reopenable).
    * otherwise — ``close`` (or a bare stop): suspend on ``next_goal_handle``
      so a later ``send_goal`` matching it resumes the conversation, then
      release the lease ``suspended``. Reopenable by simply typing again.

    No fold-ordering race: ``cancel`` writes its durable ``TaskCancelled``
    BEFORE marking the registry, so by the time the poll trips and we re-fold
    the terminal is always already visible. ``next_goal_handle is None`` (the
    daemon worker / test seams that don't pass one) ⇒ release terminal, since
    there is no resumable landing to synthesize.
    """
    task = fold(rt.event_log, rt.content_store, lease.task_id)
    consumed = lease.wake_event
    if task.status == "terminal" or next_goal_handle is None:
        rt.dispatcher.release(
            lease.lease_id,
            next_state="terminal",
            consumed_wake_event=consumed,
        )
        _discard_cancellation(rt, lease.task_id)
        return "cancelled"
    task = suspend_on_human_handle(
        engine, task, handle=next_goal_handle, lease_id=lease.lease_id
    )
    rt.dispatcher.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=task.wake_on,
        consumed_wake_event=consumed,
    )
    _discard_cancellation(rt, lease.task_id)
    return "stopped"


def _discard_cancellation(rt: WorkerRuntime, task_id: str) -> None:
    """Drop ``task_id``'s registry mark once a stopped turn has settled, so a
    later resumed turn on the same task isn't pre-aborted by a stale mark.
    No-op on hosts without the seam."""
    discard = getattr(rt, "discard_cancellation", None)
    if callable(discard):
        discard(task_id)


def _run_woken(
    rt: WorkerRuntime,
    lease: Any,
    task: Any,
    engine: Any,
    *,
    prelude: Optional[WokenPrelude] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    reliability_sink: Optional[ReliabilitySink] = None,
) -> WorkerOutcome:
    """H2 (D4) — the latest-matching-`TaskWoken` recovery state
    machine. ``task`` is the freshly folded task; ``lease.wake_event`` is the
    matched wake (re-)delivered by the dispatcher. Exactly-once: the wake is
    consumed once (case 1) or its already-durable consumption is reconciled
    without a second ``TaskWoken`` (cases 2′–4) — each consuming release
    passes ``consumed_wake_event`` so the dispatcher clears the matched
    event (D2/D6). The ``running`` reconciliation keys on the attempt
    sentinel (a ``ContextPlanComposed`` in the live window — see
    ``noeta.runtime.attempt``): none ⇒ bare re-drive (case 2′), present ⇒
    the H1 partial-step orphan goes through seal + re-drive-or-park
    recovery (case 5′, docs/adr/step-attempt-recovery.md). Case 6 = fail
    loud.
    """
    events = rt.event_log.read(lease.task_id)
    matching = _find_matching_woken_index(events, lease.wake_event)
    if matching == -2:  # no suspension this wake satisfies → case 6
        raise WakeRecoveryError(
            f"woken lease for task {lease.task_id!r} has no matching "
            "TaskSuspended (wake cannot be reconciled)"
        )

    if matching is None:
        # case 1 — first consume (must be a fresh, still-suspended window).
        if task.status != "suspended":
            raise WakeRecoveryError(
                f"task {lease.task_id!r}: no matching TaskWoken but status "
                f"is {task.status!r} (expected suspended for first consume)"
            )
        task = engine.note_woken(
            task, lease_id=lease.lease_id, wake_event=lease.wake_event
        )
        # Subtask-completion prelude: render paired tool_result(s) for
        # SubtaskCompleted / SubtaskGroupCompleted wakes so the step that
        # follows sees balanced history. Non-subtask wakes are a no-op.
        # Rides the first-consume window, same position the in-request
        # subtask_drain renders them.
        task = _render_subtask_wake(
            engine, task, lease.wake_event, lease_id=lease.lease_id
        )
        # Woken-command-prelude seam (D4): the per-command step
        # between TaskWoken and run_one_step (append-message / resolve-approval
        # / none). Rides this first-consume window so its bytes land exactly
        # where the old inline CLI path recorded them.
        if prelude is not None:
            task = prelude(engine, task, lease_id=lease.lease_id)
        task = engine.run_one_step(task, lease_id=lease.lease_id, cancelled=cancelled)
        rt.dispatcher.release(
            lease.lease_id,
            next_state=task.status,
            wake_on=task.wake_on,
            consumed_wake_event=lease.wake_event,
        )
        return "woken"

    # A matching TaskWoken is already durable — reconcile by folded status.
    if task.status == "terminal":  # case 3 — step already finished
        rt.dispatcher.release(
            lease.lease_id,
            next_state="terminal",
            consumed_wake_event=lease.wake_event,
        )
        return "woken"
    if task.status == "suspended":  # case 4 — step re-suspended on new wake_on
        rt.dispatcher.release(
            lease.lease_id,
            next_state="suspended",
            wake_on=task.wake_on,
            consumed_wake_event=lease.wake_event,
        )
        return "woken"
    if task.status == "running":
        pending = scan_pending_park(events)
        if pending is not None:
            # a crash hit the ORIGINAL park between its seal and its
            # suspend: the seal's park reason is a durable "do not
            # re-drive" decision — finish the park, never the bare step.
            return _complete_pending_park(
                rt,
                lease,
                engine,
                events,
                pending,
                consumed=lease.wake_event,
                reliability_sink=reliability_sink,
            )
        attempt = scan_interrupted_attempt(events)
        if attempt is None:
            # case 2′ — the wake is durably consumed but no attempt started
            # (no ``ContextPlanComposed`` in the live window): run the bare
            # step. Correct by construction for every caller: timer /
            # subtask re-deliveries (nothing to re-derive), seeded command
            # wakes whose prelude events were written durably at seed time
            # (D6 — they precede the first attempt and fold into ``task``),
            # and a crash between an ``auto_redrive`` seal and its re-drive
            # (the seal already re-based the state; park-reason seals never
            # reach here — ``scan_pending_park`` above completes the park
            # instead). Subtask wakes re-render their paired tool_result
            # idempotently (a crash between note_woken and the first
            # compose would otherwise strand the spawn tool_use).
            # The one remaining loss mode is an approval
            # resolution whose prelude stays drive-side (it executes the
            # approved tool, so it cannot ride the request thread): a crash
            # before it lands re-suspends on the same approval and the
            # operator simply approves again — benign, documented in
            # docs/adr/step-attempt-recovery.md.
            task = _render_subtask_wake(
                engine, task, lease.wake_event, lease_id=lease.lease_id
            )
            task = engine.run_one_step(
                task, lease_id=lease.lease_id, cancelled=cancelled
            )
            rt.dispatcher.release(
                lease.lease_id,
                next_state=task.status,
                wake_on=task.wake_on,
                consumed_wake_event=lease.wake_event,
            )
            return "woken"
        # case 5′ — an interrupted attempt after the wake (the H1
        # partial-step orphan): seal + re-drive or park.
        return _recover_interrupted_attempt(
            rt,
            lease,
            engine,
            events,
            attempt,
            cancelled=cancelled,
            consumed=lease.wake_event,
            outcome="woken",
            reliability_sink=reliability_sink,
        )
    raise WakeRecoveryError(  # case 6 — unexpected status
        f"task {lease.task_id!r}: woken lease in unexpected status {task.status!r}"
    )


def _recover_interrupted_attempt(
    rt: WorkerRuntime,
    lease: Any,
    engine: Any,
    events: list[Any],
    attempt: InterruptedAttempt,
    *,
    cancelled: Optional[Callable[[], bool]],
    consumed: Any,
    outcome: WorkerOutcome,
    reliability_sink: Optional[ReliabilitySink],
) -> WorkerOutcome:
    """Seal an interrupted attempt, then re-drive or park
    (docs/adr/step-attempt-recovery.md).

    Classify (D2: "whatever could run without a human approval gate may be
    re-driven without a human") → seal (D3/D4: ``StepAttemptAbandoned``
    with the pre-attempt baseline, written under THIS lease) → either
    re-drive the step in the same lease, or park the task as a stopped
    conversation (D7: system notice + next-goal suspend — typing resumes
    it, ``close``/``cancel`` end it, zero new verbs). ``consumed`` is the
    wake to clear on release (``None`` on the drained path). The seal is
    durable before either continuation, so a crash *during* recovery
    re-enters as a bare case-2′ re-drive (after an ``auto_redrive`` seal),
    as a park completion (after a park-reason seal — ``scan_pending_park``),
    or as a fresh case 5′ (before the seal) — recovery recurses naturally,
    and :data:`~noeta.runtime.attempt.ABANDON_CAP` consecutive seals in one
    window force a park (D8).
    """
    # Baseline = the state as it stood just BEFORE the interrupted
    # attempt's ``ContextPlanComposed`` (D4: completed attempts and the
    # turn's prelude events stay live history; only the interrupted
    # attempt dies). Same bounded-fold machinery as the conversation
    # rewind. Classification runs against this SAME baseline, not the
    # dirty full stream: the D2 question is whether the re-drive — which
    # runs on the sealed state — could proceed unattended, and folding the
    # interrupted window into the Budget / Repetition counters could park
    # an attempt the re-drive itself would allow.
    bounded = BoundedEventLog(events, attempt.attempt_start_seq - 1)
    baseline = fold(bounded, rt.content_store, lease.task_id)
    classification = classify_attempt(
        attempt.tail,
        engine=engine,
        task=baseline,
        content_store=rt.content_store,
        event_log=bounded,
    )
    capped = attempt.abandon_count >= ABANDON_CAP
    # A plan-less anchor is an interrupted approval execution: a human is
    # in that loop by definition, so recovery never re-drives it — the seal
    # restores the pending-approval state and the park below re-suspends on
    # the approval's own handle, so the ordinary approve verb re-runs it.
    # Its reason wins over the cap: the park notice must tell the operator
    # a human-approved call was interrupted, whatever the seal count.
    redrive = classification.safe and not capped and attempt.anchored_on_plan
    if not attempt.anchored_on_plan:
        reason = "interrupted_approval"
    elif capped:
        reason = "abandon_cap"
    elif redrive:
        reason = "auto_redrive"
    else:
        reason = "unsafe_tool_activity"
    abandon_step_attempt(
        engine,
        lease.task_id,
        baseline=baseline,
        abandoned_from_seq=attempt.attempt_start_seq,
        reason=reason,
        lease_id=lease.lease_id,
    )
    # Re-fold: the seal is a snapshot-shaped baseline, so this is a cheap
    # rehydrate — and the ONE way to rebuild the working Task that is
    # byte-identical to what any later resume folds.
    task = fold(rt.event_log, rt.content_store, lease.task_id)
    if reliability_sink is not None:
        reliability_sink(
            ReliabilityEvent(
                kind="attempt_abandoned" if redrive else "attempt_parked",
                task_id=lease.task_id,
                lease_id=lease.lease_id,
                detail={
                    "reason": reason,
                    "abandoned_from_seq": attempt.attempt_start_seq,
                    "blockers": list(classification.blockers),
                },
            )
        )
    if redrive:
        _log.warning(
            "worker: sealed interrupted attempt at seq %s for task %s; "
            "re-driving (crash recovery)",
            attempt.attempt_start_seq,
            lease.task_id,
        )
        task = engine.run_one_step(task, lease_id=lease.lease_id, cancelled=cancelled)
        rt.dispatcher.release(
            lease.lease_id,
            next_state=task.status,
            wake_on=task.wake_on,
            consumed_wake_event=consumed,
        )
        return outcome
    _log.warning(
        "worker: sealed interrupted attempt at seq %s for task %s; "
        "PARKING for a human (%s: %s)",
        attempt.attempt_start_seq,
        lease.task_id,
        reason,
        ", ".join(classification.blockers) or "n/a",
    )
    task = engine.append_user_message(
        task,
        content=[TextBlock(text=_park_notice(reason, classification.blockers))],
        lease_id=lease.lease_id,
        origin="system",
    )
    task = suspend_on_human_handle(
        engine,
        task,
        handle=_park_handle(events, reason=reason, consumed=consumed),
        lease_id=lease.lease_id,
    )
    rt.dispatcher.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=task.wake_on,
        consumed_wake_event=consumed,
    )
    return "stopped"


def _park_handle(events: list[Any], *, reason: str, consumed: Any) -> str:
    """The handle a park suspends on (D7).

    An interrupted approval execution re-suspends on the approval's OWN
    handle — the seal restored the pending-approval state, so the ordinary
    approve/deny verbs re-run it; everything else rests as a stopped
    conversation on the next-goal handle (typing resumes it). The approval
    handle comes from the wake this lease consumed when it is a human
    response; on a wake-less re-entry (the drained path, or completing a
    crashed park) it falls back to the suspension the live window consumed
    — the stream's last ``TaskSuspended`` on a human handle.
    """
    if reason != "interrupted_approval":
        return NEXT_GOAL_WAKE_HANDLE
    if isinstance(consumed, HumanResponseReceived):
        return consumed.handle
    for env in reversed(events):
        if env.type == "TaskSuspended":
            wake_on = getattr(env.payload, "wake_on", None)
            if isinstance(wake_on, HumanResponseReceived):
                return wake_on.handle
            break
    return NEXT_GOAL_WAKE_HANDLE


def _complete_pending_park(
    rt: WorkerRuntime,
    lease: Any,
    engine: Any,
    events: list[Any],
    pending: PendingPark,
    *,
    consumed: Any,
    reliability_sink: Optional[ReliabilitySink],
) -> WorkerOutcome:
    """Finish a park whose seal is durable but whose completion is not.

    A crash hit the original park between its seal and its suspend
    (:func:`~noeta.runtime.attempt.scan_pending_park`). The seal's park
    ``reason`` is the durable decision; re-entering as a bare re-drive
    would override it, so this path only completes the remaining writes:
    the system notice (skipped when it already landed) and the suspend +
    release. Blockers for a re-appended notice are re-derived from the
    sealed dead tail — the seal payload records only the reason.
    """
    reason = pending.seal.payload.reason
    task = fold(rt.event_log, rt.content_store, lease.task_id)
    blockers: tuple[str, ...] = ()
    if not pending.notice_appended:
        if reason == "unsafe_tool_activity":
            blockers = _sealed_tail_blockers(rt, engine, events, pending.seal, task)
        task = engine.append_user_message(
            task,
            content=[TextBlock(text=_park_notice(reason, blockers))],
            lease_id=lease.lease_id,
            origin="system",
        )
    _log.warning(
        "worker: completing interrupted park for task %s (%s; a crash hit "
        "the original park before its suspend)",
        lease.task_id,
        reason,
    )
    task = suspend_on_human_handle(
        engine,
        task,
        handle=_park_handle(events, reason=reason, consumed=consumed),
        lease_id=lease.lease_id,
    )
    rt.dispatcher.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=task.wake_on,
        consumed_wake_event=consumed,
    )
    if reliability_sink is not None:
        reliability_sink(
            ReliabilityEvent(
                kind="attempt_parked",
                task_id=lease.task_id,
                lease_id=lease.lease_id,
                detail={
                    "reason": reason,
                    "resumed_park": True,
                    "abandoned_from_seq": (pending.seal.payload.abandoned_from_seq),
                    "blockers": list(blockers),
                },
            )
        )
    return "stopped"


def _sealed_tail_blockers(
    rt: WorkerRuntime,
    engine: Any,
    events: list[Any],
    seal: Any,
    task: Any,
) -> tuple[str, ...]:
    """Re-derive an already-sealed attempt's blockers for its park notice,
    from the dead tail ``[abandoned_from_seq, seal)`` — classified against
    the same pre-attempt baseline the original classification used."""
    from_seq = seal.payload.abandoned_from_seq
    tail = tuple(e for e in events if from_seq <= e.seq < seal.seq)
    bounded = BoundedEventLog(events, from_seq - 1)
    return classify_attempt(
        tail,
        engine=engine,
        task=task,
        content_store=rt.content_store,
        event_log=bounded,
    ).blockers


def _park_notice(reason: str, blockers: tuple[str, ...]) -> str:
    """The ``origin="system"`` notice a park appends — read by the human in
    the web UI while the task rests, and by the model when the conversation
    resumes, so both can verify what may have partially applied."""
    if reason == "abandon_cap":
        cause = (
            f"automatic recovery was already retried {ABANDON_CAP} times in this turn"
        )
    elif reason == "interrupted_approval":
        return (
            "A worker crash interrupted this task while it was executing a "
            "human-approved tool call"
            + (f" ({', '.join(blockers)})" if blockers else "")
            + ". The interrupted execution was set aside and the task is "
            "waiting on the same approval again. Verify whether the call "
            "partially applied before approving it a second time."
        )
    else:
        cause = "it had already run operations with side effects: " + ", ".join(
            blockers
        )
    return (
        "A worker crash interrupted this task mid-step. The interrupted "
        f"attempt was set aside without re-running because {cause}. "
        "Before continuing, verify whether the listed operations applied "
        "fully, partially, or not at all — they must not be blindly redone."
    )


class _HeartbeatRunner:
    """Side-thread that extends a lease while a step runs (3A D2).

    Loops ``wait(interval)`` → ``dispatcher.heartbeat(lease_id,
    lease_seconds)`` until stopped. ``wait`` returns ``True`` to stop
    (clean interrupt) or ``False`` on timeout (do one heartbeat); it
    defaults to a ``threading.Event.wait`` so :meth:`stop` interrupts
    immediately and a fast step never incurs a real sleep. Tests inject
    a scripted ``wait`` to drive an exact number of heartbeats with no
    real timing.

    If ``heartbeat`` raises ``InvalidLease`` (lease reclaimed, or the
    Dispatcher's ``heartbeat_max`` cap hit) the runner logs + stops. It
    makes NO claim about the task's resulting state — the in-flight
    step's eventual EventLog write will raise ``InvalidLease`` too,
    handled by the worker exception policy (D7). ``heartbeat_interval *
    heartbeat_max`` is the max keepalive window per step; past it is an
    operational-failure path, not a recovery path (3A adds no cap-hit
    recovery).
    """

    def __init__(
        self,
        dispatcher: Any,
        lease: Any,
        *,
        interval: float,
        lease_seconds: float,
        wait: Optional[Callable[[float], bool]] = None,
        reliability_sink: Optional[ReliabilitySink] = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._lease = lease
        self._interval = interval
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._wait = wait if wait is not None else self._stop.wait
        self._thread: Optional[threading.Thread] = None
        self._sink = reliability_sink

    def _loop(self) -> None:
        while not self._wait(self._interval):
            try:
                self._dispatcher.heartbeat(
                    self._lease.lease_id, lease_seconds=self._lease_seconds
                )
            except InvalidLease:
                _log.warning(
                    "worker: heartbeat for lease %s (task %s) hit "
                    "InvalidLease; stopping heartbeat (no claim about "
                    "task state)",
                    self._lease.lease_id,
                    self._lease.task_id,
                )
                if self._sink is not None:
                    # Symptom only — the cause (cap / expired / requeued /
                    # already-released) is not knowable from InvalidLease.
                    self._sink(
                        ReliabilityEvent(
                            kind="heartbeat_invalid_lease",
                            task_id=getattr(self._lease, "task_id", None),
                            lease_id=getattr(self._lease, "lease_id", None),
                        )
                    )
                return

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="noeta-worker-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


@contextmanager
def keep_lease_alive(
    dispatcher: Any,
    lease: Any,
    *,
    interval: float = 30.0,
    lease_seconds: float = 600.0,
    reliability_sink: Optional[ReliabilitySink] = None,
) -> Iterator[None]:
    """Renew ``lease`` via a :class:`_HeartbeatRunner` for the duration of a
    synchronously-driven leased step.

    The in-request transports (``InteractionDriver.drive_seeded`` and the
    delegation drain in ``subtask_drain``) run a leased step on the request /
    background thread with **no resident** :class:`WorkerLoop` to start a
    heartbeat. A step that outlasts ``lease_seconds`` — e.g. a slow LLM
    round-trip retried to its budget (~5×300 s ≫ the 600 s lease) — would
    otherwise lose its lease mid-flight, so its own terminal write fails
    ``is_lease_valid`` (``InvalidLease``) and the task hangs non-terminal.
    Wrapping the step keeps the lease renewed until it returns.

    Past the dispatcher's ``heartbeat_max`` keepalive window the heartbeat
    stops and a subsequent write raises ``InvalidLease`` — the same
    operational-failure boundary the WorkerLoop path has; the caller's
    InvalidLease handling owns it from there. ``interval <= 0`` disables the
    heartbeat (a test seam / opt-out), byte-identical to the pre-heartbeat
    path.
    """
    if interval <= 0:
        yield
        return
    runner = _HeartbeatRunner(
        dispatcher,
        lease,
        interval=interval,
        lease_seconds=lease_seconds,
        reliability_sink=reliability_sink,
    )
    runner.start()
    try:
        yield
    finally:
        runner.stop()


class WorkerLoop:
    """Resident loop: lease a ready task, run it one step, repeat.

    Worker exception policy (3A D7) — a daemon must not crash on a
    poisoned task:

    * :class:`noeta.protocols.errors.InvalidLease` — the lease is no
      longer ours (reclaimed by a stale-sweep, or a future heartbeat cap
      hit). Log + continue; do NOT ``release`` / ``fail``. Make no claim
      about the task's resulting state.
    * Any other exception (policy / tool bug, provider error leaking) —
      ``dispatcher.fail(lease_id, retryable=True, reason=...)``: bounded
      retry up to the backend's ``max_fail_attempts``, then terminal.
    * If ``fail()`` itself raises (lease already gone) — log + continue.
    * The loop always proceeds to the next task.

    The loop also runs a per-step heartbeat side-thread (keeps a slow
    step's lease alive), a periodic stale-sweep, and a periodic timer
    poll (the ``TimerFired`` producer for ``wait_timer`` suspends), and
    supports best-effort signal-driven graceful shutdown via
    :func:`install_stop_signals` / ``run_forever(install_signals=True)``.
    """

    def __init__(
        self,
        rt: WorkerRuntime,
        *,
        worker_id: str = "noeta-worker",
        lease_seconds: float = 600.0,
        poll_interval: float = 0.5,
        heartbeat_interval: float = 30.0,
        stale_sweep_interval: float = 10.0,
        timer_poll_interval: float = 1.0,
        shutdown_grace_s: Optional[float] = DEFAULT_SHUTDOWN_GRACE_S,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        now_fn: Optional[Callable[[], float]] = None,
        heartbeat_wait: Optional[Callable[[float], bool]] = None,
        reliability_sink: Optional[ReliabilitySink] = None,
        step_poll_s: float = 0.05,
        next_goal_handle: Optional[str] = None,
    ) -> None:
        self._rt = rt
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        self._stale_sweep_interval = stale_sweep_interval
        self._timer_poll_interval = timer_poll_interval
        #: next_goal_handle — passed to run_leased_task so a human close /
        #: cancel (non-terminal) suspends the task on the next-goal handle
        #: (reopenable by typing again). ``None`` selects the daemon-CLI
        #: behaviour where a human stop releases terminal.
        self._next_goal_handle = next_goal_handle
        # H1: bounded process-shutdown. After stop(), an in-flight step is
        # waited for up to this many seconds, then ABANDONED (the step runs
        # on a daemon thread we stop waiting on) and the loop returns. A
        # value of ``None`` / ``<= 0`` selects the old unbounded wait.
        self._shutdown_grace_s = shutdown_grace_s
        # Injectable so tests never wall-clock wait.
        if sleep is None:
            import time

            sleep = time.sleep
        if clock is None:
            import time

            clock = time.monotonic
        # The timer poll compares against ``TimerFired.fire_at``, which the
        # Engine computed with a WALL clock (``time.time``, epoch seconds).
        # Keep it separate from the loop's monotonic cadence ``clock`` —
        # mixing the two bases would fire timers at the wrong moment.
        if now_fn is None:
            import time

            now_fn = time.time
        self._sleep = sleep
        self._clock = clock
        self._now_fn = now_fn
        # Optional injected heartbeat wait (tests drive exact heartbeat
        # counts); None → each runner uses its own Event.wait.
        self._heartbeat_wait = heartbeat_wait
        # Process-local reliability sink (NOT EventLog). Default logs.
        self._reliability_sink: ReliabilitySink = (
            reliability_sink or _default_reliability_sink
        )
        self._step_poll_s = step_poll_s
        self._running = True
        # Set when a shutdown grace elapsed with a step still in flight.
        # The serve/CLI host MUST treat this as process-shutdown (exit);
        # the abandoned step thread may still be running.
        self._abandoned = False
        self._last_sweep = clock()
        self._last_timer_poll = clock()

    def stop(self) -> None:
        """Signal the loop to stop after the current iteration. (I3 wires
        signal handlers onto this.)"""
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def abandoned(self) -> bool:
        """True once a shutdown grace elapsed with a step still in flight.
        The host MUST treat this as process-shutdown (exit the process):
        the abandoned step thread may still run + write the EventLog, so
        reusing this loop/runtime in-process is unsupported."""
        return self._abandoned

    def _emit(self, event: ReliabilityEvent) -> None:
        try:
            self._reliability_sink(event)
        except Exception:  # noqa: BLE001 — observability must never break the loop
            _log.exception("worker: reliability_sink raised; continuing")

    def tick(self) -> bool:
        """Lease one ready task and advance it one step.

        Returns ``True`` if a task was leased (and processed, success or
        handled-failure), ``False`` if the ready queue was empty. The
        exception policy is applied here so callers never see a step
        failure propagate.
        """
        lease = self._rt.dispatcher.lease(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            task_id=None,
        )
        if lease is None:
            return False
        self._run_one(lease)
        return True

    def maybe_sweep(self) -> bool:
        """Run `requeue_stale` if `stale_sweep_interval` has elapsed.

        Returns True if a sweep ran. Cadence is measured with the
        injected `clock` so tests drive it without real time.
        """
        if self._stale_sweep_interval <= 0:
            return False
        if self._clock() - self._last_sweep < self._stale_sweep_interval:
            return False
        try:
            requeued = self._rt.dispatcher.requeue_stale()
            if requeued:
                self._emit(
                    ReliabilityEvent(
                        kind="stale_requeued",
                        detail={"count": len(requeued), "task_ids": list(requeued)},
                    )
                )
        except Exception:  # noqa: BLE001 — sweep failure must not crash the loop
            _log.exception("worker: requeue_stale failed; continuing")
        self._last_sweep = self._clock()
        return True

    def maybe_poll_timers(self) -> bool:
        """Run ``fire_due_timers`` if ``timer_poll_interval`` has elapsed.

        Returns True if a poll ran. Cadence is measured with the
        injected monotonic ``clock`` (like :meth:`maybe_sweep`); the
        due-check itself uses the injected wall-clock ``now_fn`` — the
        same time base the Engine used to compute ``TimerFired.fire_at``.
        A dispatcher without ``fire_due_timers`` (a pre-timer external
        adapter) is skipped: the poll then degrades to a no-op instead
        of crashing the loop.
        """
        if self._timer_poll_interval <= 0:
            return False
        if self._clock() - self._last_timer_poll < self._timer_poll_interval:
            return False
        fire = getattr(self._rt.dispatcher, "fire_due_timers", None)
        if fire is not None:
            try:
                fired = fire(now=self._now_fn())
                if fired:
                    self._emit(
                        ReliabilityEvent(
                            kind="timers_fired",
                            detail={
                                "count": len(fired),
                                "task_ids": list(fired),
                            },
                        )
                    )
            except Exception:  # noqa: BLE001 — poll failure must not crash the loop
                _log.exception("worker: fire_due_timers failed; continuing")
        self._last_timer_poll = self._clock()
        return True

    def _run_one(self, lease: Any) -> None:
        """Drive one leased task on a daemon **step thread** so the loop
        can impose a shutdown deadline on it (H1).

        Normal path: the loop waits for the step thread to finish (so
        ``tick()`` is synchronous from the caller's view), then returns.
        Shutdown path: if ``stop()`` was signalled and the step does not
        finish within ``shutdown_grace_s``, the loop **abandons** it —
        stops the heartbeat (so the lease expires → ``requeue_stale``
        reclaims after the process exits), emits ``shutdown_abandoned``,
        sets :attr:`abandoned`, and returns WITHOUT releasing/failing the
        lease (it no longer owns the outcome). The abandoned daemon thread
        may still run; recovery depends on the process actually exiting.
        """
        heartbeat: Optional[_HeartbeatRunner] = None
        if self._heartbeat_interval > 0:
            heartbeat = _HeartbeatRunner(
                self._rt.dispatcher,
                lease,
                interval=self._heartbeat_interval,
                lease_seconds=self._lease_seconds,
                wait=self._heartbeat_wait,
                reliability_sink=self._reliability_sink,
            )
            heartbeat.start()
        done = threading.Event()
        step = threading.Thread(
            target=self._execute_step,
            args=(lease, done),
            name="noeta-worker-step",
            daemon=True,
        )
        step.start()
        finished = self._wait_for_step(done)
        # Always stop the heartbeat after the wait: on the normal finish
        # path it is cleanup; on the abandon path it lets the lease expire
        # so ``requeue_stale`` can reclaim once the process exits.
        if heartbeat is not None:
            heartbeat.stop()
        if finished:
            return
        # Abandoned: emit the symptom + mark for process-shutdown. Do NOT
        # release/fail the lease (we no longer own the outcome).
        self._abandoned = True
        self._emit(
            ReliabilityEvent(
                kind="shutdown_abandoned",
                task_id=getattr(lease, "task_id", None),
                lease_id=getattr(lease, "lease_id", None),
                detail={"grace_s": self._shutdown_grace_s},
            )
        )
        _log.warning(
            "worker: shutdown grace elapsed with task %s in flight; "
            "ABANDONING step (process-shutdown — abandoned thread may "
            "still run; recovery via requeue_stale after process exit)",
            getattr(lease, "task_id", None),
        )

    def _wait_for_step(self, done: threading.Event) -> bool:
        """Wait for the step thread. Returns True if it finished, False if
        it was abandoned (shutdown grace elapsed). Unbounded wait when not
        shutting down or when ``shutdown_grace_s`` is ``None`` / ``<= 0``
        (the old best-effort-forever behaviour)."""
        grace = self._shutdown_grace_s
        grace_deadline: Optional[float] = None
        while True:
            if done.wait(self._step_poll_s):
                return True
            if self._running:
                continue
            # Stop signalled. Unbounded mode → keep waiting (old behaviour).
            if grace is None or grace <= 0:
                continue
            now = self._clock()
            if grace_deadline is None:
                grace_deadline = now + grace
            elif now >= grace_deadline:
                return False

    def _execute_step(self, lease: Any, done: threading.Event) -> None:
        """Run the step + the worker exception policy on the step thread.
        Always sets ``done`` in ``finally``; the heartbeat is stopped by
        the main thread (``_run_one``) after the wait, on both the normal
        and abandon paths."""
        # Pop any stashed non-durable woken prelude (e.g.
        # ResolveApprovalPrelude) the host handed off at seed-yield time
        # (round 3a single-host-multi-worker). One-shot: consumed by this
        # step, not re-delivered on retry (a retry reads the durable
        # fold where the prelude's durable events never landed — matches
        # the benign documented loss mode for approval preludes).
        take = getattr(self._rt, "take_pending_prelude", None)
        prelude = take(lease.task_id) if callable(take) else None
        try:
            outcome = run_leased_task(
                self._rt,
                lease,
                prelude=prelude,
                reliability_sink=self._emit,
                next_goal_handle=self._next_goal_handle,
            )
            if outcome == "skipped":
                _log.warning(
                    "worker: task %s suspended with no wake_event; "
                    "re-released preserving wake_on (diagnostic symptom — "
                    "task is still waiting; H2 makes wake delivery "
                    "exactly-once, not a loss path)",
                    lease.task_id,
                )
                self._emit(
                    ReliabilityEvent(
                        kind="suspended_without_wake",
                        task_id=getattr(lease, "task_id", None),
                        lease_id=getattr(lease, "lease_id", None),
                    )
                )
            # Delegation tail: a parent this step drove to a subtask barrier has
            # its FOREGROUND children enqueued but unseeded — the resident-worker
            # path (unlike the in-request driver's ``drive_seeded``) has no drain
            # to seed + drive them. Settle the subtree here through the runtime's
            # duck-typed seam (the WorkerLoop cannot import ``noeta.execution``).
            # Self-contained + never raises, so it perturbs neither the settled
            # lease above nor the non-delegating common path.
            self._settle_subtasks(lease.task_id)
        except InvalidLease:
            # Lease is no longer ours — do NOT release/fail. No claim
            # about task state (cannot distinguish requeue vs cap-hit).
            _log.warning(
                "worker: lease %s for task %s became invalid mid-step; relinquishing",
                lease.lease_id,
                lease.task_id,
            )
        except Exception as exc:  # noqa: BLE001 — daemon must not crash
            # ② error recovery (README D-2c): provider failures NEVER reach
            # here. The only raw ``provider.complete()`` call sites are inside
            # ``runtime/llm.py`` (RuntimeLLMClient), wrapped so a provider
            # exception is translated into an error
            # ``LLMResponse`` (stop_reason="error", raw['category']=...) and
            # returned, not raised — Policy reads the category and decides.
            # Transient retries are consumed inside that wrapper (LIVE-only,
            # D-2d), so there is no double-backoff between this worker layer
            # and the LLM layer. This backstop therefore only catches genuine
            # in-process crashes (bugs, storage faults), which stay retryable
            # via the Dispatcher's bounded ``max_fail_attempts``.
            _log.exception(
                "worker: step failed for task %s; failing lease (retryable)",
                lease.task_id,
            )
            # Symptom only — we called fail(retryable=True); the Dispatcher
            # decides requeue-vs-terminal via max_fail_attempts (fail()
            # returns nothing, so the worker cannot observe the outcome).
            self._emit(
                ReliabilityEvent(
                    kind="step_failed_retryable",
                    task_id=getattr(lease, "task_id", None),
                    lease_id=getattr(lease, "lease_id", None),
                    detail={"reason": str(exc)},
                )
            )
            try:
                self._rt.dispatcher.fail(
                    lease.lease_id, retryable=True, reason=str(exc)
                )
            except Exception:  # noqa: BLE001
                _log.exception(
                    "worker: dispatcher.fail also failed for task %s; continuing",
                    lease.task_id,
                )
        finally:
            # Best-effort: the abandon path stops the heartbeat from the
            # main thread; this covers the normal/finish path.
            done.set()

    def _settle_subtasks(self, task_id: str) -> None:
        """Drive a delegation subtree the just-driven task barriered on.

        Calls the runtime's ``settle_subtasks_after_step`` seam (duck-typed —
        a bare :class:`WorkerRuntime` test double has none ⇒ byte-identical
        no-op). A NON-delegating task settles to a no-op inside the seam (the
        ``suspended``-on-``Subtask*`` gate). Best-effort by design: a subtree
        node that loses its lease is reclaimed by the periodic stale-sweep, and
        an unexpected fault must not crash the loop nor touch the already-settled
        parent lease — so both are logged, not raised.
        """
        settle = getattr(self._rt, "settle_subtasks_after_step", None)
        if settle is None:
            return
        try:
            settle(task_id)
        except InvalidLease:
            _log.warning(
                "worker: subtask drain for task %s lost a lease mid-flight; "
                "a stale-sweep reclaim will re-drive the affected node",
                task_id,
            )
        except Exception:  # noqa: BLE001 — a drain fault must not crash the loop
            _log.exception(
                "worker: subtask drain for task %s failed; continuing", task_id
            )

    def run_forever(self, *, install_signals: bool = False) -> None:
        """Drive tasks until :meth:`stop` is called. Runs the periodic
        stale-sweep and timer poll each iteration; sleeps
        ``poll_interval`` whenever the ready queue is empty.

        When ``install_signals`` is True, SIGTERM / SIGINT are wired to
        :meth:`stop` for the duration of the loop (best-effort graceful
        shutdown) and the previous handlers are restored on exit. Signal
        installation only works on the main thread; off-thread it is
        skipped with a warning (the loop can still be stopped via
        :meth:`stop`). Default False so embeddings / tests do not touch
        global signal state.
        """
        restore = install_stop_signals(self) if install_signals else None
        try:
            while self._running:
                self.maybe_sweep()
                self.maybe_poll_timers()
                if not self.tick():
                    self._sleep(self._poll_interval)
        finally:
            if restore is not None:
                restore()


def install_stop_signals(loop: WorkerLoop) -> Callable[[], None]:
    """Install SIGTERM / SIGINT handlers that call ``loop.stop()``.

    Returns a callable that restores the previous handlers. Best-effort:
    ``signal.signal`` only works on the main thread, so off-thread this
    logs a warning and returns a no-op restore (the loop is still
    stoppable via :meth:`WorkerLoop.stop`). The handlers only flip the
    loop's running flag — signal-safe; the loop notices at the top of
    its next iteration after the current synchronous step finishes
    (best-effort graceful shutdown — no in-process interrupt).
    """
    import signal

    def _handler(_signum: int, _frame: Any) -> None:
        loop.stop()

    try:
        prev_term = signal.signal(signal.SIGTERM, _handler)
        prev_int = signal.signal(signal.SIGINT, _handler)
    except ValueError:
        # Not the main thread — signal handlers cannot be installed.
        _log.warning(
            "worker: cannot install SIGTERM/SIGINT handlers off the main "
            "thread; rely on WorkerLoop.stop() for shutdown"
        )
        return lambda: None

    def _restore() -> None:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)

    return _restore
