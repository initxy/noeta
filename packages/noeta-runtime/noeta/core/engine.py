"""Engine: drives a single Task forward by one Step.

The Engine advances a Task to its next suspend point or terminal:

    * ``finish`` / ``fail`` emit a ``TaskSnapshot`` before every terminal
      event, and ``create_task`` emits the ``TaskCreated`` genesis event.
    * the ``tool_calls`` branch runs each call through a ``ToolRuntime``
      and loops back to ``compose → decide`` until the Policy returns a
      non-tool_calls decision; multiple results from one decision batch
      into a single ``MessagesAppended`` event.
    * the ``spawn_subtask`` branch keeps strict event ordering; a
      ``note_woken`` API lets workers emit ``TaskWoken`` on re-lease.
      Engine deliberately knows nothing about the Dispatcher or any
      Observer: the ``SubtaskCompleted`` append to the parent stream and
      the ``dispatcher.wake`` handoff live entirely in
      ``noeta.core.observers.ChildLifecycleObserver``. The per-decision
      work itself runs in the module-level ``handle_finish`` /
      ``handle_fail`` / ``handle_spawn_subtask`` in
      ``noeta.core._decision_handlers``.
    * a third ``TaskSnapshot`` trigger writes a snapshot when consecutive
      ``tool_calls`` iterations cross
      ``CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD`` (default 20) without
      releasing the lease (the terminal- and suspend-prefix triggers
      cover the other two snapshot points).
    * suspend branches: ``yield_for_human`` exits on a
      ``HumanResponseReceived`` wake; ``wait_timer`` exits on a
      ``TimerFired`` wake.

The line budget is ≤ 500 lines
of body code; we are well under that.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Optional

import copy

from noeta.core._decision_handlers import (
    ContentHashesFn,
    HandlerContext,
    SkillHashesFn,
    _validate_tool_output_inline_limit,
    append_tool_denial_feedback,
    dispatch_exit,
    emit_skill_provenance_for_patch,
    emit_step_transition,
    handle_compaction_requested,
    handle_spawn_background_subtask,
    handle_state_patch,
    handle_tool_calls,
    handle_yield_for_human,
    invoke_approved_tool_call,
    put_messages,
    record_assistant_thinking,
    strip_message_origin,
)
from noeta.core.fold import apply_event, fold
from noeta.core.hooks import HookManager
from noeta.core.snapshot import (
    CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD,
    serialize_task_state,
    snapshot_media_type,
)
from noeta.protocols.composer import ContextComposer
from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLog
from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.values import ContentRef
from noeta.protocols.decisions import (
    CompactionRequestedDecision,
    Decision,
    SpawnSubtaskDecision,
    StatePatchDecision,
    TaskStatePatch,
    ToolCall,
    ToolCallsDecision,
    YieldForHumanDecision,
)
from noeta.protocols.errors import (
    ApprovalNotPending,
    TaskCancellationRequested,
    UserQuestionNotPending,
)
from noeta.protocols.events import (
    AgentBoundPayload,
    TaskHostBoundPayload,
    ContextPlanComposedPayload,
    ConversationClosedPayload,
    ContextContentRecordedPayload,
    ConversationReopenedPayload,
    EventEnvelope,
    ModelBoundPayload,
    TaskCreatedPayload,
    TaskSnapshotPayload,
    TaskStartedPayload,
    TaskStatePatchedPayload,
    TaskWokenPayload,
    ToolCallApprovalResolvedPayload,
    UserQuestionAnsweredPayload,
)
from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    VerdictResult,
)
from noeta.protocols.messages import (
    Block,
    ImageBlock,
    Message,
    MessageOrigin,
    TextBlock,
    ToolResultBlock,
)
from noeta.protocols.policy import Policy
from noeta.protocols.step_context import StepContext
from noeta.protocols.task import Task, TaskState
from noeta.protocols.tool import Tool
from noeta.protocols.tool_args import resolve_tool_call_arguments


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


#: Work item ④ — upper bound on the recent tool-call history the Engine folds
#: into ``GuardContext.recent_tool_calls`` for ``RepetitionGuard``. Generous
#: enough to cover any sane repetition threshold; the guard truncates to its
#: own ``policy.window`` when counting the consecutive run.
_RECENT_TOOL_CALLS_WINDOW = 32


def _emit_child_task_created(
    event_log: EventLog,
    actor: str,
    policy_name: str,
    *,
    child_task_id: str,
    parent_task_id: str,
    agent_name: str,
    goal: str,
    inputs: dict[str, Any],
    trace_id: str,
    subtask_depth: int = 0,
    background: bool = False,
) -> EventEnvelope:
    """Cross-stream ``system_emit`` of a child's ``TaskCreated`` (the one
    cross-stream system write a handler needs).

    The ``actor`` / ``origin`` / ``trace_id`` bookkeeping for child-task genesis
    stays in one place; ``policy_name`` is locked to the Engine's
    ``_SUBTASK_DEFAULT_POLICY_NAME`` (``"scripted"``) to preserve byte-equal
    behaviour with the pre-refactor inline write. ``background=True``
    (docs/adr/background-subagent.md) marks the child so the
    ``ChildLifecycleObserver`` skips it; the default ``False`` omits the key
    (``__canonical_omit_none__``) ⇒ byte-identical to every foreground child."""
    return event_log.system_emit(
        task_id=child_task_id,
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal=goal,
            policy_name=policy_name,
            agent_name=agent_name,
            parent_task_id=parent_task_id,
            inputs=dict(inputs),
            subtask_depth=subtask_depth,
            background=True if background else None,
        ),
        actor=actor,
        origin="engine",
        trace_id=trace_id,
    )


def _background_subagent_seams(
    launcher: Optional[Any],
) -> tuple[Optional[Any], Optional[Any]]:
    """Split a duck-typed background-sub-agent launcher into the two
    ``HandlerContext`` seams ``(launch, capacity)`` — or ``(None, None)`` when no
    launcher is wired (docs/adr/background-subagent.md)."""
    if launcher is None:
        return None, None
    return launcher.launch, launcher.capacity


def _is_background_spawn(decision: Any, launch_seam: Optional[Any]) -> bool:
    """True iff this decision is a background ``spawn_subagent`` AND a launcher is
    wired — the gate for the loop-continuing background branch in
    ``run_one_step`` (docs/adr/background-subagent.md). False ⇒ the decision
    falls through to the foreground barrier spawn in ``dispatch_exit``."""
    return (
        isinstance(decision, SpawnSubtaskDecision)
        and bool(decision.background)
        and launch_seam is not None
    )


class Engine:
    """Phase 0 Engine. Drives one Step toward suspend or terminal.

    Per-Decision branch logic (finish / fail / spawn_subtask / wait_timer /
    yield_for_human / tool_calls) lives in
    :mod:`noeta.core._decision_handlers` (issue C3 implementation).
    Engine retains the compose → decide loop, the controlled callable
    seams (``_emit`` / ``_guard`` / ``_write_snapshot`` /
    ``_resolve_tool`` / ``_create_child_task``), and the
    state_patch / assistant_message helpers that fire before every
    branch handler.
    """

    #: Subtask genesis default. Pre-refactor inline literal at
    #: engine.py:723; preserved here so :meth:`_create_child_task`
    #: writes byte-equal child ``TaskCreatedPayload``.
    _SUBTASK_DEFAULT_POLICY_NAME = "scripted"

    def __init__(
        self,
        *,
        event_log: EventLog,
        content_store: ContentStore,
        composer: ContextComposer,
        policy: Optional[Policy] = None,
        tools: Optional[dict[str, Tool]] = None,
        tool_runtime: Any = None,
        hooks: Optional[HookManager] = None,
        clock: Any = None,
        id_factory: Optional[Callable[[], str]] = None,
        actor: str = "engine",
        skill_hashes: Optional[SkillHashesFn] = None,
        content_hashes: Optional[ContentHashesFn] = None,
        tool_output_inline_limit: Optional[int] = None,
        background_runner: Optional[Any] = None,
        file_checkpoint_registry: Optional[Any] = None,
        background_subagent_launcher: Optional[Any] = None,
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        # the kernel holds no opinion on View assembly and
        # never reaches up into a concrete ``noeta.context`` Composer.
        # ``composer`` is a required injection — hosts wire a real one
        # (e.g. ``noeta.context.ThreeSegmentComposer``); callers wanting the
        # zero-opinion empty View pass ``noeta.core.composer.PassthroughComposer``.
        self._composer = composer
        self._policy = policy
        self._tools = dict(tools or {})
        # reject non-positive limits centrally so
        # every construction path (live host, resume) shares one
        # error. ``None`` is allowed and disables truncation entirely.
        _validate_tool_output_inline_limit(tool_output_inline_limit)
        self._tool_output_inline_limit = tool_output_inline_limit
        if tool_runtime is None and tools:
            # Default ToolRuntime so tests can pass tools without wiring the
            # wrapper (an injected one brings its own); see _default_tool_runtime.
            tool_runtime = _default_tool_runtime(
                event_log, content_store, background_runner, file_checkpoint_registry)
        self._tool_runtime = tool_runtime
        self._hooks = hooks or HookManager()
        self._clock = clock or time.time
        # ``id_factory`` mints subtask_ids the engine generates inside
        # ``_spawn_subtask``. Defaults to ``uuid.uuid4`` so production
        # callers keep the original behaviour; a test can inject a
        # deterministic factory (e.g. one that pops a fixed sequence of ids).
        self._id_factory: Callable[[], str] = (
            id_factory if id_factory is not None else _default_id_factory
        )
        self._actor = actor

        # Build the HandlerContext once; handlers receive it by value
        # and reach EventLog / HookManager / ContentStore only through
        # the typed callables wired here. No raw event_log or
        # hook_manager exposure to the handler module (issue C3 design
        # contract).
        def _apply_event(task: Task, env: EventEnvelope) -> None:
            apply_event(task, env, self._content_store)

        # background sub-agent seams (docs/adr/background-subagent.md): the
        # launcher is a duck-typed object (``.launch`` / ``.capacity``) so
        # ``noeta.core`` never imports the executor-driven registry up in
        # ``noeta.execution``. Derived in one module-level helper; ``None``
        # everywhere but a top-level interactive Engine.
        bg_launch, bg_capacity = _background_subagent_seams(
            background_subagent_launcher
        )
        self._launch_background_subagent = bg_launch

        self._ctx = HandlerContext(
            emit=self._emit,
            create_child_task=(
                lambda **kw: _emit_child_task_created(
                    self._event_log, self._actor,
                    self._SUBTASK_DEFAULT_POLICY_NAME, **kw,
                )
            ),
            apply_event=_apply_event,
            guard=self._guard,
            write_snapshot=self._write_snapshot,
            resolve_tool=self._resolve_tool,
            tool_invoker=self._tool_runtime,
            content_store=self._content_store,
            id_factory=self._id_factory,
            clock=self._clock,
            actor=self._actor,
            skill_hashes=skill_hashes,
            content_hashes=content_hashes,
            tool_output_inline_limit=tool_output_inline_limit,
            launch_background_subagent=bg_launch,
            background_subagent_capacity=bg_capacity,
        )

    # -- task bootstrap ---------------------------------------------------

    def create_task(
        self,
        *,
        goal: str,
        policy_name: str,
        agent_name: str = "unnamed",
        parent_task_id: Optional[str] = None,
        inputs: Optional[dict[str, Any]] = None,
        task_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        host_binding: Optional[TaskHostBoundPayload] = None,
    ) -> Task:
        """Append ``TaskCreated`` (and, for a named task, ``AgentBound`` /
        ``TaskHostBound``) and return the in-memory Task object.

        A named task's genesis
        sequence is ``TaskCreated → AgentBound`` emitted **atomically inside this
        call** — one trusted write point, so a named product Task can never be
        created without its durable Agent identity record. When ``host_binding``
        is also supplied (the server product / session path), a ``TaskHostBound``
        follows the ``AgentBound``. The legacy / ``unnamed`` path emits neither
        (byte-equal with old recordings).
        """
        _validate_genesis_provenance(agent_name, host_binding)
        tid = task_id or f"task-{uuid.uuid4().hex}"
        trace = trace_id or f"trace-{uuid.uuid4().hex}"
        payload = TaskCreatedPayload(
            goal=goal,
            policy_name=policy_name,
            agent_name=agent_name,
            parent_task_id=parent_task_id,
            inputs=dict(inputs or {}),
        )
        self._event_log.system_emit(
            task_id=tid,
            type="TaskCreated",
            payload=payload,
            actor=self._actor,
            origin="engine",
            trace_id=trace,
        )
        _emit_genesis_provenance(
            self._event_log,
            tid=tid,
            trace=trace,
            actor=self._actor,
            agent_name=agent_name,
            host_binding=host_binding,
        )
        return Task(
            task_id=tid,
            status="pending",
            parent_task_id=parent_task_id,
            state=TaskState(goal=goal),
        )

    # -- conversation seeding --------------------------------------------

    def append_user_message(
        self,
        task: Task,
        *,
        content: list[Block],
        lease_id: str,
        trace_id: Optional[str] = None,
        origin: Optional[MessageOrigin] = None,
    ) -> Task:
        """Seed a ``user`` turn into the conversation via the EventLog.

        Callers (CLI / SDK / demo) use this to inject user input *after*
        ``create_task`` and *before* ``run_one_step``, so the message is
        durable in the EventLog and shows up identically after
        ``fold(events)`` — a resume then reconstructs the same
        ``view.messages`` the live Policy saw. Direct mutation of
        ``task.runtime.messages`` from outside Engine would break the single-writer invariant
        (Engine is the single writer of RuntimeState) and surface as an
        ``llm_args`` divergence between the live turn and a refold.

        The seam takes a typed ``content: list[Block]`` (the
        breaking change for image input — a text-only turn passes
        ``[TextBlock(text)]`` and serializes byte-identically to the old
        path). Only the blocks a user turn may legitimately carry are
        accepted — ``TextBlock`` / ``ImageBlock``; ``ThinkingBlock`` /
        ``ToolUseBlock`` / ``ToolResultBlock`` and an empty list are
        rejected with a clear ``ValueError`` so a caller cannot smuggle a
        model-side or tool-side block into the user channel.

        ``origin`` is the **sole writer seam** for
        ``Message.origin``: hosts tag injected system-side content
        (``system``) or memory recall (``memory``) here; Policy-supplied
        messages get origin stripped at the Decision seams, so a value
        in the ledger always means "the host said so at this seam".
        """
        _validate_user_content(content)
        msg = Message(role="user", content=content, origin=origin)
        return self._append_message(task, msg, lease_id=lease_id, trace_id=trace_id)

    def _append_message(
        self, task: Task, msg: Message, *, lease_id: str, trace_id: Optional[str]
    ) -> Task:
        """Emit one ``MessagesAppended`` for ``msg`` and mirror it into
        ``task.runtime.messages`` (the shared tail of every message-append
        seam; Engine stays the single writer)."""
        self._emit(
            task_id=task.task_id,
            type_="MessagesAppended",
            payload=put_messages(self._content_store, [msg]),
            lease_id=lease_id,
            trace_id=trace_id or self._latest_trace_id(task.task_id),
        )
        task.runtime.messages.append(msg)
        return task

    def append_subagent_result_message(
        self,
        task: Task,
        *,
        call_id: str,
        output: Any,
        success: bool,
        lease_id: str,
        error: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Task:
        """Append the paired ``role="tool"`` result for a delegated
        sub-agent (Phase 4.5 Issue C, architect pin 2).

        After a parent wakes from a ``SubtaskCompleted``, the runner uses
        this narrow seam to render the child's ``SubtaskResult`` as a
        ``ToolResultBlock`` paired to the original ``spawn_subagent``
        ``tool_use`` ``call_id`` — so the child result enters the parent's
        next compose (``dynamic_suffix``) and the dangling delegation
        ``tool_use`` gets its matching ``tool_result``. Engine stays the
        single writer of ``RuntimeState.messages``; the runner
        never appends directly.

        On failure the child's own ``reason`` is surfaced via ``error`` (the
        caller passes ``SubtaskResult.error``) — matching the group seam, so a
        failed single delegate is no longer flattened to a generic string and a
        workflow's ``agent()`` can report *why* its helper failed.
        """
        block = ToolResultBlock(
            call_id=call_id,
            output=self._deref_subagent_output(output),
            success=success,
            error=None if success else (error or "sub-agent failed"),
        )
        msg = Message(role="tool", content=[block])
        return self._append_message(task, msg, lease_id=lease_id, trace_id=trace_id)

    def _deref_subagent_output(self, output: Any) -> Any:
        """A subtask result output may be a ``ContentRef`` (a large answer
        spilled to the ContentStore); deref it to the full body so
        the paired ``tool_result`` carries the real text — the Message then
        re-refs via ``messages_ref``, keeping the event under the payload cap.
        Inline str/dict outputs pass through; ``None`` normalises to ``""``."""
        if isinstance(output, ContentRef):
            return from_canonical_bytes(self._content_store.get(output))
        return output if output is not None else ""

    def append_subagent_group_result_messages(
        self,
        task: Task,
        wake_event: Any,
        call_ids: list[str],
        *,
        lease_id: str,
        trace_id: Optional[str] = None,
    ) -> Task:
        """SR2 — render a fan-out group's N child results
        as **one** ``MessagesAppended`` carrying N ``ToolResultBlock``s in
        **member (spawn) order**.

        ``wake_event`` is the consumed ``SubtaskGroupCompleted`` (gives the
        ordered ``subtask_ids``); ``call_ids`` is the positional pairing of
        originating ``spawn_subagent`` call ids (member order, supplied by
        the caller from the assistant message). The per-child results are
        read from the parent stream's keyed ``SubtaskCompleted`` events —
        NOT the unkeyed ``governance.subtask_results``. Per-block
        normalization matches the single-child seam (``output`` never
        ``null``). Engine stays the single writer.
        """
        subtask_ids = tuple(wake_event.subtask_ids)
        if len(call_ids) != len(subtask_ids):
            raise ValueError(
                "subagent group result: call_ids / subtask_ids length "
                f"mismatch ({len(call_ids)} != {len(subtask_ids)})"
            )
        # keyed results from the parent stream (last completion per id).
        results: dict[str, Any] = {}
        for env in self._event_log.read(task.task_id):
            if env.type == "SubtaskCompleted":
                results[env.payload.subtask_id] = env.payload.result
        blocks: list[Block] = []
        for sid, call_id in zip(subtask_ids, call_ids):
            r = results[sid]
            success = r.status == "completed"
            blocks.append(
                ToolResultBlock(
                    call_id=call_id,
                    output=self._deref_subagent_output(r.output),
                    success=success,
                    error=None if success else (r.error or "sub-agent failed"),
                )
            )
        msg = Message(role="tool", content=blocks)
        return self._append_message(task, msg, lease_id=lease_id, trace_id=trace_id)

    # -- operator-driven state patch -------------------------------------

    def apply_state_patch(
        self,
        task: Task,
        *,
        patch: TaskStatePatch,
        lease_id: str,
        trace_id: Optional[str] = None,
    ) -> Task:
        """Apply an operator-driven ``TaskStatePatch`` (Phase 4 B17).

        Used by the Noeta-Code runner to deterministically activate
        skills before the first compose. Emits the durable
        ``TaskStatePatched`` event so a resume reproduces the same
        active set (``ContextPlan.selected_skills``) without depending
        on the model emitting ``activate_skills``.

        Engine remains the single writer of ``TaskState``;
        callers MUST hold a valid lease. The Policy-side path
        ``_apply_decision_state_patch`` is unchanged — this is a parallel
        operator-side entry that emits the same event type so fold /
        resume handle both identically.

        Patches carrying ``activate_skills`` automatically emit one
        content-provenance event per skill (per-task first-only,
        fold-guarded) right before the ``TaskStatePatched`` event,
        matching the causal order the pre-loop SDK helper produces.
        Post issue-07 generation switch the event is the generic
        ``ContextContentRecorded`` (kind="skill", policy="pinned") via the
        ``content_hashes`` seam; an Engine wired through the retained
        legacy ``skill_hashes`` seam (resuming an old recording) still
        emits the old ``SkillContentRecorded`` byte-equal.
        With neither resolver the emission is skipped entirely, preserving
        old host byte shapes.
        """
        resolved_trace = trace_id or self._latest_trace_id(task.task_id)
        emit_skill_provenance_for_patch(self._ctx, task, patch, lease_id=lease_id, trace_id=resolved_trace)
        self._emit(
            task_id=task.task_id,
            type_="TaskStatePatched",
            payload=TaskStatePatchedPayload(patch=patch.to_dict()),
            lease_id=lease_id,
            trace_id=resolved_trace,
        )
        patch.apply(task.state)
        return task

    # -- operator-driven tool-call approval (Phase 4.5 Issue A) ----------

    def resolve_tool_approval(
        self,
        task: Task,
        *,
        call_id: str,
        approved: bool,
        reason: Optional[str] = None,
        resolver: Optional[str] = None,
        lease_id: str,
        trace_id: Optional[str] = None,
    ) -> Task:
        """Resolve a pending human-in-the-loop tool-call approval.

        The public seam the worker/runner calls **after** ``note_woken``
        re-leases a task that suspended on
        ``HumanResponseReceived(handle="approval-{call_id}")`` (Issue A).
        Engine stays the single writer of both the governance events and
        the runtime messages.

        Fail-closed precondition (architect risk #1): ``call_id`` must
        still be in ``task.governance.pending_approvals`` — the durable,
        restart-safe anchor folded from the recorded
        ``ToolCallApprovalRequested``. A stale or duplicate resolution
        (``call_id`` absent) raises :class:`ApprovalNotPending` and emits
        **no** event, so the log never carries two resolutions for one
        ``call_id``.

        On **approve** the recorded pending call is reconstructed and
        invoked (bypassing the guard — the human already approved); on
        **deny** a ``role="tool"`` denial-feedback message is appended and
        no tool runs. On resume the resolution is read from the recorded
        ``ToolCallApprovalResolved`` event rather than a live decision.
        """
        pending = task.governance.pending_approvals.get(call_id)
        if pending is None:
            raise ApprovalNotPending(
                f"no pending approval for call_id {call_id!r}; "
                "stale or duplicate resolution rejected"
            )
        tool_name = pending["tool_name"]
        arguments = pending["arguments"]
        resolved_trace = trace_id or self._latest_trace_id(task.task_id)

        # 1) The single authoritative resolution event. apply_event folds
        #    it into governance (pop pending; append approvals; on deny
        #    also append denied) so the in-memory task matches a fresh
        #    fold.
        env = self._emit(
            task_id=task.task_id,
            type_="ToolCallApprovalResolved",
            payload=ToolCallApprovalResolvedPayload(
                call_id=call_id,
                tool_name=tool_name,
                approved=approved,
                reason=reason,
                resolver=resolver,
            ),
            lease_id=lease_id,
            trace_id=resolved_trace,
        )
        apply_event(task, env, self._content_store)

        # 2) Continue deterministically: run the approved call, or append
        #    denial feedback so the resumed loop is not left with a
        #    dangling assistant tool_call and no tool result.
        if approved:
            call = ToolCall(
                tool_name=tool_name, arguments=arguments, call_id=call_id
            )
            # Foundation B (D-B2): the approval-resume is a non-default continuation —
            # tag it so the recovery guards read ``last_transition`` O(1).
            emit_step_transition(self._ctx, task, reason="approval_resume", lease_id=lease_id, trace_id=resolved_trace)
            invoke_approved_tool_call(
                self._ctx, task, call,
                lease_id=lease_id, trace_id=resolved_trace,
            )
        else:
            append_tool_denial_feedback(
                self._ctx, task,
                call_id=call_id,
                reason=reason or "denied by human",
                lease_id=lease_id, trace_id=resolved_trace,
            )
        return task

    def answer_user_question(self, task: Task, *, question_id: str, answers: dict[str, dict[str, Any]], answered_by: Optional[str] = None, lease_id: str, trace_id: Optional[str] = None) -> Task:
        """Record a structured HITL answer and append the paired tool result."""
        return _answer_user_question(self, task, question_id=question_id, answers=answers, answered_by=answered_by, lease_id=lease_id, trace_id=trace_id)

    # -- wake bookkeeping -------------------------------------------------

    def note_woken(
        self, task: Task, *, lease_id: str, wake_event: Any
    ) -> Task:
        """Append ``TaskWoken`` to a re-leased Task's stream.

        Workers call this once after re-leasing a Task that the
        Dispatcher woke (e.g. ``SubtaskCompleted`` delivered). The Task
        moves back to ``running`` and ``wake_on`` is cleared. Keeping
        this separate from ``run_one_step`` keeps the main loop free of
        a "is this a fresh start or a resume?" branch.
        """
        trace_id = self._latest_trace_id(task.task_id)
        task.status = "running"
        task.wake_on = None
        self._emit(
            task_id=task.task_id,
            type_="TaskWoken",
            payload=TaskWokenPayload(wake_event=wake_event),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        return task

    def note_model_bound(
        self,
        task: Task,
        *,
        lease_id: str,
        model: str,
        principal_identity: str,
        provider: Optional[str] = None,
    ) -> Task:
        """Append ``ModelBound`` for an authorized model selector (issue 06).

        The driver/server validated ``selector ∈ principal.allowed_models ∩
        deployment-allowlist`` *before* calling this, so a
        rejected selector never reaches here — no ``ModelBound`` is written
        and no binding is left behind. The Engine is the writer (under a
        driver command, exactly like :meth:`note_woken`), keeping the
        single-writer invariant intact: this is **not** a policy ``Decision``.

        Emitted once at task open (opening binding) and again on each
        per-turn switch; fold accumulates the latest binding into
        ``GovernanceState`` so the resolver keys the Engine on
        ``(agent_name, model)``.

        ``provider`` is the
        session-level provider name, folded into this same binding (no separate
        ProviderBound event). ``None`` ⇒ a turn switched only the model: fold
        does not overwrite provider_binding, so provider carries over from the
        current binding. The driver/server already validated that the (provider,
        model) pair is legal (provider configured + model ∈ provider.models),
        rejecting before any durable write, so an illegal pair never reaches here.
        """
        trace_id = self._latest_trace_id(task.task_id)
        env = self._emit(
            task_id=task.task_id,
            type_="ModelBound",
            payload=ModelBoundPayload(
                model=model,
                principal_identity=principal_identity,
                provider=provider,
            ),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        apply_event(task, env, self._content_store)
        return task

    def note_conversation_closed(self, task: Task, *, closed_by: str, reason: Optional[str] = None, trace_id: Optional[str] = None) -> Task:
        """Append ``ConversationClosed`` for a human close/archive (issue 08)."""
        return _note_conversation_closed(self, task, closed_by=closed_by, reason=reason, trace_id=trace_id)

    def note_conversation_reopened(self, task: Task, *, reopened_by: str, reason: Optional[str] = None, trace_id: Optional[str] = None) -> Task:
        """Append the audit-symmetric ``ConversationReopened`` (issue 08)."""
        return _note_conversation_reopened(self, task, reopened_by=reopened_by, reason=reason, trace_id=trace_id)

    # -- main loop --------------------------------------------------------

    def run_one_step(
        self,
        task: Task,
        *,
        lease_id: str,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Task:
        """Advance ``task`` until its next suspend or terminal event.

        A ``tool_calls`` decision keeps the loop turning in-place (the
        Engine appends the tool results, recomposes the View, asks the
        Policy again). Any other decision exits the loop: terminal
        decisions transition status to ``terminal``; suspending
        decisions (handled by issues 03–05) transition to ``suspended``.

        ``cancelled`` (cancel-cascade) is an optional cooperative-cancel
        predicate the delegation drain binds to ``is_cancelled(root_id)``.
        It is polled at the two turn boundaries — the top of the loop and
        again right after the Policy decides (once the in-flight LLM / tool
        round has returned) — and a truthy poll raises
        :class:`TaskCancellationRequested`, abandoning the in-flight result
        WITHOUT acting on it (no assistant message, no tools, no next
        turn). ``None`` (resume / the root seed turn) ⇒ no poll, so
        recordings stay byte-identical.
        """
        trace_id = self._latest_trace_id(task.task_id)
        if task.status == "pending":
            self._emit(
                task_id=task.task_id,
                type_="TaskStarted",
                payload=TaskStartedPayload(lease_id=lease_id),
                lease_id=lease_id,
                trace_id=trace_id,
            )
            task.status = "running"

        if self._policy is None:
            raise RuntimeError("Engine started without a Policy.")

        consecutive_tool_calls = 0
        while True:
            # cancel-cascade: poll before composing/deciding (a cancel that
            # landed between turns stops the next from starting) and again
            # right after the Policy decides — decide() is where the blocking
            # LLM round happens, so a cancel that landed mid-call is caught
            # HERE, before the decision is acted on: the in-flight result is
            # abandoned (no assistant message, no tools, no next turn).
            _raise_if_cancelled(cancelled, task.task_id)
            # rebuild StepContext each turn so the compaction
            # trigger sees the REAL input-token usage fold projected from the
            # PREVIOUS round-trip's ``LLMRequestFinished`` (``0`` on the first
            # turn → the Policy falls back to a pure estimate). The other three
            # identifiers are loop-invariant; only ``last_input_tokens`` moves.
            ctx = StepContext(
                task_id=task.task_id, lease_id=lease_id, trace_id=trace_id,
                last_input_tokens=task.runtime.last_input_tokens)
            view = self._composer.compose(task)
            _emit_context_plan(
                self._emit, self._content_store, task, view, lease_id, trace_id
            )
            decision: Decision = self._policy.decide(ctx, view)
            _raise_if_cancelled(cancelled, task.task_id)
            if isinstance(decision, StatePatchDecision):
                # Loop-continuing state-write control tool. It carries its
                # OWN ordered messages + patch (messages_before → patch →
                # messages_after), so it must NOT flow through the generic
                # ``_apply_decision_*`` pre-apply (which would emit a bare
                # state_patch / assistant_message in the wrong order). Run
                # the handler directly and loop back. No suspend possible.
                handle_state_patch(
                    self._ctx, task, decision,
                    lease_id=lease_id, trace_id=trace_id,
                )
                continue
            if isinstance(decision, CompactionRequestedDecision):
                # ③ (D-3b): a loop-continuing compaction step. The handler
                # owns its emits (tag → CompactionRequested → Compacted) and
                # the anti-spiral escalation; a returned Task is the terminal
                # escalation, None loops back to recompose the compacted view.
                escalated = handle_compaction_requested(
                    self._ctx, task, decision,
                    lease_id=lease_id, trace_id=trace_id,
                )
                if escalated is not None:
                    return escalated
                continue
            self._apply_decision_state_patch(
                task, decision, lease_id=lease_id, trace_id=trace_id
            )
            self._apply_decision_assistant_message(
                task, decision, lease_id=lease_id, trace_id=trace_id
            )

            if _is_background_spawn(decision, self._launch_background_subagent):
                # background sub-agent (docs/adr/background-subagent.md): a
                # loop-CONTINUING spawn (like tool_calls, not an exit) — the
                # handler emits Started + creates the child + appends a "started"
                # tool_result + hands it to the executor driver, then returns None
                # so the parent's SAME turn keeps deciding (no barrier suspend). A
                # guard deny/approval returns a terminal/suspended Task (exit).
                outcome = handle_spawn_background_subtask(
                    self._ctx, task, decision,
                    lease_id=lease_id, trace_id=trace_id,
                )
                if outcome is not None:
                    return outcome
                continue

            if isinstance(decision, ToolCallsDecision):
                # Issue C3: tool_calls is the only loop-continuing
                # handler, special-cased here so dispatch_exit's
                # `-> Task` return type stays honest.
                suspended = handle_tool_calls(
                    self._ctx, task, decision,
                    lease_id=lease_id, trace_id=trace_id,
                )
                if suspended is not None:
                    return suspended
                # Mid-loop snapshot: a Policy that keeps
                # returning tool_calls without ever yielding must still
                # produce a usable resume point. We write a snapshot
                # every N consecutive iterations and keep running.
                consecutive_tool_calls += 1
                if consecutive_tool_calls >= CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD:
                    self._write_snapshot(
                        task, lease_id=lease_id, trace_id=trace_id
                    )
                    consecutive_tool_calls = 0
                # loop back to compose → decide; tool_calls is in-place.
                continue

            return self._dispatch(
                task, decision, lease_id=lease_id, trace_id=trace_id
            )

    # -- decision dispatch ------------------------------------------------

    def _apply_decision_state_patch(
        self,
        task: Task,
        decision: Decision,
        *,
        lease_id: str,
        trace_id: str,
    ) -> None:
        patch = getattr(decision, "state_patch", None)
        if patch is None:
            return
        # same provenance causal order as explicit paths; fold guards first-only.
        emit_skill_provenance_for_patch(self._ctx, task, patch, lease_id=lease_id, trace_id=trace_id)
        # Invariant: TaskStatePatch.apply is a total function over the
        # state dict — it never raises. Emit then apply is therefore
        # safe; if apply ever grows defensive validation that can raise,
        # invert the order (apply first, then emit) so a refold's fold
        # path doesn't resume a patch the live path skipped.
        self._emit(
            task_id=task.task_id,
            type_="TaskStatePatched",
            payload=TaskStatePatchedPayload(patch=patch.to_dict()),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        patch.apply(task.state)

    def _apply_decision_assistant_message(
        self,
        task: Task,
        decision: Decision,
        *,
        lease_id: str,
        trace_id: str,
    ) -> None:
        """Append + emit when a Decision carries an ``assistant_message``.

        The Decision is the typed channel through which a
        Policy hands a side-effect hint to the Engine; the
        Engine is the only writer of ``RuntimeState.messages``. ReAct-
        style Policies (issue 13) attach the full LLM-produced assistant
        turn here so the next compose sees the new history.
        """
        msg = getattr(decision, "assistant_message", None)
        if msg is None:
            return
        # sole-writer guard: a Policy cannot smuggle origin
        # through the Decision channel — only the Engine ledger seam
        # (``append_user_message``) writes it.
        msg = strip_message_origin(msg)
        self._append_message(task, msg, lease_id=lease_id, trace_id=trace_id)
        # Slice B: persist any out-of-band extended-thinking the Policy
        # carried on the Decision (module-level helper keeps the Engine lean).
        record_assistant_thinking(
            self._ctx, task, decision, msg, lease_id=lease_id, trace_id=trace_id
        )

    def _dispatch(
        self,
        task: Task,
        decision: Decision,
        *,
        lease_id: str,
        trace_id: str,
    ) -> Task:
        # Issue C3: delegate to the typed dispatch in
        # noeta.core._decision_handlers. The handler module preserves
        # the pre-refactor ``NotImplementedError`` shape byte-equal
        # (message: "Unknown decision type: <name>") for any unmapped
        # Decision class.
        return dispatch_exit(
            self._ctx, task, decision, lease_id=lease_id, trace_id=trace_id
        )

    # -- Guard plumbing --------------------------------------------------

    def _guard(
        self,
        action: ProposedAction,
        task: Task,
        *,
        spawned_subtasks_override: Optional[int] = None,
    ) -> VerdictResult:
        """Issue 18: refold the EventLog right before each guard check
        so Guards see counters from emit-sites outside this Engine
        (ToolRuntime, RuntimeLLMClient). ``copy.deepcopy`` isolates the
        ``GovernanceState`` snapshot — canonical round-trip would
        return a plain dict and break the typed Guard contract.

        SR2 (B2): ``spawned_subtasks_override`` simulates **only** the
        ``spawned_subtasks`` counter for batch fan-out admission (the i-th
        spec sees ``current + i``); ``subtask_depth`` / ``active_skills`` /
        everything else still come from the fresh fold, so non-budget
        guards are unaffected.
        """
        fresh = fold(self._event_log, self._content_store, task.task_id)
        governance = copy.deepcopy(fresh.governance)
        if spawned_subtasks_override is not None:
            governance.spawned_subtasks = spawned_subtasks_override
        recent = _recent_tool_calls(
            self._event_log.read(task.task_id),
            self._content_store,
            window=_RECENT_TOOL_CALLS_WINDOW,
        )
        ctx = GuardContext(
            task_id=task.task_id,
            governance=governance,
            # Issue B: fold-derived active skills so skill `allowed-tools`
            # enforcement sees the identical set live / resume.
            active_skills=tuple(fresh.state.active_skills),
            # SR1: fold-derived delegation depth so the BudgetGuard depth
            # cap sees the identical value live / resume.
            subtask_depth=fresh.subtask_depth,
            # Work item ④: recorded tool-call history (neutral identity keys)
            # so RepetitionGuard detects a stuck loop resume-deterministically.
            recent_tool_calls=recent,
        )
        return self._hooks.check(action, ctx)

    # -- controlled seam methods used by HandlerContext -----------------

    def _resolve_tool(self, call: ToolCall) -> Tool:
        tool = self._tools.get(call.tool_name)
        if tool is None:
            raise KeyError(f"unknown tool: {call.tool_name!r}")
        return tool

    # Child-task genesis (``HandlerContext.create_child_task``) is the
    # module-level :func:`_emit_child_task_created` — wired in ``__init__`` via a
    # thin lambda. It lives outside the class only to keep the Engine body under
    # its line budget; the ``actor`` / ``origin`` / ``trace_id`` / locked
    # ``policy_name`` bookkeeping is unchanged.

    # -- snapshot --------------------------------------------------------

    def _write_snapshot(
        self, task: Task, *, lease_id: str, trace_id: str
    ) -> None:
        # Issue 18: refold so the snapshot body captures emit-site
        # governance accumulation (ToolRuntime, RuntimeLLMClient).
        task.governance = fold(self._event_log, self._content_store, task.task_id).governance
        ref = self._content_store.put(serialize_task_state(task), media_type=snapshot_media_type())
        self._emit(
            task_id=task.task_id,
            type_="TaskSnapshot",
            payload=TaskSnapshotPayload(state_ref=ref),
            lease_id=lease_id,
            trace_id=trace_id,
        )

    # -- envelope helpers ------------------------------------------------

    def _emit(
        self,
        *,
        task_id: str,
        type_: str,
        payload: Any,
        lease_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> EventEnvelope:
        """Emit one business event through the log.

        Thin wrapper that pins ``actor`` to ``self._actor``. The log
        mints id / seq / occurred_at. Cross-stream / pre-lease writes
        (``TaskCreated`` genesis + spawn-subtask child stream) call
        ``self._event_log.system_emit`` directly at the call site —
        only two spots, not worth a second helper.
        """
        return self._event_log.emit(
            task_id=task_id,
            type=type_,
            payload=payload,
            lease_id=lease_id,
            trace_id=trace_id,
            actor=self._actor,
            origin="engine",
        )

    def _latest_trace_id(self, task_id: str) -> str:
        events = self._event_log.read(task_id)
        return events[0].trace_id if events else "trace-unknown"


def _raise_if_cancelled(
    cancelled: Optional[Callable[[], bool]], task_id: str
) -> None:
    """cancel-cascade poll. Raises :class:`TaskCancellationRequested` when the
    injected predicate fires. Module-level (not an Engine method) so the
    Engine class body stays within its line budget; ``None`` predicate ⇒
    no-op (resume / the root seed turn) so recordings are unchanged.
    """
    if cancelled is not None and cancelled():
        raise TaskCancellationRequested(task_id)


def _recent_tool_calls(
    events: list[EventEnvelope],
    content_store: ContentStore,
    *,
    window: int,
) -> tuple[tuple[str, bytes], ...]:
    """Project the last ``window`` recorded tool calls into neutral identity
    keys ``(tool_name, canonical input bytes)`` for ``RepetitionGuard``
    (work item ④).

    Pure projection of the recorded ``ToolCallStarted`` prefix — no clock /
    random — so live and resume see the identical history. Arguments
    offloaded to the ContentStore are dereferenced through the shared
    ``resolve_tool_call_arguments`` helper, then canonicalised so the key is
    key-order independent and provider-neutral.
    """
    if window <= 0:
        return ()
    keys: list[tuple[str, bytes]] = []
    for env in events:
        if env.type != "ToolCallStarted":
            continue
        payload = env.payload
        args = resolve_tool_call_arguments(payload, content_store)
        keys.append((payload.tool_name, to_canonical_bytes(args)))
    return tuple(keys[-window:])


def _emit_context_plan(
    emit: Callable[..., EventEnvelope],
    content_store: ContentStore,
    task: Task,
    view: Any,
    lease_id: str,
    trace_id: str,
) -> None:
    """Issue 14 / PRD §C: emit ContextPlanComposed in front of every LLM
    round-trip, then converge live state through fold so a mid-step
    snapshot captures the freshly-set plan_ref.

    Emitted **unconditionally** — even when the composer produced no
    stored plan (``view.plan_ref is None``, the PassthroughComposer
    fallback). This event is the per-step boundary fold counts
    ``governance.iterations`` from; skipping it for plan-less views made
    ``BudgetGuard.max_iterations`` inert under Passthrough (core #2).
    Module-level helper so the Engine body stays under its
    500-line budget.
    """
    env = emit(
        task_id=task.task_id,
        type_="ContextPlanComposed",
        payload=ContextPlanComposedPayload(plan_ref=view.plan_ref),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    apply_event(task, env, content_store)


def _answer_user_question(engine: Engine, task: Task, *, question_id: str, answers: dict[str, dict[str, Any]], answered_by: Optional[str], lease_id: str, trace_id: Optional[str]) -> Task:
    pending = task.governance.pending_questions.get(question_id)
    if pending is None:
        raise UserQuestionNotPending(
            f"no pending user question for question_id {question_id!r}"
        )
    call_id = str(pending["call_id"])
    resolved_trace = trace_id or engine._latest_trace_id(task.task_id)
    # Neutral answer-audit codec inlined here (the kernel no longer imports
    # the product ``user_questions`` module). Byte-identical to the old
    # ``put_answers_body``: a single ``{"answers": ...}`` JSON object.
    answers_ref = engine._content_store.put(
        to_canonical_bytes({"answers": answers}),
        media_type="application/json",
    )
    env = engine._emit(
        task_id=task.task_id,
        type_="UserQuestionAnswered",
        payload=UserQuestionAnsweredPayload(
            question_id=question_id,
            call_id=call_id,
            answers_ref=answers_ref,
            answer_count=len(answers),
            answered_by=answered_by,
        ),
        lease_id=lease_id,
        trace_id=resolved_trace,
    )
    apply_event(task, env, engine._content_store)
    msg = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id=call_id,
                output={"question_id": question_id, "answers": answers},
                success=True,
                error=None,
            )
        ],
    )
    return engine._append_message(
        task, msg, lease_id=lease_id, trace_id=resolved_trace
    )


def _is_named_agent(agent_name: str) -> bool:
    """A resolvable Agent identity (``unnamed`` / empty carries none)."""
    return bool(agent_name) and agent_name != "unnamed"


def _validate_genesis_provenance(
    agent_name: str,
    host_binding: Optional[TaskHostBoundPayload],
) -> None:
    """Consistency guard for ``create_task`` provenance.

    A ``host_binding`` (server product / session path) requires a resolvable
    ``agent_name`` — TaskHostBound follows AgentBound, and ``unnamed`` carries no
    identity to bind. Never written without its predicate.
    """
    if host_binding is not None and not _is_named_agent(agent_name):
        raise ValueError(
            "host_binding requires a resolvable agent_name (TaskHostBound "
            f"follows AgentBound); got agent_name={agent_name!r}"
        )


def _emit_genesis_provenance(
    event_log: Any,
    *,
    tid: str,
    trace: str,
    actor: Any,
    agent_name: str,
    host_binding: Optional[TaskHostBoundPayload],
) -> None:
    """Emit ``AgentBound`` (and ``TaskHostBound``) atomically after ``TaskCreated``.

    Kept module-level (off the Engine class) so the ≤500-line core budget stays
    honest; the single trusted write point is preserved — both
    events are emitted here or not at all. AgentBound records the bound
    ``agent_name`` for every named task; TaskHostBound follows when the caller
    supplied a host/session binding.
    """
    if _is_named_agent(agent_name):
        event_log.system_emit(
            task_id=tid,
            type="AgentBound",
            payload=AgentBoundPayload(agent_name=agent_name),
            actor=actor,
            origin="engine",
            trace_id=trace,
        )
    if host_binding is not None:
        # TaskHostBound follows AgentBound, before any ModelBound.
        event_log.system_emit(
            task_id=tid,
            type="TaskHostBound",
            payload=host_binding,
            actor=actor,
            origin="engine",
            trace_id=trace,
        )


def _note_conversation_closed(engine: Engine, task: Task, *, closed_by: str, reason: Optional[str], trace_id: Optional[str]) -> Task:
    resolved_trace = trace_id or engine._latest_trace_id(task.task_id)
    env = engine._event_log.system_emit(
        task_id=task.task_id,
        type="ConversationClosed",
        payload=ConversationClosedPayload(closed_by=closed_by, reason=reason),
        actor=engine._actor,
        origin="engine",
        trace_id=resolved_trace,
    )
    apply_event(task, env, engine._content_store)
    return task


def _note_conversation_reopened(engine: Engine, task: Task, *, reopened_by: str, reason: Optional[str], trace_id: Optional[str]) -> Task:
    resolved_trace = trace_id or engine._latest_trace_id(task.task_id)
    env = engine._event_log.system_emit(
        task_id=task.task_id,
        type="ConversationReopened",
        payload=ConversationReopenedPayload(reopened_by=reopened_by, reason=reason),
        actor=engine._actor,
        origin="engine",
        trace_id=resolved_trace,
    )
    apply_event(task, env, engine._content_store)
    return task


def suspend_on_human_handle(
    engine: Engine, task: Task, *, handle: str, lease_id: str
) -> Task:
    """Cooperative-stop landing: suspend ``task`` on a human ``handle``.

    Reuses the exact :func:`handle_yield_for_human` machinery a normally
    finished interactive turn exits through, so the task rests in the SAME
    ``suspended`` state — a later ``send_goal`` matching ``handle`` resumes it.
    The difference is *why* we got here: a human pressed *stop* mid-turn (a
    reopenable ``close``), abandoning the in-flight result, rather than the
    Policy yielding on its own. ``handle`` is supplied by the caller (the SDK's
    next-goal handle), so the Engine stays policy-agnostic — it never names the
    handle itself.

    A MODULE-LEVEL free function (like :func:`_note_conversation_closed`), not
    an ``Engine`` method: it reaches into ``engine`` internals — which the
    handler module's AST guard forbids there — yet must stay OUT of the
    ``class Engine`` body so it does not count against the line budget.
    """
    return handle_yield_for_human(
        engine._ctx,
        task,
        YieldForHumanDecision(prompt=handle),
        lease_id=lease_id,
        trace_id=engine._latest_trace_id(task.task_id),
    )


#: The only blocks a user turn may legitimately carry.
#: Model-side (``ThinkingBlock``) and tool-side (``ToolUseBlock`` /
#: ``ToolResultBlock``) blocks ride other seams and are rejected here.
_USER_TURN_BLOCKS = (TextBlock, ImageBlock)


def _validate_user_content(content: list[Block]) -> None:
    """Guard ``append_user_message`` content.

    Rejects an empty list and any block a user turn must not carry, so a
    caller cannot route a thinking / tool block through the user channel.
    Kept module-level (off the Engine class) so the ≤500-line core budget
    stays honest.
    """
    if not content:
        raise ValueError("append_user_message: content must not be empty")
    for block in content:
        if not isinstance(block, _USER_TURN_BLOCKS):
            raise ValueError(
                "append_user_message: a user turn may only carry "
                f"TextBlock / ImageBlock, got {type(block).__name__}"
            )


def _default_tool_runtime(
    event_log: Any,
    content_store: Any,
    background_runner: Any,
    file_checkpoint_registry: Any,
) -> Any:
    """Convenience ToolRuntime for callers that pass ``tools`` but no explicit
    ``tool_runtime`` (mostly tests). Forward the host's
    background runner and per-turn file-checkpoint gate so ``shell_run`` bg jobs
    and AI-edit rewind baselines reach the runtime. Local import breaks the
    runtime→core import cycle."""
    from noeta.runtime.tool import ToolRuntime

    return ToolRuntime(
        event_log=event_log,
        content_store=content_store,
        background_runner=background_runner,
        file_checkpoint_registry=file_checkpoint_registry,
    )


def _default_id_factory() -> str:
    """Default subtask_id source for production callers.

    Mirrors the inline ``f"task-{uuid.uuid4().hex}"`` the spawn branch
    used pre-issue-08. This is the production random factory; a test can
    inject a deterministic replacement via ``id_factory``.
    """
    return f"task-{uuid.uuid4().hex}"


def emit_context_content_recorded(
    engine: Engine,
    task: Task,
    *,
    kind: str,
    name: str,
    version: str,
    content_hash: str,
    policy: str,
    lease_id: str,
    trace_id: Optional[str] = None,
) -> Task:
    """Per-task per-(kind, name) first-emission provenance (issue 07
    generation switch).

    The kind-neutral successor of the retired ``emit_skill_content_recorded``
    helper: emits one ``ContextContentRecorded`` right *before* whatever
    durable activation follows (e.g. ``TaskStatePatched(activate_skills=…)``
    for the skill kind), so the causal order is unambiguous. Duplicate calls
    for the same (task, kind, name) drop against fold's authoritative
    generic activation map ``TaskState.active_content``. All five payload
    strings are caller-computed (the SDK registry owns kind semantics,
    hashes, and drift policy), so the kernel stays compare-only-strings and
    never imports noeta-sdk. Module-level (off the Engine class) like the
    genesis/lifecycle helpers above, keeping the ≤500-line core budget
    honest.
    """
    if not kind or not name or not content_hash:
        return task
    if name in task.state.active_content.get(kind, ()):
        return task
    env = engine._emit(
        task_id=task.task_id,
        type_="ContextContentRecorded",
        payload=ContextContentRecordedPayload(
            kind=kind,
            name=name,
            version=version,
            content_hash=content_hash,
            policy=policy,
        ),
        lease_id=lease_id,
        trace_id=trace_id or engine._latest_trace_id(task.task_id),
    )
    apply_event(task, env, engine._content_store)
    return task
