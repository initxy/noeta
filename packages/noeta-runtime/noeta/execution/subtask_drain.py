"""Host-neutral in-process sub-agent delegation drain (Issue C / SR1/SR2).

Extracted verbatim from :class:`noeta.agent.execution.runner.CodeSessionRunner`
(Part B slice S3a) so a later slice (S3b) can drive the same delegation tree
on the server path. **Pure, behaviour-preserving refactor** — the moved state
machine is byte-identical to the runner method it came from; the only change is
that ``self._prepared`` storage/lease access is parameterised through the
:class:`DrainHost` seam, and the runner's child-engine builder is injected as a
callback.

The drain owns no lifecycle: it takes a :class:`DrainHost` carrying the
storage/lease seam (``dispatcher`` / ``event_log`` / ``content_store``), a
``build_child_engine(child_id) -> Engine`` callback, a ``parent_engine(parent_id,
*, is_root) -> Engine`` callback (root uses the prepared engine; a non-root
parent rebuilds its own agent engine), and an ``on_root_release(lease_id)``
callback the loop fires at the single point the root parent is re-leased — the
runner uses it to keep its own ``p.lease_id`` in sync (the one mutation of
runner state that used to live inside this loop). The entry point is
:func:`drive_pending_subtasks`.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional

from noeta.core.fold import fold
from noeta.policies.control_tools import RUN_WORKFLOW_TOOL
from noeta.policies.react import SPAWN_SUBAGENT_TOOL
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.engine import EngineProtocol
from noeta.protocols.errors import CodedError, TaskCancellationRequested
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.events import TaskCancelledPayload
from noeta.protocols.messages import TextBlock, ToolResultBlock, ToolUseBlock
from noeta.protocols.wake import SubtaskCompleted, SubtaskGroupCompleted
from noeta.runtime.worker import keep_lease_alive


__all__ = [
    "DrainHost",
    "UnsupportedSubtaskSuspend",
    "_DelegationFrame",
    "drive_pending_subtasks",
    "resume_woken_parent",
]


#: The control-tool tool_use names that spawn a subtask whose result must be
#: paired back as a tool_result. ``spawn_subagent`` delegates to a
#: roster agent; ``run_workflow`` spawns an orchestration-Policy
#: child. Both suspend the parent on a ``SubtaskCompleted`` and resume by
#: rendering one paired ``tool_result`` against the originating call_id, so the
#: result-pairing scan treats them identically.
_SPAWN_TOOL_NAMES = frozenset({SPAWN_SUBAGENT_TOOL, RUN_WORKFLOW_TOOL})


# ---------------------------------------------------------------------------
# fan-out v2: bounded concurrent group
# ---------------------------------------------------------------------------

#: Process-global cap on simultaneously-in-flight group members. Read once,
#: lazily, on first concurrent group; the wall-clock win comes from overlapping
#: LLM/tool I/O (storage writes serialise through the per-adapter lock anyway).
_MAX_SUBTASK_CONCURRENCY_ENV = "NOETA_MAX_SUBTASK_CONCURRENCY"
_executor_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None


def _max_concurrency() -> int:
    raw = os.environ.get(_MAX_SUBTASK_CONCURRENCY_ENV)
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return min(8, (os.cpu_count() or 2))


def _global_executor() -> ThreadPoolExecutor:
    """The shared, bounded pool that runs a concurrent group's members.

    ``max_workers`` IS the concurrency bound: a flat group of N members
    submits N jobs and at most ``max_workers`` run at once. Member subtrees
    drive **sequentially** (``allow_concurrent=False``), so a worker never
    submits more jobs — no recursive-pool deadlock, and no separate semaphore.
    """
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_max_concurrency(),
                thread_name_prefix="noeta-fanout",
            )
        return _executor


def _frame_is_concurrent(frame: "_DelegationFrame") -> bool:
    """True iff this frame is an opt-in concurrent group.
    A single-child (SR1) frame —
    ``group_wake is None`` — is never concurrent."""
    gw = frame.group_wake
    return isinstance(gw, SubtaskGroupCompleted) and bool(gw.concurrent)


class UnsupportedSubtaskSuspend(CodedError):
    """SR1 — a driven sub-agent child suspended on a wake condition the
    recursive delegation driver does not support (anything other than a
    ``SubtaskCompleted`` — i.e. approval / human / timer). Recursive
    *delegation* is supported; mid-child HITL/approval/timer is a
    documented later slice. The driver releases the child's lease (in its
    true ``suspended`` state) **before** raising, so the lease is never
    leaked."""

    code = "unsupported_subtask_suspend"

    def __init__(self, *, task_id: str, wake_on: Any, reason: str) -> None:
        self.task_id = task_id
        self.wake_on = wake_on
        self.reason = reason
        super().__init__(
            f"unsupported subtask suspend on task {task_id!r}: {reason} "
            f"(wake_on={wake_on!r})"
        )


@dataclass
class _DelegationFrame:
    """SR1/SR2 — a suspended parent awaiting its delegated member(s). Its
    lease is RELEASED while it waits; ``remaining`` members are driven one
    at a time (member order). Single child → ``remaining`` has 1 id,
    ``group_wake`` None; fan-out group → ``remaining`` has N ids,
    ``group_wake`` is the :class:`SubtaskGroupCompleted` condition. The
    parent is re-leased + resumed **once**, after the last member is driven
    and the observer has fired the (single or group) wake."""

    parent_id: str
    remaining: list[str]
    group_wake: Any = None


@dataclass(frozen=True)
class DrainHost:
    """Host-neutral seam for :func:`drive_pending_subtasks`.

    Carries the storage/lease seam plus the engine-construction callbacks the
    drain needs. Keeps the drain ignorant of *who* owns the runtime (the
    in-process ``CodeSessionRunner`` today; the server worker in S3b), so the
    same state machine drives delegation on either path.
    """

    dispatcher: Dispatcher
    event_log: EventLogFull
    content_store: ContentStore
    #: ``(child_id) -> child EngineProtocol`` — build a child sub-agent's runtime.
    build_child_engine: Callable[[str], EngineProtocol]
    #: ``(parent_id, *, is_root) -> EngineProtocol`` — the engine that resumes a
    #: parent: the prepared engine for the root, a rebuilt agent engine for a
    #: non-root parent (SR1 fix).
    parent_engine: Callable[..., EngineProtocol]
    #: Fired with the root parent's fresh lease id at the single point the
    #: root is re-leased + resumed, so the in-process host can keep its own
    #: ``p.lease_id`` in sync (the one mutation of host state inside the loop).
    on_root_release: Callable[[str], None]
    #: ``(child_id) -> Optional[(model id, principal_identity)]``.
    #: When set and it returns a binding, :func:`_descend_to_child` writes the
    #: child's opening ``ModelBound`` before seeding its goal (mirroring
    #: ``InteractionDriver.start``'s bind-then-seed order). The resolver owns
    #: the choice: the child agent's declared default model (identity
    #: ``"agent-default"``) wins, else the root session's non-default bound
    #: model (identity ``"inherited"``). ``None`` return (host-default model)
    #: or ``None`` field (the default, and the base-runner path) ⇒ no event,
    #: existing recordings byte-identical.
    child_model_binding: Optional[
        Callable[[str], Optional[tuple[str, str]]]
    ] = None
    #: The root session's bound provider name, inherited by
    #: all child sub-agents so the whole delegation tree runs on ONE provider.
    #: ``None`` ⇒ host default provider, byte-identical to pre-I4 recordings.
    child_provider: Optional[str] = None
    #: cancel-cascade — a per-tree cooperative-cancel predicate (bound by the
    #: resolver to ``is_cancelled(root_id)``). Threaded into every child's
    #: ``run_one_step`` so a mid-flight child abandons its result, AND polled
    #: between children so the drain stops descending into not-yet-run members.
    #: ``None`` (no host registry, or the in-process session-runner path) ⇒ no
    #: cancellation, byte-identical to pre-cancel-cascade behaviour.
    cancel_check: Optional[Callable[[], bool]] = None
    #: ``(child_id, child_task, lease_id) -> child_task`` — pre-loop activation
    #: of the child's session-level instructions + environment content channels
    #: (the SAME parity ``InteractionDriver.seed_start`` /
    #: ``AgentSessionRunner.prepare()`` give a top-level session). Called in
    #: :func:`_descend_to_child` right after the goal seed and before the first
    #: ``run_one_step``, so the child's first request carries the workspace dir /
    #: git / platform block (and the project AGENTS.md/NOETA.md when configured).
    #: The callback owns the snapshot source (the host's
    #: ``session_content_snapshots`` over the inherited workspace) and the
    #: ``record_instructions`` → ``record_environment`` order; both records are
    #: first-only/idempotent so a re-entrant descent / resume re-call is safe.
    #: ``None`` (the in-process session-runner path, test doubles, control-plane
    #: hosts) ⇒ no activation, byte-identical to pre-fix child recordings.
    record_session_content: Optional[Callable[[str, Any, str], Any]] = None


# ---------------------------------------------------------------------------
# Issue C: typed sub-agent delegation drain
# ---------------------------------------------------------------------------


def drive_pending_subtasks(host: DrainHost, parent: Any) -> Any:
    """SR1 — drive a (possibly **nested**) delegation tree to terminal
    and resume the root parent.

    Iterative (an explicit ``waiters`` stack — never Python call
    recursion, so depth never consumes the interpreter stack). Lease
    invariant: at most **one lease is checked out at a time** across the
    whole tree — the ``active`` task holds a lease; every task on
    ``waiters`` is ``suspended`` with its lease released (exactly the
    one-level release-on-suspend / targeted-re-lease-on-wake pattern,
    generalized to any depth).

    Bounding is honest: under the ``noeta code`` default profile the
    BudgetGuard ``max_subtask_depth`` cap denies a spawn past the cap so
    the tree is finite; with ``Budget(max_subtask_depth=None)`` the loop
    is still iterative but **not logically bounded** (some other budget
    cap must stop it).

    A driven **child** that suspends on a non-``SubtaskCompleted`` wake
    (approval / human / timer) is out of scope for SR1: the driver
    releases its lease then raises :class:`UnsupportedSubtaskSuspend`.
    The **root** parent suspending on its own wake (multi-turn
    next-goal / tool approval) is the normal Issue-A/I3 resume seam —
    released and returned, not an error.
    """
    if not (
        parent.status == "suspended"
        and isinstance(parent.wake_on, (SubtaskCompleted, SubtaskGroupCompleted))
    ):
        return parent
    root_id = parent.task_id
    # ``waiters`` is a stack of suspended parents (leases released). Each
    # frame tracks the member ids it still must drive (1 for a single
    # child, N for a fan-out group). The root is the bottom; the deepest
    # waiter is the top.
    waiters: list[_DelegationFrame] = [_frame_for(parent)]
    # cancel-cascade: the driving loop is hoisted into _run_delegation_loop so
    # this try/except can catch a TaskCancellationRequested raised mid-flight
    # (a child's run_one_step). The loop ALSO handles a between-children cancel
    # inline; both paths converge on _abort_cancelled_drain. No cancel_check
    # (the in-process session-runner path) ⇒ the loop never raises and this is
    # a plain pass-through, byte-identical to before.
    try:
        return _run_delegation_loop(host, root_id, waiters)
    except TaskCancellationRequested:
        return _abort_cancelled_drain(host, root_id, waiters)


def resume_woken_parent(host: DrainHost, parent: Any) -> Optional[Any]:
    """Resume a delegation-suspended parent whose member wake was
    delivered OUT-OF-BAND — i.e. not during a drain's own descent but by
    the :class:`ChildLifecycleObserver`, after a child that had suspended
    for approval/human input (:class:`UnsupportedSubtaskSuspend`) was
    later resolved (approved / denied / answered) and driven to terminal.

    Probes the parent's dispatcher state with a targeted lease:

    * lease refused → the parent's wake has not fired (a fan-out group
      with members still pending, or no wake at all) — return ``None``;
      a later member's resolution re-triggers this seam.
    * lease granted with **no** matched wake — the parent is ready for
      some other reason (e.g. an operator force-enqueue); it is not this
      seam's to drive, so put it back exactly as found and return ``None``.
    * lease granted **with** the matched (single or group) wake — resume
      the parent through :func:`_resume_parent_leased` (note_woken +
      paired ``tool_result`` rendering + one engine pass on the parent's
      OWN engine) and continue the standard delegation state machine, so
      a parent that immediately delegates again keeps draining.

    Returns the settled parent task (terminal, or its own suspend), or
    ``None`` when the parent was not resumable.
    """
    if not (
        parent.status == "suspended"
        and isinstance(
            parent.wake_on, (SubtaskCompleted, SubtaskGroupCompleted)
        )
    ):
        return None
    parent_lease = host.dispatcher.lease(
        worker_id="noeta-code", lease_seconds=600.0, task_id=parent.task_id
    )
    if parent_lease is None:
        return None
    if parent_lease.wake_event is None:
        # Put the row back re-leasable exactly as found (ready, no matched).
        host.dispatcher.release(
            parent_lease.lease_id,
            next_state="suspended",
            wake_on=parent.wake_on,
        )
        host.dispatcher.enqueue(parent.task_id)
        return None
    frame = _frame_for(parent)
    # The wake already fired — every member is settled; nothing to descend
    # into. (For a group wake the observer only fires after the LAST
    # distinct member terminated, so an out-of-band group resume can never
    # have unfinished members.)
    frame.remaining.clear()
    engine = host.parent_engine(parent.task_id, is_root=True)
    waiters: list[_DelegationFrame] = []
    try:
        active, active_lease, active_consumed = _resume_parent_leased(
            host, frame, engine, parent_lease
        )
        host.on_root_release(active_lease.lease_id)
        return _drive_loop(
            host, parent.task_id, waiters,
            active, active_lease, active_consumed,
            is_top_level=True, allow_concurrent=True,
        )
    except TaskCancellationRequested:
        return _abort_cancelled_drain(host, parent.task_id, waiters)


def _run_delegation_loop(
    host: DrainHost,
    root_id: str,
    waiters: list["_DelegationFrame"],
    *,
    is_top_level: bool = True,
    allow_concurrent: bool = True,
) -> Any:
    """Drive the delegation tree to its resumed terminal / suspend.

    The driving loop hoisted out of :func:`drive_pending_subtasks` so the
    cancel-cascade try/except can wrap it without re-indenting the body.
    Polls ``host.cancel_check`` between children (a truthy poll means a
    cancel landed while a child was *not* mid-step — release the
    just-returned child's still-held lease, then cascade); a cancel that
    lands *during* a child's ``run_one_step`` surfaces as a
    :class:`TaskCancellationRequested` (the descent/resume helper
    self-released that child's lease) and propagates to the caller's
    except. Both converge on :func:`_abort_cancelled_drain`.

    fan-out v2: the same loop drives
    both the top-level tree (``is_top_level=True``) and an individual concurrent
    group member's subtree (``is_top_level=False``, from an executor worker via
    :func:`_drive_member_to_terminal`). For ``is_top_level=False`` the *local*
    root is just a child, so its resume rebuilds its own engine (never the
    prepared root engine) and never fires ``on_root_release``, and a
    non-delegation suspend of that local root is an error (not a returnable
    session suspend). ``allow_concurrent`` gates whether an opt-in concurrent
    frame is fanned onto the executor (top level) or drained sequentially
    (inside a worker, so a worker never re-submits → no pool deadlock).

    ``active_consumed``
    is the wake (if any) the active lease consumed — a freshly-descended child
    consumed none (None); a resumed parent consumed its wake. EVERY release of
    ``active_lease`` passes it so the dispatcher clears the matched event.
    """
    active, active_lease, active_consumed = _enter_frame(
        host, waiters, root_id,
        is_top_level=is_top_level, allow_concurrent=allow_concurrent,
    )
    return _drive_loop(
        host, root_id, waiters, active, active_lease, active_consumed,
        is_top_level=is_top_level, allow_concurrent=allow_concurrent,
    )


def _drive_loop(
    host: DrainHost,
    root_id: str,
    waiters: list["_DelegationFrame"],
    active: Any,
    active_lease: Any,
    active_consumed: Any,
    *,
    is_top_level: bool,
    allow_concurrent: bool,
) -> Any:
    """The delegation state machine proper, entered with an ``active``
    task whose lease is held. Extracted from :func:`_run_delegation_loop`
    so :func:`resume_woken_parent` can enter it at the resumed-parent
    point (waiters empty, active = the just-resumed parent) instead of at
    the descend-into-first-member point."""
    while True:
        # cancel-cascade: a cancel landed between children. The just-returned
        # child's lease is still held — release it (terminal) before tearing
        # the rest of the tree down.
        if host.cancel_check is not None and host.cancel_check():
            host.dispatcher.release(
                active_lease.lease_id, next_state="terminal",
                consumed_wake_event=active_consumed,
            )
            return _abort_cancelled_drain(host, root_id, waiters)
        status = active.status
        if status == "terminal":
            host.dispatcher.release(
                active_lease.lease_id, next_state="terminal",
                consumed_wake_event=active_consumed,
            )
            if not waiters:
                return active  # the root itself reached terminal
            frame = waiters[-1]
            frame.remaining.pop(0)  # this member is done
            if frame.remaining:
                # more members of this SEQUENTIAL frame → drive the next one.
                # (A concurrent frame is never seen here with members left: it
                # is drained whole in _enter_frame.) The parent is NOT touched
                # until the whole group is done (B1) — the observer fires the
                # group wake only on the last distinct member.
                active, active_lease = _descend_to_child(host, frame.remaining[0])
                active_consumed = None
                continue
            # all members driven → the observer has fired the parent's
            # (single or group) wake. Resume the parent ONCE, with ITS OWN
            # engine (top-level root uses the prepared engine; any other parent
            # rebuilds its own agent engine, SR1 fix).
            active, active_lease, active_consumed = _resume_top_frame(
                host, waiters, root_id, is_top_level=is_top_level
            )
            continue
        if status == "suspended" and isinstance(
            active.wake_on, (SubtaskCompleted, SubtaskGroupCompleted)
        ):
            # `active` itself delegated (single or fan-out) → release it
            # (suspended, consuming any prior wake) and push its own
            # sub-frame; drive its first member. Parent waiter NOT
            # re-pushed (B1).
            host.dispatcher.release(
                active_lease.lease_id,
                next_state="suspended",
                wake_on=active.wake_on,
                consumed_wake_event=active_consumed,
            )
            waiters.append(_frame_for(active))
            active, active_lease, active_consumed = _enter_frame(
                host, waiters, root_id,
                is_top_level=is_top_level, allow_concurrent=allow_concurrent,
            )
            continue
        if (
            status == "suspended"
            and is_top_level
            and active.task_id == root_id
            and not waiters
        ):
            # The ROOT's own legitimate suspend (multi-turn / approval) —
            # released for its own resume seam, exactly as one-level did.
            host.dispatcher.release(
                active_lease.lease_id,
                next_state="suspended",
                wake_on=active.wake_on,
                consumed_wake_event=active_consumed,
            )
            return active
        if status == "suspended":
            # B3: a DESCENDANT suspended on a non-delegation wake — release
            # the lease in its true state FIRST, then raise a named typed
            # error (never leak the lease).
            host.dispatcher.release(
                active_lease.lease_id,
                next_state="suspended",
                wake_on=active.wake_on,
                consumed_wake_event=active_consumed,
            )
            raise UnsupportedSubtaskSuspend(
                task_id=active.task_id,
                wake_on=active.wake_on,
                reason="nested sub-agent suspended on a non-delegation "
                "wake (approval/human/timer); recursive delegation only",
            )
        # run_one_step only ever leaves a task terminal or suspended.
        raise RuntimeError(
            f"delegation driver: task {active.task_id!r} in unexpected "
            f"status {status!r}"
        )


def _enter_frame(
    host: DrainHost,
    waiters: list["_DelegationFrame"],
    root_id: str,
    *,
    is_top_level: bool,
    allow_concurrent: bool,
) -> tuple[Any, Any, Any]:
    """Begin driving the top frame's members; returns
    ``(active, active_lease, active_consumed)``.

    * **Sequential frame** (or any frame when ``allow_concurrent`` is off):
      targeted-descend into its first member (the member-at-a-time loop drives
      the rest). ``active_consumed`` is ``None`` — a fresh child consumed no wake.
    * **Concurrent opt-in frame** (top level only): drive ALL members on the
      shared executor to terminal, then pop the frame and resume its parent in
      one step — ``active`` is the resumed parent and ``active_consumed`` is the
      group wake it consumed.
    """
    frame = waiters[-1]
    if allow_concurrent and _frame_is_concurrent(frame):
        _drive_members_concurrently(host, list(frame.remaining))
        frame.remaining.clear()
        return _resume_top_frame(
            host, waiters, root_id, is_top_level=is_top_level
        )
    active, active_lease = _descend_to_child(host, frame.remaining[0])
    return active, active_lease, None


def _resume_top_frame(
    host: DrainHost,
    waiters: list["_DelegationFrame"],
    root_id: str,
    *,
    is_top_level: bool,
) -> tuple[Any, Any, Any]:
    """Pop the members-exhausted top frame and resume its parent once.

    Shared by the sequential terminal-resume path and the concurrent-group
    path. ``is_root`` (prepared engine + ``on_root_release``) is true only for
    the **top-level** session root; a concurrent member subtree drives with
    ``is_top_level=False`` so its local root resume rebuilds its own engine and
    never touches the runner's lease bookkeeping.
    """
    frame = waiters.pop()
    is_root = is_top_level and frame.parent_id == root_id
    engine = host.parent_engine(frame.parent_id, is_root=is_root)
    active, active_lease, active_consumed = _resume_parent(host, frame, engine)
    if is_root:
        host.on_root_release(active_lease.lease_id)
    return active, active_lease, active_consumed


def _drive_members_concurrently(
    host: DrainHost, member_ids: list[str]
) -> None:
    """Drive every member subtree of an opt-in concurrent group to terminal,
    in parallel, on the shared bounded executor.

    Each member drains **sequentially** inside its worker (so a worker never
    re-submits → no recursive-pool deadlock). Joins ALL workers before
    returning — even on error — so no member is left mid-flight with a held
    lease, then re-raises the first worker exception (a cooperative
    ``TaskCancellationRequested`` or an :class:`UnsupportedSubtaskSuspend`).
    """
    if len(member_ids) <= 1:
        # zero / one member: nothing to overlap — drive inline, byte- and
        # behaviour-identical to the sequential path (and no pool spin-up).
        for member_id in member_ids:
            _drive_member_to_terminal(host, member_id)
        return
    executor = _global_executor()
    futures = [
        executor.submit(_drive_member_to_terminal, host, member_id)
        for member_id in member_ids
    ]
    first_error: Optional[BaseException] = None
    for fut in futures:
        try:
            fut.result()
        except BaseException as exc:  # noqa: BLE001 — join all, surface one
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _drive_member_to_terminal(host: DrainHost, member_id: str) -> None:
    """Drive ONE concurrent-group member (and its nested subtree) to terminal,
    holding only its own lease(s). Runs in an executor worker.

    Its subtree drains sequentially (``allow_concurrent=False``). A member that
    suspends on a non-delegation wake (approval/human/timer) cannot satisfy the
    group barrier, so it raises :class:`UnsupportedSubtaskSuspend` — surfaced by
    :func:`_drive_members_concurrently` after the group joins.
    """
    active, active_lease = _descend_to_child(host, member_id)
    if active.status == "terminal":
        host.dispatcher.release(
            active_lease.lease_id, next_state="terminal", consumed_wake_event=None
        )
        return
    if active.status == "suspended" and isinstance(
        active.wake_on, (SubtaskCompleted, SubtaskGroupCompleted)
    ):
        # The member itself delegated → release it suspended and drive its own
        # subtree with the shared loop, member as its OWN local root
        # (is_top_level=False). Nested groups drain sequentially.
        host.dispatcher.release(
            active_lease.lease_id,
            next_state="suspended",
            wake_on=active.wake_on,
            consumed_wake_event=None,
        )
        final = _run_delegation_loop(
            host,
            member_id,
            [_frame_for(active)],
            is_top_level=False,
            allow_concurrent=False,
        )
        if final.status != "terminal":  # defensive — the loop raises first
            raise UnsupportedSubtaskSuspend(
                task_id=final.task_id,
                wake_on=final.wake_on,
                reason="concurrent group member resumed but did not reach "
                "terminal (non-delegation suspend)",
            )
        return
    # Member suspended on a non-delegation wake on its very first step.
    host.dispatcher.release(
        active_lease.lease_id,
        next_state="suspended",
        wake_on=active.wake_on,
        consumed_wake_event=None,
    )
    raise UnsupportedSubtaskSuspend(
        task_id=active.task_id,
        wake_on=active.wake_on,
        reason="concurrent group member suspended on a non-delegation wake "
        "(approval/human/timer); recursive delegation only",
    )


def _abort_cancelled_drain(
    host: DrainHost, root_id: str, waiters: list["_DelegationFrame"]
) -> Any:
    """cancel-cascade teardown. The in-flight child's lease is already
    released (loop-top release, or the descent/resume helper's self-release
    on the raise). Mark every spawned-but-unfinished member across all
    waiter frames — the in-flight child plus its not-yet-driven
    siblings/cousins — cancelled with a ``TaskCancelled`` event, so fold and
    the read-models show them terminal rather than orphaned-pending, then
    return the already-terminal root (its ``TaskCancelled`` was written by
    the control-plane ``cancel``)."""
    seen: set[str] = set()
    for frame in waiters:
        for cid in frame.remaining:
            if cid in seen:
                continue
            seen.add(cid)
            _emit_child_cancelled(host, cid)
    return fold(host.event_log, host.content_store, root_id)


def _emit_child_cancelled(host: DrainHost, child_id: str) -> None:
    """Write a cascade ``TaskCancelled`` for one tree member. Idempotent —
    skips a member that has no ``TaskCreated`` yet or already carries a
    ``TaskCancelled``. No lease: like the control-plane ``cancel`` this is an
    observer-style ``system_emit`` (no lease / no ``state_patch``) so it does
    not race the Engine's single RuntimeState writer."""
    events = host.event_log.read(child_id)
    # Skip a member with no TaskCreated yet, or one that already reached a
    # terminal — a concurrent group member can finish (TaskCompleted/TaskFailed)
    # before the cascade reaches its frame, and double-terminal-marking it would
    # be wrong.
    if not events or any(
        e.type in ("TaskCancelled", "TaskCompleted", "TaskFailed")
        for e in events
    ):
        return
    host.event_log.system_emit(
        task_id=child_id,
        type="TaskCancelled",
        payload=TaskCancelledPayload(reason="parent-cancelled", cascade=True),
        actor="cancel-cascade",
        origin="system",
        trace_id=events[0].trace_id,
    )


def _frame_for(parent: Any) -> "_DelegationFrame":
    """Build the waiter frame for a parent suspended on a (single or
    group) delegation wake."""
    wc = parent.wake_on
    if isinstance(wc, SubtaskGroupCompleted):
        return _DelegationFrame(
            parent_id=parent.task_id,
            remaining=list(wc.subtask_ids),
            group_wake=wc,
        )
    return _DelegationFrame(
        parent_id=parent.task_id, remaining=[wc.subtask_id], group_wake=None
    )


def _descend_to_child(host: DrainHost, expected_child_id: str) -> tuple[Any, Any]:
    """B2 — targeted-lease the named child (never the non-targeted
    "next ready" task), build its own runtime, seed only its goal, and
    advance one engine pass. Returns ``(child_task, child_lease)``."""
    child_lease = host.dispatcher.lease(
        worker_id="noeta-code-child",
        lease_seconds=600.0,
        task_id=expected_child_id,
    )
    if child_lease is None:
        raise RuntimeError(
            f"delegation: expected child {expected_child_id!r} not ready "
            "to lease (ChildLifecycleObserver should have enqueued it)"
        )
    assert child_lease.task_id == expected_child_id, (
        f"targeted child lease returned {child_lease.task_id!r}, "
        f"expected {expected_child_id!r}"
    )
    child_engine = host.build_child_engine(child_lease.task_id)
    child_task = fold(host.event_log, host.content_store, child_lease.task_id)
    # A child's opening model binding (its agent's declared default model,
    # else the root session's inherited non-default binding — the resolver's
    # ``child_model_binding`` callback owns the choice) lands as the child
    # task's own opening ModelBound — written BEFORE the goal seed, mirroring
    # InteractionDriver.start's bind-then-seed order, so fold/_bound_model_for
    # resolve the child on that model and a later cold resume rebuilds the
    # same binding. Skipped when the host wires no callback (test doubles),
    # the callback returns no binding (host-default model), or the child is
    # already bound (re-entrant descent after a suspend).
    if host.child_model_binding is not None:
        binding = host.child_model_binding(child_lease.task_id)
        if binding and not child_task.governance.model_binding:
            bind_model, bind_identity = binding
            child_task = child_engine.note_model_bound(
                child_task,
                lease_id=child_lease.lease_id,
                model=bind_model,
                principal_identity=bind_identity,
                provider=host.child_provider,
            )
    # Seed ONLY the child goal (isolated context — never the parent's
    # messages or system prompt). Resume-safe: only seed when the child has no
    # messages yet (a fresh child folds to an empty ``runtime.messages``). A
    # background-sub-agent crash-recovery re-drive (docs/adr/background-subagent.md)
    # descends into an already-seeded child to continue it from its own EventLog —
    # re-seeding the goal there would duplicate it. The foreground path always
    # descends into a fresh child, so this guard is a no-op for it.
    if not child_task.runtime.messages:
        child_task = child_engine.append_user_message(
            child_task,
            content=[TextBlock(text=child_task.state.goal)],
            lease_id=child_lease.lease_id,
        )
    # Pre-loop activation of the child's instructions + environment content
    # channels — the same parity a top-level session gets via
    # ``InteractionDriver.seed_start`` / ``AgentSessionRunner.prepare()`` (and
    # what Claude Code gives a subagent). Recorded AFTER the goal seed and BEFORE
    # the first step so the child's first request carries the workspace block.
    # The records are first-only/idempotent (so a re-entrant descent is safe) and
    # land in semi_stable under the ``evolving`` drift policy — they never enter
    # the stable_prefix, so adding them does not bust prompt caching. ``None``
    # callback (in-process runner / test doubles) ⇒ no-op, byte-identical.
    if host.record_session_content is not None:
        child_task = host.record_session_content(
            child_lease.task_id, child_task, child_lease.lease_id
        )
    try:
        # Keep the child's lease alive while its step runs — no resident
        # WorkerLoop heartbeats this in-request drain, so a child step longer
        # than the lease TTL would otherwise lose its lease mid-flight.
        with keep_lease_alive(host.dispatcher, child_lease):
            child_task = child_engine.run_one_step(
                child_task, lease_id=child_lease.lease_id,
                cancelled=host.cancel_check,
            )
    except TaskCancellationRequested:
        # cancel-cascade: release our own lease so it never leaks, then let
        # the drain catch the signal and cascade-cancel the tree.
        host.dispatcher.fail(
            child_lease.lease_id, retryable=False, reason="cancelled"
        )
        raise
    return child_task, child_lease


def _resume_parent(
    host: DrainHost, frame: "_DelegationFrame", engine: Any
) -> tuple[Any, Any, Any]:
    """Targeted re-lease a parent whose member(s) just completed
    (consuming the single or group wake), fold it, ``note_woken``, render
    the result(s) as paired ``tool_result``(s) (Engine single-writer
    seam — lands between ``TaskWoken`` and the next
    ``ContextPlanComposed``, the post-wake control-event window fold
    reconstructs on resume), and advance one engine pass through **the
    parent's own engine**.
    Returns ``(parent_task, lease, consumed_wake_event)`` — the consumed
    wake the caller must pass to the eventual ``release`` so the
    dispatcher clears the matched event (H2 D2; lease no longer clears
    it)."""
    parent_lease = host.dispatcher.lease(
        worker_id="noeta-code", lease_seconds=600.0, task_id=frame.parent_id
    )
    if parent_lease is None or parent_lease.wake_event is None:
        raise RuntimeError(
            "delegation: dispatcher did not hand back the woken parent "
            f"{frame.parent_id!r} after member completion"
        )
    return _resume_parent_leased(host, frame, engine, parent_lease)


def _resume_parent_leased(
    host: DrainHost, frame: "_DelegationFrame", engine: Any, parent_lease: Any
) -> tuple[Any, Any, Any]:
    """The body of :func:`_resume_parent` for a parent whose woken lease
    the caller already holds (the out-of-band :func:`resume_woken_parent`
    seam probes the lease itself before committing to a resume)."""
    # Re-fold from the durable log to pick up the observer-written
    # SubtaskCompleted(s) (governance.subtask_results) + folded messages.
    parent = fold(host.event_log, host.content_store, frame.parent_id)
    parent = engine.note_woken(
        parent, lease_id=parent_lease.lease_id, wake_event=parent_lease.wake_event
    )
    if frame.group_wake is None:
        # SR1 single child: one paired tool_result.
        result = parent.governance.subtask_results[-1]
        call_id = _pending_spawn_call_id(parent)
        parent = engine.append_subagent_result_message(
            parent,
            call_id=call_id,
            output=result.output,
            success=result.status == "completed",
            error=result.error,
            lease_id=parent_lease.lease_id,
        )
    else:
        # SR2 fan-out: N paired tool_results in member order, keyed from
        # the parent stream; call_ids positional from the assistant msg.
        wake_event = parent_lease.wake_event  # SubtaskGroupCompleted (B4)
        call_ids = _pending_spawn_call_ids(
            parent, len(wake_event.subtask_ids)
        )
        parent = engine.append_subagent_group_result_messages(
            parent, wake_event, call_ids, lease_id=parent_lease.lease_id
        )
    try:
        # Keep the parent's lease alive while its resume step runs. This is the
        # post-subtask-completion step that re-folds the (now large) context and
        # calls the LLM to continue the turn; with no resident WorkerLoop to
        # heartbeat it, a slow round-trip here is exactly what burned the whole
        # lease window and stranded the task non-terminal.
        with keep_lease_alive(host.dispatcher, parent_lease):
            parent = engine.run_one_step(
                parent, lease_id=parent_lease.lease_id,
                cancelled=host.cancel_check,
            )
    except TaskCancellationRequested:
        # cancel-cascade: release our own lease (see _descend_to_child).
        host.dispatcher.fail(
            parent_lease.lease_id, retryable=False, reason="cancelled"
        )
        raise
    return parent, parent_lease, parent_lease.wake_event


def _pending_spawn_call_id(parent: Any) -> str:
    """The ``call_id`` of the most recent parent ``spawn_subagent``
    ``ToolUseBlock`` that has no paired tool result yet — the pairing
    key for the child result message (Issue C)."""
    resolved: set[str] = set()

    for msg in parent.runtime.messages:
        if msg.role == "tool":
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    resolved.add(block.call_id)
    for msg in reversed(parent.runtime.messages):
        if msg.role != "assistant":
            continue
        for block in msg.content:
            if (
                isinstance(block, ToolUseBlock)
                and block.tool_name in _SPAWN_TOOL_NAMES
                and block.call_id not in resolved
            ):
                return block.call_id
    raise RuntimeError(
        "delegation: no unpaired spawn_subagent/run_workflow tool_use on the parent"
    )


def _spawn_member_count(block: Any) -> int:
    """How many fan-out members one spawn tool_use carries: the length of a
    well-formed batch ``spawns`` array, else 1 (the legacy single form, every
    pre-batch recording, and ``run_workflow``). Mirrors the translate seam's
    member expansion so the positional pairing below stays aligned with the
    specs the handler admitted."""
    arguments = getattr(block, "arguments", None)
    if isinstance(arguments, dict):
        raw = arguments.get("spawns")
        if isinstance(raw, (list, tuple)) and raw:
            return len(raw)
    return 1


def _pending_spawn_call_ids(parent: Any, n: int) -> list[str]:
    """SR2 — the ``n`` unpaired spawn member call_ids on the parent, in
    **member order** (assistant tool_use order, then entry order within a
    batch call — a tool_use carrying a ``spawns`` array contributes its
    call_id once per entry), for positional pairing with a group's
    ``subtask_ids``. Raises if the count != n.

    This is unambiguous: ``wake_on`` is scalar so a parent has at most
    ONE pending delegation group, and that group's ``spawn_subagent``
    tool_uses are contiguous in a single assistant turn; any earlier
    spawn is already paired (has a ``tool_result``) and excluded.
    """
    resolved: set[str] = set()
    for msg in parent.runtime.messages:
        if msg.role == "tool":
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    resolved.add(block.call_id)
    unpaired: list[str] = []
    for msg in parent.runtime.messages:  # forward = member (spawn) order
        if msg.role != "assistant":
            continue
        for block in msg.content:
            if (
                isinstance(block, ToolUseBlock)
                and block.tool_name in _SPAWN_TOOL_NAMES
                and block.call_id not in resolved
            ):
                unpaired.extend([block.call_id] * _spawn_member_count(block))
    if len(unpaired) != n:
        raise RuntimeError(
            f"delegation: expected {n} unpaired spawn_subagent call_ids "
            f"on the parent, found {len(unpaired)}"
        )
    return unpaired
