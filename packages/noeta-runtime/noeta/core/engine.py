"""Engine: drives one Task from ``compose → decide`` to its next suspend
point or terminal event.

The Engine is the single writer of ``RuntimeState`` / ``TaskState``: every
mutation is emitted as an event and folded back through
:mod:`noeta.core.fold`, so a resume that refolds the prefix lands on exactly
the state the live turn saw. It knows nothing about the Dispatcher or any
Observer — the parent/child handoff lives entirely in
:class:`noeta.core.observers.ChildLifecycleObserver`.
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
from noeta.core.fold import apply_event, apply_host_binding, fold
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
    MessagesAppendedPayload,
    ModelBoundPayload,
    StepAttemptAbandonedPayload,
    TaskCreatedPayload,
    spill_goal,
    TaskSnapshotPayload,
    TaskStartedPayload,
    TaskStatePatchedPayload,
    TaskWokenPayload,
    ToolCallApprovalResolvedPayload,
    UserQuestionAnsweredPayload,
    UserQuestionWithdrawnPayload,
)
from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedToolCall,
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


#: Upper bound on the recent tool-call history the Engine folds into
#: ``GuardContext.recent_tool_calls``. Generous enough to cover any sane
#: repetition threshold; ``RepetitionGuard`` truncates to its own
#: ``policy.window`` when counting the consecutive run.
_RECENT_TOOL_CALLS_WINDOW = 32


def _emit_child_task_created(
    event_log: EventLog,
    content_store: ContentStore,
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
    """Cross-stream ``system_emit`` of a child's ``TaskCreated``.

    The one cross-stream system write a handler needs, kept here so the
    ``actor`` / ``origin`` / ``trace_id`` bookkeeping for child-task genesis
    lives in one place. ``background=True`` marks the child so the
    ``ChildLifecycleObserver`` skips it. An oversized ``goal`` spills to the
    ContentStore (``spill_goal``) instead of blowing the payload cap —
    content-addressed, so it lands on the same ref the parent's
    ``SubtaskSpawned`` spill wrote."""
    goal_inline, goal_ref = spill_goal(content_store, goal)
    return event_log.system_emit(
        task_id=child_task_id,
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal=goal_inline,
            policy_name=policy_name,
            agent_name=agent_name,
            parent_task_id=parent_task_id,
            inputs=dict(inputs),
            subtask_depth=subtask_depth,
            background=True if background else None,
            goal_ref=goal_ref,
        ),
        actor=actor,
        origin="engine",
        trace_id=trace_id,
    )


def _background_subagent_seams(
    launcher: Optional[Any],
) -> tuple[Optional[Any], Optional[Any]]:
    """Split a duck-typed background-sub-agent launcher into the two
    ``HandlerContext`` seams ``(launch, capacity)``; ``(None, None)`` when no
    launcher is wired."""
    if launcher is None:
        return None, None
    return launcher.launch, launcher.capacity


def _is_background_spawn(decision: Any, launch_seam: Optional[Any]) -> bool:
    """True iff this decision is a background ``spawn_subagent`` AND a launcher
    is wired. Without a launcher the decision must fall through to the
    foreground barrier spawn in ``dispatch_exit``, so a resume or a child
    engine never launches anything concurrently."""
    return (
        isinstance(decision, SpawnSubtaskDecision)
        and bool(decision.background)
        and launch_seam is not None
    )


class Engine:
    """Drives one Step toward suspend or terminal.

    Holds the compose → decide loop, the state_patch / assistant_message
    helpers that fire before every branch handler, and the controlled callable
    seams (``_emit`` / ``_guard`` / ``_write_snapshot`` / ``_resolve_tool`` /
    ``_create_child_task``). Per-Decision branch logic lives in
    :mod:`noeta.core._decision_handlers`, which reaches the EventLog and the
    HookManager only through those seams.
    """

    #: The ``policy_name`` every engine-created child task is born with; the
    #: Engine picks no policy of its own.
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
        content_discovery: Optional[Any] = None,
        content_preloader: Optional[Any] = None,
        content_init_hooks: tuple[Any, ...] = (),
        tool_result_transforms: tuple[Any, ...] = (),
        answer_codec: Optional[Any] = None,
        injection_inbox: Optional[Any] = None,
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        # The kernel holds no opinion on View assembly and never reaches up
        # into a concrete Composer, so this is a required injection; callers
        # wanting the empty View pass ``PassthroughComposer``.
        self._composer = composer
        self._policy = policy
        self._tools = dict(tools or {})
        # Reject non-positive limits centrally so every construction path
        # (live host, resume) shares one error. ``None`` disables truncation.
        _validate_tool_output_inline_limit(tool_output_inline_limit)
        self._tool_output_inline_limit = tool_output_inline_limit
        if tool_runtime is None and (tools or tool_result_transforms):
            # Transforms alone are enough to build a runtime, so an activated
            # redaction/transform stage is never silently dropped just because
            # this engine was compiled with no tools.
            tool_runtime = _default_tool_runtime(
                event_log, content_store, background_runner,
                file_checkpoint_registry, tool_result_transforms)
        elif tool_runtime is not None and tool_result_transforms:
            # An injected runtime carries its own transform chain, so honouring
            # these would mean reaching into someone else's wrapper. Refuse
            # loudly: silently ignoring them turns an activated redaction
            # plugin into a no-op while every listing still reports it as wired.
            raise ValueError(
                "tool_result_transforms cannot be applied to an injected "
                "tool_runtime — pass them to ToolRuntime(tool_result_transforms=…) "
                "when you construct it"
            )
        self._tool_runtime = tool_runtime
        self._hooks = hooks or HookManager()
        self._clock = clock or time.time
        # The subtask_id source. Random by default; a test injects a
        # deterministic factory to make spawn streams reproducible.
        self._id_factory: Callable[[], str] = (
            id_factory if id_factory is not None else _default_id_factory
        )
        self._actor = actor

        # Handlers receive the HandlerContext by value and reach EventLog /
        # HookManager / ContentStore only through the typed callables wired
        # here — no raw log or hook-manager reference escapes into the handler
        # module, so a handler cannot bypass the single-writer invariant.
        def _apply_event(task: Task, env: EventEnvelope) -> None:
            apply_event(task, env, self._content_store)

        # The launcher is duck-typed (``.launch`` / ``.capacity``) so
        # ``noeta.core`` never imports the executor-driven registry up in
        # ``noeta.execution``. ``None`` everywhere but a top-level interactive
        # Engine, which is what keeps a resume from re-launching.
        bg_launch, bg_capacity = _background_subagent_seams(
            background_subagent_launcher
        )
        self._launch_background_subagent = bg_launch
        # Impure host hook run at the top of each step to re-supply renderer
        # state the ledger says is active.
        self._content_preloader = content_preloader
        # mid-turn goal injection — the process-local inbox an HTTP-thread
        # ``inject_goal`` submits to. The step loop drains it at each turn
        # boundary (next to the cancel poll) and delivers each pending injection
        # as a real ``MessagesAppended``. ``None`` (resume double / test host
        # without the seam) ⇒ the loop still drains the DURABLE
        # ``governance.pending_injections`` folded from the log, so a resumed
        # turn re-delivers an injection the crash interrupted; the inbox is only
        # the live-path accelerator. Duck-typed (``snapshot`` / ``consume``) so
        # the kernel never imports the runtime inbox class.
        self._injection_inbox = injection_inbox
        # Pre-loop ``init`` hooks the driver runs at seed time; the Engine
        # itself never invokes them. ``()`` ⇒ no pre-loop residents.
        self._content_init_hooks = content_init_hooks
        # Duck-typed answer codec so the driver can decode a submitted answer
        # without importing the ``ask_user_question`` built-in (the kernel
        # never imports ``noeta.builtins``). ``None`` when the session mounted
        # no ask capability — an answer arriving for it fails loudly in the
        # driver. Public because the driver reads it structurally.
        self.answer_codec = answer_codec

        self._ctx = HandlerContext(
            emit=self._emit,
            create_child_task=(
                lambda **kw: _emit_child_task_created(
                    self._event_log, self._content_store, self._actor,
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
            content_discovery=content_discovery,
        )

    @property
    def content_init_hooks(self) -> tuple[Any, ...]:
        """Pre-loop ``init`` hooks, read by the driver's seed path."""
        return self._content_init_hooks

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

        A named task's genesis sequence ``TaskCreated → AgentBound`` is emitted
        **atomically inside this call** — one trusted write point, so a named
        Task can never be created without its durable Agent identity record.
        ``host_binding`` adds a ``TaskHostBound`` after the ``AgentBound``; an
        ``unnamed`` task carries no identity and emits neither.
        """
        _validate_genesis_provenance(agent_name, host_binding)
        tid = task_id or f"task-{uuid.uuid4().hex}"
        trace = trace_id or f"trace-{uuid.uuid4().hex}"
        # An oversized goal spills to the ContentStore (goal_ref) — the genesis
        # event has no other escape from the payload cap, and a host-supplied
        # goal is unbounded text. The returned Task keeps the full goal.
        goal_inline, goal_ref = spill_goal(self._content_store, goal)
        payload = TaskCreatedPayload(
            goal=goal_inline,
            policy_name=policy_name,
            agent_name=agent_name,
            parent_task_id=parent_task_id,
            inputs=dict(inputs or {}),
            goal_ref=goal_ref,
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
        task = Task(
            task_id=tid,
            status="pending",
            parent_task_id=parent_task_id,
            state=TaskState(goal=goal),
        )
        # The returned Task must agree with the ``TaskHostBound`` just emitted:
        # ``resolve_engine`` reads the session's workspace / container off this
        # slice, and ``InteractionDriver.seed_start`` resolves an Engine from
        # THIS object — before anything folds the stream back. Left unfolded,
        # that resolve silently falls back to the host-fixed default workspace
        # with no ExecEnv, and the pre-loop content init it drives captures the
        # workspace-environment resident against the WRONG root. That resident
        # is activate-once (``refresh=False``), so the mis-captured block is the
        # task's for life. Same ``apply_host_binding`` the fold handler uses, so
        # the two can never drift.
        if host_binding is not None:
            apply_host_binding(task, host_binding)
        return task

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

        The only sanctioned way to inject user input between ``create_task``
        and ``run_one_step``. Mutating ``task.runtime.messages`` directly would
        break the single-writer invariant and surface as an ``llm_args``
        divergence between the live turn and a refold.

        Only the blocks a user turn may legitimately carry are accepted
        (``TextBlock`` / ``ImageBlock``), so a caller cannot smuggle a
        model-side or tool-side block into the user channel.

        ``origin`` is the **sole writer seam** for ``Message.origin``:
        Policy-supplied messages get origin stripped at the Decision seams, so
        a value in the ledger always means "the host said so at this seam".
        """
        _validate_user_content(content)
        msg = Message(role="user", content=content, origin=origin)
        return self._append_message(task, msg, lease_id=lease_id, trace_id=trace_id)

    def _append_message(
        self, task: Task, msg: Message, *, lease_id: str, trace_id: Optional[str]
    ) -> Task:
        """Emit one ``MessagesAppended`` and mirror it into
        ``task.runtime.messages`` — the shared tail of every message-append
        seam, which is what keeps Engine the single writer."""
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
        """Append the paired ``role="tool"`` result for a delegated sub-agent.

        After a parent wakes from a ``SubtaskCompleted``, this narrow seam
        renders the child's ``SubtaskResult`` as a ``ToolResultBlock`` paired
        to the originating ``spawn_subagent`` ``tool_use`` ``call_id``, so the
        dangling delegation call gets its matching result and the child's
        outcome enters the parent's next compose. Engine stays the single
        writer of ``RuntimeState.messages``; the caller never appends directly.

        On failure the child's own ``error`` is surfaced verbatim so a caller
        can report *why* its helper failed.
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
        """Render a fan-out group's N child results as **one**
        ``MessagesAppended`` carrying N ``ToolResultBlock``s in **member (spawn)
        order**.

        ``wake_event`` is the consumed ``SubtaskGroupCompleted`` (gives the
        ordered ``subtask_ids``); ``call_ids`` is the positional pairing of
        originating ``spawn_subagent`` call ids (member order, supplied by the
        caller from the assistant message — a batch call carrying a ``spawns``
        array contributes its id once per entry, contiguously). The per-child
        results are read from the parent stream's keyed ``SubtaskCompleted``
        events — NOT the unkeyed ``governance.subtask_results``. Per-block
        normalization matches the single-child seam (``output`` never ``null``).
        Engine stays the single writer.

        Wire correctness pins the block shape: exactly ONE ``ToolResultBlock``
        per originating call. A one-member run renders one block; a k>1 run (one
        batch call) renders one block whose ``output`` lists the k member
        results in entry order (``{"spawn": i, "success": …, "output": …[,
        "error": …]}``), ``success`` = all members succeeded.
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
        pairs = list(zip(subtask_ids, call_ids))
        blocks: list[Block] = []
        start = 0
        while start < len(pairs):
            end = start
            while end < len(pairs) and pairs[end][1] == pairs[start][1]:
                end += 1
            call_id = pairs[start][1]
            if end - start == 1:
                r = results[pairs[start][0]]
                success = r.status == "completed"
                blocks.append(
                    ToolResultBlock(
                        call_id=call_id,
                        output=self._deref_subagent_output(r.output),
                        success=success,
                        error=None if success else (r.error or "sub-agent failed"),
                    )
                )
            else:
                members: list[dict[str, Any]] = []
                failed = 0
                for index, (sid, _) in enumerate(pairs[start:end]):
                    r = results[sid]
                    ok = r.status == "completed"
                    entry: dict[str, Any] = {
                        "spawn": index,
                        "success": ok,
                        "output": self._deref_subagent_output(r.output),
                    }
                    if not ok:
                        failed += 1
                        entry["error"] = r.error or "sub-agent failed"
                    members.append(entry)
                blocks.append(
                    ToolResultBlock(
                        call_id=call_id,
                        output=members,
                        success=failed == 0,
                        error=(
                            None if failed == 0
                            else f"{failed} of {end - start} spawns failed"
                        ),
                    )
                )
            start = end
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
        """Apply an operator-driven ``TaskStatePatch``.

        Emits the durable ``TaskStatePatched`` event so a resume reproduces the
        same active set (``ContextPlan.selected_skills``) without depending on
        the model emitting ``activate_skills``.

        Engine remains the single writer of ``TaskState``; callers MUST hold a
        valid lease. This is a parallel operator-side entry to the Policy-side
        ``_apply_decision_state_patch``, emitting the same event type so fold /
        resume handle both identically.

        A patch carrying ``activate_skills`` automatically emits one
        content-provenance event per skill (per-task first-only, fold-guarded)
        right before the ``TaskStatePatched`` event — the generic
        ``ContextContentRecorded`` (kind="skill", policy="pinned") via the
        ``content_hashes`` seam, matching the causal order the pre-loop SDK
        helper produces. With no resolver wired the emission is skipped.
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

    def record_content(
        self,
        task: Task,
        *,
        kind: str,
        name: str,
        version: str,
        body: bytes,
        media_type: str = "text/markdown",
        policy: str,
        refresh: bool = True,
        lease_id: str,
        trace_id: Optional[str] = None,
    ) -> Task:
        """Activate or refresh a content-channel resident mid-task.

        The in-turn twin of the seed window's ``SessionRecorder.record_content``
        (:class:`noeta.execution.recorder.SeedRecorder`), same gate: ``put()``
        the bytes, then record ``ContextContentRecorded`` unless ``(kind,
        name)`` is already active at this exact hash; ``refresh=False`` narrows
        that to first-write-wins — active at ANY hash appends nothing. Fold
        stamps the activation anchor at the current rolling-history length, so a
        resident recorded right after a goal append renders right there
        (anchored placement) rather than rewriting the head segments. Callers
        MUST hold a valid lease; the Engine stays the single writer.
        """
        if not kind or not name:
            return task
        active = task.state.active_content.get(kind, {}).get(name)
        if active is not None and not refresh:
            return task
        ref = self._content_store.put(body, media_type=media_type)
        return emit_context_content_recorded(
            self,
            task,
            kind=kind,
            name=name,
            version=version,
            content_hash=ref.hash,
            policy=policy,
            lease_id=lease_id,
            trace_id=trace_id,
        )

    # -- operator-driven tool-call approval --------------------------------

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
        ``HumanResponseReceived(handle="approval-{call_id}")``. Engine stays the
        single writer of both the governance events and the runtime messages.

        Fail-closed precondition: ``call_id`` must still be in
        ``task.governance.pending_approvals`` — the durable, restart-safe anchor
        folded from the recorded ``ToolCallApprovalRequested``. A stale or
        duplicate resolution (``call_id`` absent) raises
        :class:`ApprovalNotPending` and emits **no** event, so the log never
        carries two resolutions for one ``call_id``.

        On **approve** the recorded pending call is reconstructed and invoked
        (bypassing the guard — the human already approved); on **deny** a
        ``role="tool"`` denial-feedback message is appended and no tool runs. On
        resume the resolution is read from the recorded
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

        # The single authoritative resolution event. apply_event folds it into
        # governance (pop pending; append approvals; on deny also append denied)
        # so the in-memory task matches a fresh fold.
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

        # Continue deterministically: run the approved call, or append denial
        # feedback so the resumed loop is not left with a dangling assistant
        # tool_call and no tool result.
        if approved:
            call = ToolCall(
                tool_name=tool_name, arguments=arguments, call_id=call_id
            )
            # The approval-resume is a non-default continuation — tag it so the
            # recovery guards read ``last_transition`` O(1).
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

    def withdraw_user_question(self, task: Task, *, question_id: str, withdrawn_by: Optional[str] = None, reason: Optional[str] = None, lease_id: str, trace_id: Optional[str] = None) -> Task:
        """Drop a pending HITL question without an answer and append the paired
        withdrawal tool result (the ``interrupt``/Stop-on-question landing)."""
        return _withdraw_user_question(self, task, question_id=question_id, withdrawn_by=withdrawn_by, reason=reason, lease_id=lease_id, trace_id=trace_id)

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
        """Append ``ModelBound`` for an authorized model selector.

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
        """Append ``ConversationClosed`` for a human close/archive."""
        return _note_conversation_closed(self, task, closed_by=closed_by, reason=reason, trace_id=trace_id)

    def note_conversation_reopened(self, task: Task, *, reopened_by: str, reason: Optional[str] = None, trace_id: Optional[str] = None) -> Task:
        """Append the audit-symmetric ``ConversationReopened``."""
        return _note_conversation_reopened(self, task, reopened_by=reopened_by, reason=reason, trace_id=trace_id)

    def _drain_injections(
        self, task: Task, *, lease_id: str, trace_id: str
    ) -> Task:
        """Deliver every pending mid-turn injection as a real ``MessagesAppended``.

        Called at the top-of-loop boundary in :meth:`run_one_step`. The pending
        set is the **union** of two sources, deduped by ``injection_id``:

        * the live process-local inbox (``self._injection_inbox``) — an HTTP
          thread's ``inject_goal`` wrote ``InjectionRequested`` to the log and
          poked the inbox, but the Engine's in-memory ``task`` never folded that
          cross-thread event, so the inbox is the only live-path signal;
        * the DURABLE ``task.governance.pending_injections`` folded from the log
          — the resume source (a fresh process has an empty inbox) and the
          re-scan a crash-interrupted turn needs.

        Anything still in either source genuinely needs delivery: a durable
        marker is popped only by its consuming ``MessagesAppended`` (see
        ``_on_messages_appended``), so its presence means "not yet delivered".
        Each is delivered in arrival order as a ``MessagesAppended`` carrying
        ``consumes_injection=id`` and the stored ``messages_ref``
        (content-addressed → reused, never re-put); fold appends the message and
        pops the marker in one reduction — exactly-once by construction.
        ``apply_event`` folds it onto the live task so the next ``compose`` sees
        it and a fresh fold matches. Nothing pending ⇒ no emit ⇒ byte-identical.
        """
        inbox = self._injection_inbox
        live = inbox.snapshot(task.task_id) if inbox is not None else {}
        # Durable set first (fold preserves arrival order), then any live inbox
        # entries not yet folded onto this in-memory task. dict union keeps
        # first-seen order and dedups on ``injection_id``.
        pending = {**task.governance.pending_injections, **live}
        if not pending:
            return task
        for injection_id, descriptor in pending.items():
            env = self._emit(
                task_id=task.task_id,
                type_="MessagesAppended",
                payload=MessagesAppendedPayload(
                    messages_ref=descriptor["messages_ref"],
                    count=descriptor["count"],
                    consumes_injection=injection_id,
                ),
                lease_id=lease_id,
                trace_id=trace_id,
            )
            apply_event(task, env, self._content_store)
            if inbox is not None:
                inbox.consume(task.task_id, injection_id)
        return task

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
        decisions transition to ``suspended``.

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
        # Resume preload: give the host one impure hook BEFORE the first compose
        # of this step to re-supply renderer state the ledger says is active
        # (e.g. discovered instruction files a fresh process has not read yet).
        # Best-effort — a broken preload may only omit context, never fail the
        # step — and a no-op for every host that wires nothing.
        if self._content_preloader is not None:
            try:
                self._content_preloader(task)
            except Exception:  # noqa: BLE001 — preload is best-effort.
                pass
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
        # The per-turn step index threaded through StepContext. A step-capped
        # Policy (ReAct ``max_steps``) reads it instead of keeping an instance
        # counter: the Engine — and therefore its Policy — is cached across
        # turns and tasks, so any instance counter would accumulate for the
        # cache entry's whole life and eventually fail every turn that shares
        # it. A local counter here resets per drive by construction.
        steps_in_turn = 0
        while True:
            # cancel-cascade: poll before composing/deciding (a cancel that
            # landed between turns stops the next from starting) and again
            # right after the Policy decides — decide() is where the blocking
            # LLM round happens, so a cancel that landed mid-call is caught
            # HERE, before the decision is acted on: the in-flight result is
            # abandoned (no assistant message, no tools, no next turn).
            _raise_if_cancelled(cancelled, task.task_id)
            # mid-turn goal injection: deliver any pending injected user
            # message HERE, at the clean top-of-loop boundary — the prior
            # iteration's tool_calls handler has already appended its
            # tool_result(s) before ``continue``, so the last message is never a
            # dangling ``tool_use`` and an injected ``user`` message can never
            # split a tool_use/tool_result pair. Each delivery is a real
            # ``MessagesAppended`` carrying the injection's id, so fold appends
            # the message and pops the pending marker in one reduction; the next
            # ``compose`` below sees it. A turn with nothing pending emits
            # nothing → byte-identical to the pre-injection recording.
            task = self._drain_injections(
                task, lease_id=lease_id, trace_id=trace_id
            )
            # rebuild StepContext each turn so the compaction trigger sees the
            # REAL input-token usage fold projected from the PREVIOUS
            # round-trip's ``LLMRequestFinished`` (``0`` on the first turn → the
            # Policy falls back to a pure estimate). The three identifiers are
            # loop-invariant; ``last_input_tokens`` and ``steps_in_turn`` move.
            #
            # ``apply_event`` hands the LLM client the applier for the task we
            # are stepping. Its emits land straight in the EventLog, so without
            # this the in-memory task never folds them and ``last_input_tokens``
            # above stays frozen for the WHOLE turn no matter how many
            # round-trips the tool loop makes — the read is rebuilt per
            # iteration, but the field behind it never moved. The Engine stays
            # the sole physical writer of RuntimeState: it owns the task and
            # supplies the applier; the client only notifies.
            # ``cancelled`` rides along so the LLM client can abandon a
            # blocking provider wait (and its retry backoff) the moment a
            # human stop lands, instead of holding the turn until the round
            # returns. ``None`` (resume / replay) disarms every downstream
            # abort site — recordings stay byte-identical on those paths.
            ctx = StepContext(
                task_id=task.task_id, lease_id=lease_id, trace_id=trace_id,
                last_input_tokens=task.runtime.last_input_tokens,
                apply_event=lambda env: apply_event(
                    task, env, self._content_store
                ),
                cancelled=cancelled,
                steps_in_turn=steps_in_turn)
            view = self._composer.compose(task)
            _emit_context_plan(
                self._emit, self._content_store, task, view, lease_id, trace_id
            )
            decision: Decision = self._policy.decide(ctx, view)
            steps_in_turn += 1
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
                # A loop-continuing compaction step. The handler owns its emits
                # (tag → CompactionRequested → Compacted) and the anti-spiral
                # escalation; a returned Task is the terminal escalation, None
                # loops back to recompose the compacted view.
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
                # A background sub-agent: a loop-CONTINUING spawn (like
                # tool_calls, not an exit) — the handler emits Started + creates
                # the child + appends a "started" tool_result + hands it to the
                # executor driver, then returns None so the parent's SAME turn
                # keeps deciding (no barrier suspend). A guard deny/approval
                # returns a terminal/suspended Task (exit).
                outcome = handle_spawn_background_subtask(
                    self._ctx, task, decision,
                    lease_id=lease_id, trace_id=trace_id,
                )
                if outcome is not None:
                    return outcome
                continue

            if isinstance(decision, ToolCallsDecision):
                # tool_calls is the only loop-continuing handler, special-cased
                # here so dispatch_exit's `-> Task` return type stays honest.
                # ``cancelled`` rides along so a stop landing during call N of
                # a batch doesn't sit through calls N+1… (the handler closes
                # the remaining calls and raises).
                suspended = handle_tool_calls(
                    self._ctx, task, decision,
                    lease_id=lease_id, trace_id=trace_id,
                    cancelled=cancelled,
                )
                if suspended is not None:
                    return suspended
                # Mid-loop snapshot: a Policy that keeps returning tool_calls
                # without ever yielding must still produce a usable resume point,
                # so write one every N consecutive iterations and keep running.
                consecutive_tool_calls += 1
                if consecutive_tool_calls >= CONSECUTIVE_TOOL_CALLS_SNAPSHOT_THRESHOLD:
                    self._write_snapshot(
                        task, lease_id=lease_id, trace_id=trace_id
                    )
                    consecutive_tool_calls = 0
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

        The Decision is the typed channel through which a Policy hands a
        side-effect hint to the Engine; the Engine is the only writer of
        ``RuntimeState.messages``. ReAct-style Policies attach the full
        LLM-produced assistant turn here so the next compose sees the new
        history.
        """
        msg = getattr(decision, "assistant_message", None)
        if msg is None:
            return
        # sole-writer guard: a Policy cannot smuggle origin through the Decision
        # channel — only the Engine ledger seam (``append_user_message``) writes
        # it.
        msg = strip_message_origin(msg)
        self._append_message(task, msg, lease_id=lease_id, trace_id=trace_id)
        # Persist any out-of-band extended-thinking the Policy carried on the
        # Decision (module-level helper keeps the Engine lean).
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
        # Delegate to the typed dispatch in noeta.core._decision_handlers, which
        # raises ``NotImplementedError`` ("Unknown decision type: <name>") for
        # any unmapped Decision class.
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
        event_log: Optional[Any] = None,
    ) -> VerdictResult:
        """Refold the EventLog right before each guard check so Guards see
        counters from emit-sites outside this Engine (ToolRuntime,
        RuntimeLLMClient). ``copy.deepcopy`` isolates the ``GovernanceState``
        snapshot — a canonical round-trip would return a plain dict and break
        the typed Guard contract.

        ``spawned_subtasks_override`` simulates **only** the ``spawned_subtasks``
        counter for batch fan-out admission (the i-th spec sees ``current + i``);
        ``subtask_depth`` / ``active_skills`` / everything else still come from
        the fresh fold, so non-budget guards are unaffected.

        ``event_log`` substitutes the reader the fresh fold (and the repetition
        window) is taken from — the crash-recovery classifier passes a
        ``BoundedEventLog`` capped at the pre-attempt baseline so Guards judge
        the state a re-drive would actually run on, not the interrupted
        attempt's dirty in-window counters. ``None`` (every live-execution call)
        keeps the Engine's own log.
        """
        log = event_log if event_log is not None else self._event_log
        fresh = fold(log, self._content_store, task.task_id)
        governance = copy.deepcopy(fresh.governance)
        if spawned_subtasks_override is not None:
            governance.spawned_subtasks = spawned_subtasks_override
        recent = _recent_tool_calls(
            log.read(task.task_id),
            self._content_store,
            window=_RECENT_TOOL_CALLS_WINDOW,
        )
        ctx = GuardContext(
            task_id=task.task_id,
            governance=governance,
            # fold-derived active skills so skill `allowed-tools` enforcement
            # sees the identical set live / resume.
            active_skills=tuple(fresh.state.active_skills),
            # fold-derived delegation depth so the BudgetGuard depth cap sees
            # the identical value live / resume.
            subtask_depth=fresh.subtask_depth,
            # recorded tool-call history (neutral identity keys) so
            # RepetitionGuard detects a stuck loop resume-deterministically.
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
        # Refold so the snapshot body captures emit-site governance accumulation
        # (ToolRuntime, RuntimeLLMClient).
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
    keys ``(tool_name, canonical input bytes)`` for ``RepetitionGuard``.

    Pure projection of the recorded ``ToolCallStarted`` suffix — no clock /
    random — so live and resume see the identical history. Arguments
    offloaded to the ContentStore are dereferenced through the shared
    ``resolve_tool_call_arguments`` helper, then canonicalised so the key is
    key-order independent and provider-neutral.

    Called once per proposed tool call, with the *entire* task event stream
    (``_guard`` reads with no ``after_seq``): scans ``events`` in reverse and
    stops as soon as ``window`` ``ToolCallStarted`` entries are collected, so
    ``resolve_tool_call_arguments`` (a ContentStore round-trip for offloaded
    args) only ever runs for the last ``window`` calls, not every historical
    one. The result is re-reversed back to chronological (append) order —
    identical output to a forward scan kept to ``[-window:]``.
    """
    if window <= 0:
        return ()
    keys: list[tuple[str, bytes]] = []
    for env in reversed(events):
        if env.type != "ToolCallStarted":
            continue
        payload = env.payload
        args = resolve_tool_call_arguments(payload, content_store)
        keys.append((payload.tool_name, to_canonical_bytes(args)))
        if len(keys) >= window:
            break
    keys.reverse()
    return tuple(keys)


def _emit_context_plan(
    emit: Callable[..., EventEnvelope],
    content_store: ContentStore,
    task: Task,
    view: Any,
    lease_id: str,
    trace_id: str,
) -> None:
    """Emit ContextPlanComposed in front of every LLM round-trip, then converge
    live state through fold so a mid-step snapshot captures the freshly-set
    plan_ref.

    Emitted **unconditionally** — even when the composer produced no stored plan
    (``view.plan_ref is None``, the PassthroughComposer fallback). This event is
    the per-step boundary fold counts ``governance.iterations`` from; skipping
    it for plan-less views would make ``BudgetGuard.max_iterations`` inert under
    Passthrough.
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
    # Neutral answer-audit codec inlined here (the kernel does not import the
    # product ``user_questions`` module): a single ``{"answers": ...}`` JSON
    # object.
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


def _withdraw_user_question(engine: Engine, task: Task, *, question_id: str, withdrawn_by: Optional[str], reason: Optional[str], lease_id: str, trace_id: Optional[str]) -> Task:
    # Mirrors ``_answer_user_question`` but records no answer: emit the neutral
    # withdrawal audit (fold pops ``pending_questions``) and close the dangling
    # ``ask_user_question`` tool_use with a ``success=False`` result so a later
    # turn's transcript stays well-formed. Unlike the answer path, no answer body
    # is stored and the model turn is NOT driven (the caller parks the task idle).
    pending = task.governance.pending_questions.get(question_id)
    if pending is None:
        raise UserQuestionNotPending(
            f"no pending user question for question_id {question_id!r}"
        )
    call_id = str(pending["call_id"])
    resolved_trace = trace_id or engine._latest_trace_id(task.task_id)
    env = engine._emit(
        task_id=task.task_id,
        type_="UserQuestionWithdrawn",
        payload=UserQuestionWithdrawnPayload(
            question_id=question_id,
            call_id=call_id,
            withdrawn_by=withdrawn_by,
            reason=reason,
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
                output={"question_id": question_id, "withdrawn": True},
                success=False,
                error="Question withdrawn by user (no answer provided).",
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
    engine: Engine,
    task: Task,
    *,
    handle: str,
    lease_id: str,
    suspend_reason: Optional[str] = None,
) -> Task:
    """Cooperative-stop landing: suspend ``task`` on a human ``handle``.

    ``suspend_reason`` rides onto the recorded ``TaskSuspended.reason`` (and
    through it the dispatcher's stored ``suspend_reason``), so a rest reached by
    a human stop is distinguishable from an ordinary ``waiting_human`` one
    without scanning the stream for the control event that caused it. ``None``
    keeps the default.

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
        YieldForHumanDecision(prompt=handle, suspend_reason=suspend_reason),
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
    tool_result_transforms: tuple[Any, ...] = (),
) -> Any:
    """Convenience ToolRuntime for callers that pass ``tools`` but no explicit
    ``tool_runtime`` (mostly tests). Forward the host's background runner and
    per-turn file-checkpoint gate so ``shell_run`` bg jobs and AI-edit rewind
    baselines reach the runtime, plus the agent's ``tool_result_transform``
    stages. Local import breaks the runtime→core import cycle.
    """
    from noeta.runtime.tool import ToolRuntime

    return ToolRuntime(
        event_log=event_log,
        content_store=content_store,
        background_runner=background_runner,
        file_checkpoint_registry=file_checkpoint_registry,
        tool_result_transforms=tool_result_transforms,
    )


def _default_id_factory() -> str:
    """Default subtask_id source for production callers.

    The production random factory; a test can inject a deterministic replacement
    via ``id_factory``.
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
    """Per-task per-(kind, name) first-emission content provenance.

    Emits one ``ContextContentRecorded`` right *before* whatever durable
    activation follows (e.g. ``TaskStatePatched(activate_skills=…)`` for the
    skill kind), so the causal order is unambiguous. Duplicate calls for the
    same (task, kind, name) drop against fold's authoritative generic activation
    map ``TaskState.active_content``. All five payload strings are
    caller-computed (the SDK registry owns kind semantics, hashes, and drift
    policy), so the kernel stays compare-only-strings and never imports
    noeta-sdk. Module-level (off the Engine class) to keep the core line budget
    honest.
    """
    if not kind or not name or not content_hash:
        return task
    # Hash last-write-wins: no-op only when this exact hash is already active
    # for ``(kind, name)``; a new hash records a refresh.
    if task.state.active_content.get(kind, {}).get(name) == content_hash:
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


def abandon_step_attempt(
    engine: Engine,
    task_id: str,
    *,
    baseline: Task,
    abandoned_from_seq: int,
    reason: str,
    lease_id: str,
) -> EventEnvelope:
    """Seal an interrupted decide→act attempt (crash recovery).

    Serialises ``baseline`` (the state as it stood just before the
    interrupted attempt's ``ContextPlanComposed``) into the ContentStore —
    the same 4-slice body ``TaskSnapshot`` / ``TaskRewound`` point at — and
    appends the snapshot-shaped ``StepAttemptAbandoned`` marker, making the
    partial attempt folded-over dead history. Written **under the recovery
    lease** (unlike the control-plane ``TaskRewound``) so a zombie step
    thread from the crashed process can never race the seal.

    A MODULE-LEVEL free function (like :func:`suspend_on_human_handle`): it
    reaches into ``engine`` internals yet must stay OUT of the ``class
    Engine`` body so it does not count against the line budget.
    """
    state_ref = engine._content_store.put(
        serialize_task_state(baseline), media_type=snapshot_media_type()
    )
    return engine._emit(
        task_id=task_id,
        type_="StepAttemptAbandoned",
        payload=StepAttemptAbandonedPayload(
            abandoned_from_seq=abandoned_from_seq,
            state_ref=state_ref,
            reason=reason,
        ),
        lease_id=lease_id,
        trace_id=engine._latest_trace_id(task_id),
    )


def guard_allows_tool_call(
    engine: Engine, task: Task, call: ToolCall, *, event_log: Any = None
) -> bool:
    """Would ``call`` run with no human approval gate right now?

    The crash-recovery classifier's single question: an interrupted attempt
    may be re-driven automatically only when every tool call recorded in it
    would execute unattended (the same surface ``handle_tool_calls`` gates
    live execution on — permission mode, risk ceiling, ``can_use_tool``,
    skill grants). Any non-ALLOW verdict — approval *or* deny — classifies
    the attempt as needing a human, conservatively: a deny would not re-run
    the call, but its original side effects already happened once and only
    an operator can judge them. A guard crash also returns ``False``
    (fail-closed), matching ``PermissionGuard``'s own conventions.

    ``event_log`` (a ``BoundedEventLog`` capped at the pre-attempt
    baseline) makes the verdict about the state the re-drive would run on:
    counting the dirty interrupted window into Budget / Repetition guards
    would park attempts the re-drive itself would allow.
    """
    try:
        return engine._guard(
            ProposedToolCall(call=call), task, event_log=event_log
        ).is_allow
    except Exception:  # noqa: BLE001 — classification must fail closed
        return False
