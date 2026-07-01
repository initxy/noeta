"""Per-Decision handler functions extracted from Engine.

Each Decision variant's branch logic — previously `Engine._tool_calls /
_spawn_subtask / _finish / _fail / _wait_timer / _yield_for_human` —
moves here as module-level functions taking a typed
:class:`HandlerContext`. ``Engine.run_one_step`` keeps the compose →
decide loop, the ``ToolCallsDecision`` special case, the
state_patch / assistant_message helpers, and the controlled
callables (``_emit`` / ``_guard`` / ``_write_snapshot`` /
``_resolve_tool`` / ``_create_child_task``). The result: Engine
class body shrinks well under its 500-line budget, future
Decision variants get headroom, and per-Decision logic is now
unit-testable with a stub ``HandlerContext``.

Design contract:

* No raw ``EventLog`` exposure — handlers reach the log only through
  ``ctx.emit`` (business write, lease-checked) and
  ``ctx.create_child_task`` (the one narrow cross-stream system write
  needed for subtask genesis).
* No HookManager exposure — handlers call ``ctx.guard``.
* No raw Engine reference — handlers take ``HandlerContext``, not
  ``Engine``. AST regression test in CI enforces this.
* No ``# type: ignore`` — every callable in ``HandlerContext`` is a
  typed Protocol with the exact signature its Engine-bound
  implementation uses.
* WaitExternalDecision is deliberately absent from ``dispatch_exit``
  — Engine's pre-refactor ``NotImplementedError("Unknown decision
  type: <name>")`` behaviour is byte-equal preserved.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from typing import Any, Callable, Container, Optional, Protocol

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.content_store import ContentStore
from noeta.protocols.values import ContentRef, EVENT_PAYLOAD_MAX_BYTES
from noeta.protocols.decisions import (
    CompactionRequestedDecision,
    Decision,
    FailDecision,
    FinishDecision,
    SpawnSubtaskDecision,
    SpawnSubtaskSpec,
    SpawnSubtasksDecision,
    StatePatchDecision,
    TaskStatePatch,
    ToolCall,
    ToolCallsDecision,
    WaitTimerDecision,
    YieldForHumanDecision,
)
from noeta.protocols.events import (
    AssistantThinkingRecordedPayload,
    BackgroundSubagentStartedPayload,
    CompactedPayload,
    CompactionRequestedPayload,
    ContextContentRecordedPayload,
    EventEnvelope,
    MessagesAppendedPayload,
    SkillContentRecordedPayload,
    StepTransitionMarkedPayload,
    SubtaskDeniedPayload,
    SubtaskSpawnedPayload,
    TaskCompletedPayload,
    TaskFailedPayload,
    TaskStatePatchedPayload,
    TaskSuspendedPayload,
    ToolCallDeniedPayload,
    UserQuestionRequestedPayload,
)
from noeta.protocols.hooks import (
    ProposedAction,
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    Verdict,
    VerdictResult,
)
from noeta.protocols.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.step_transition import TransitionReason
from noeta.protocols.task import Task
from noeta.protocols.tool import Tool, ToolResult
from noeta.protocols.tool_args import (
    build_tool_call_approval_requested_payload,
)
from noeta.protocols.wake import (
    HumanResponseReceived,
    SubtaskCompleted,
    SubtaskGroupCompleted,
    TimerFired,
    derive_group_id,
)


__all__ = [
    "ApplyEventFn",
    "BackgroundSubagentCapacityFn",
    "ContentHashesFn",
    "CreateChildTaskFn",
    "EmitFn",
    "GuardFn",
    "HandlerContext",
    "LaunchBackgroundSubagentFn",
    "MAX_FANOUT",
    "ResolveToolFn",
    "SkillHashesFn",
    "ToolInvoker",
    "WriteSnapshotFn",
    "append_tool_denial_feedback",
    "dispatch_exit",
    "emit_skill_provenance_for_patch",
    "handle_compaction_requested",
    "handle_fail",
    "handle_finish",
    "handle_spawn_background_subtask",
    "handle_spawn_subtask",
    "handle_spawn_subtasks",
    "handle_state_patch",
    "handle_tool_calls",
    "handle_wait_timer",
    "handle_yield_for_human",
    "invoke_approved_tool_call",
    "maybe_emit_provenance",
    "maybe_emit_skill_content_recorded",
    "put_messages",
    "record_assistant_thinking",
    "strip_message_origin",
    "wrap_tool_result_block",
]


_MESSAGES_MEDIA_TYPE = "application/json"


# ---------------------------------------------------------------------------
# Typed callable Protocols — every HandlerContext callable has a precise
# signature; no Callable[..., ...] placeholders, no Any return types.
# ---------------------------------------------------------------------------


class EmitFn(Protocol):
    """Engine-bound business emit (mirrors ``Engine._emit``)."""

    def __call__(
        self,
        *,
        task_id: str,
        type_: str,
        payload: Any,
        lease_id: str,
        trace_id: str,
    ) -> EventEnvelope: ...


class CreateChildTaskFn(Protocol):
    """Engine-bound cross-stream child-task creation.

    The only cross-stream system write any handler currently needs is
    the child's ``TaskCreated`` envelope. Wrapping that single write
    as a narrow callable keeps the cross-stream seam visible without
    re-exposing the general ``system_emit`` shape.
    """

    def __call__(
        self,
        *,
        child_task_id: str,
        parent_task_id: str,
        agent_name: str,
        goal: str,
        inputs: dict[str, Any],
        trace_id: str,
        subtask_depth: int,
        background: bool = False,
    ) -> EventEnvelope: ...


class LaunchBackgroundSubagentFn(Protocol):
    """Engine-injected hook that hands a freshly-created background sub-agent
    to the executor-driven background-subagent driver (Mechanism C).

    docs/adr/background-subagent.md. Called by
    :func:`handle_spawn_background_subtask` AFTER the child's ``TaskCreated``
    (``background=True``) and the parent's "started" tool_result are written —
    it submits the child subtree to the shared executor so it runs CONCURRENTLY
    with the parent's continuing turn, and arranges turn-boundary delivery of
    its result. ``None`` (oneshot / lifecycle / a child engine / resume) ⇒ the
    background branch is never taken — the spawn degrades to a foreground barrier
    spawn — so fold / resume never launch and nested background never recurses.
    """

    def __call__(self, *, parent_task_id: str, child_task_id: str) -> None: ...


class BackgroundSubagentCapacityFn(Protocol):
    """Pre-flight per-session concurrency check for a background spawn.

    docs/adr/background-subagent.md. Given the spawning (session-root) task id,
    returns a rejection reason string when the session is already at its
    background-sub-agent cap, or ``None`` when there is room. Checked BEFORE the
    handler writes any ``BackgroundSubagentStarted`` / child genesis, so an
    over-cap launch is invisible to the durable record (reject = no event, the
    same discipline as the background-shell job cap). ``None`` seam ⇒ no cap."""

    def __call__(self, parent_task_id: str) -> Optional[str]: ...


class WriteSnapshotFn(Protocol):
    """Engine-bound snapshot write (mirrors
    ``Engine._write_snapshot(task, *, lease_id, trace_id)``)."""

    def __call__(
        self, task: Task, *, lease_id: str, trace_id: str
    ) -> None: ...


class GuardFn(Protocol):
    """Engine-bound Guard runner (mirrors ``Engine._guard``).

    SR2 (B2): ``spawned_subtasks_override`` simulates the
    ``GovernanceState.spawned_subtasks`` counter for batch fan-out
    admission — the i-th spec in a `SpawnSubtasksDecision` is checked
    against ``current + i`` so a batch cannot overshoot
    ``max_spawned_subtasks``. ONLY that counter is overridden; everything
    else (``subtask_depth``, ``active_skills``, …) stays from the fresh
    fold, so PermissionGuard / skill guards are unaffected."""

    def __call__(
        self,
        action: ProposedAction,
        task: Task,
        *,
        spawned_subtasks_override: Optional[int] = None,
    ) -> VerdictResult: ...


class ApplyEventFn(Protocol):
    """Engine-bound fold-after-emit (wraps ``noeta.core.fold.apply_event``
    with the ContentStore Engine owns)."""

    def __call__(self, task: Task, env: EventEnvelope) -> None: ...


class ResolveToolFn(Protocol):
    """Engine-bound tool resolution (mirrors ``Engine._resolve_tool``)."""

    def __call__(self, call: ToolCall) -> Tool: ...


class ToolInvoker(Protocol):
    """``noeta.runtime.tool.ToolRuntime``'s ``invoke``-equivalent surface.

    ``ToolRuntime`` exposes
    ``invoke(tool, call, *, task_id, lease_id, trace_id) -> ToolResult``,
    writing the tool-side three-event trio (``ToolCallStarted`` /
    ``ToolResultRecorded`` / ``ToolCallFinished``) onto the EventLog
    as a side effect. Declaring a narrow Protocol keeps the handler
    module free of the L2 ``ToolRuntime`` import.
    """

    def invoke(
        self,
        tool: Tool,
        call: ToolCall,
        *,
        task_id: str,
        lease_id: str,
        trace_id: str,
    ) -> ToolResult: ...


class SkillHashesFn(Protocol):
    """Resolve a skill's declared version + current content hash (issue
    04).

    Consulted right *before* a ``TaskStatePatched(activate_skills=…)``
    that first activates the skill. Returns ``(version, content_hash)``
    for skills the host knows, ``None`` for unknown skills — ``None``
    skips the ``SkillContentRecorded`` emission. Both strings are opaque
    to the kernel (the hash is
    computed host-side, e.g. via ``noeta.execution.skills.skill_content_hash``),
    so noeta-runtime never imports noeta-sdk.
    """

    def __call__(self, skill_name: str) -> Optional[tuple[str, str]]: ...


class ContentHashesFn(Protocol):
    """Resolve a content item's declared version + current hash by
    ``(kind, name)``.

    The generic successor of :class:`SkillHashesFn`: one resolver for
    every content-channel kind — the skill-specific seam is subsumed as
    its ``kind="skill"`` resident. Returns ``(version, content_hash)``
    for items the host knows, ``None`` for unknown ones — ``None`` skips
    the provenance emission. Both strings stay opaque to the kernel, so
    noeta-runtime never imports noeta-sdk.
    """

    def __call__(self, kind: str, name: str) -> Optional[tuple[str, str]]: ...


# ---------------------------------------------------------------------------
# HandlerContext — the controlled seam Engine hands to each handler
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HandlerContext:
    """Engine-injected handle that per-Decision handler functions use.

    Construction lives in ``Engine.__init__``; handlers receive this
    by value and may not reach back to a live Engine instance. Every
    write goes through one of the typed callable seams below;
    handlers cannot accidentally bypass the single-writer
    invariant or the actor / origin marker convention.
    """

    # ─ Controlled writes ─────────────────────────────────────────────
    emit: EmitFn
    create_child_task: CreateChildTaskFn
    apply_event: ApplyEventFn

    # ─ Guard surface ─────────────────────────────────────────────────
    guard: GuardFn

    # ─ Snapshot surface ─────────────────────────────────────────────
    write_snapshot: WriteSnapshotFn

    # ─ Tool resolution + invocation ─────────────────────────────────
    resolve_tool: ResolveToolFn
    tool_invoker: Optional[ToolInvoker]

    # ─ Runtime helpers ──────────────────────────────────────────────
    content_store: ContentStore

    # ─ Identity & determinism ──────────────────────────────────────
    id_factory: Callable[[], str]
    clock: Callable[[], float]
    actor: str

    # ─ LEGACY per-skill content-hash seam ──
    #: After the generic content-channel generation switch this seam exists for OLD-recording
    #: resume only: when wired the mid-loop skill path re-emits the old
    #: ``SkillContentRecorded`` byte-equal from the recording's own hashes.
    #: Live hosts wire ``content_hashes`` instead.
    #: ``None`` (with no generic seam either) ⇒ no provenance events.
    skill_hashes: Optional[SkillHashesFn] = None

    # ─ generic (kind, name) content-hash resolver seam ─
    #: What live hosts wire (the registry's ``content_hashes()``); the
    #: skill sugar path emits the generic ``ContextContentRecorded``
    #: (kind="skill", policy="pinned") through it. Keep this field LAST —
    #: every field without a default above is positional public surface.
    content_hashes: Optional[ContentHashesFn] = None
    #: inline char cap for ``ToolResultBlock.output``.
    #: ``None`` ⇒ never truncate (zero behaviour change for existing hosts).
    #: When a tool's inline output is longer than this limit we keep the
    #: first N chars plus a deterministic truncated-marker suffix that
    #: references the full bytes via ``ToolResultRecorded.output_ref``.
    #: The full audit body always lives in the ContentStore; truncation
    #: applies only to the messages-stream shape. For byte-equivalent
    #: resume the recording's host must wire the same value.
    tool_output_inline_limit: Optional[int] = None
    #: background sub-agent launch hook (docs/adr/background-subagent.md).
    #: Wired only on a top-level interactive Engine; ``None`` everywhere else
    #: (oneshot / child engines / resume) so a ``spawn_subagent(background=True)``
    #: degrades to the ordinary foreground barrier spawn. Keep these two LAST.
    launch_background_subagent: Optional[LaunchBackgroundSubagentFn] = None
    #: background sub-agent per-session cap pre-check (paired with the launch
    #: hook; wired iff that is). ``None`` ⇒ no cap.
    background_subagent_capacity: Optional[BackgroundSubagentCapacityFn] = None


# ---------------------------------------------------------------------------
# Module-level helpers (used by handlers and by Engine.append_user_message)
# ---------------------------------------------------------------------------


def put_messages(
    content_store: ContentStore, messages: list[Message]
) -> MessagesAppendedPayload:
    """Serialize messages into ContentStore and build a ref-based
    ``MessagesAppendedPayload`` so the envelope stays under the
    4 KB ceiling regardless of message body size."""
    body = to_canonical_bytes(messages)
    ref = content_store.put(body, media_type=_MESSAGES_MEDIA_TYPE)
    return MessagesAppendedPayload(messages_ref=ref, count=len(messages))


#: Answer bytes above this go to the ContentStore so neither the worker's
#: ``TaskCompleted`` nor the parent's ``SubtaskCompleted`` (which re-carries the
#: result) can blow the ``EVENT_PAYLOAD_MAX_BYTES`` cap; the ~1 KB headroom
#: covers the surrounding payload structure.
_ANSWER_INLINE_LIMIT = EVENT_PAYLOAD_MAX_BYTES - 1024


def _spill_answer(
    content_store: ContentStore, answer: Any
) -> tuple[Any, Optional[ContentRef]]:
    """Keep a small answer inline; spill a large one to the ContentStore and
    return ``(None, ref)`` so the terminal ``TaskCompleted`` event stays under
    the payload cap. Read it back with
    :func:`noeta.protocols.events.answer_from_payload`."""
    body = to_canonical_bytes(answer)
    if len(body) <= _ANSWER_INLINE_LIMIT:
        return answer, None
    return None, content_store.put(body, media_type=_MESSAGES_MEDIA_TYPE)


def strip_message_origin(message: Message) -> Message:
    """Single-writer guard: strip ``origin`` off a Policy-submitted message.

    The only writer of ``Message.origin`` is the engine ledger seam
    (``Engine.append_user_message``). Messages handed in via the Decision
    channel (``assistant_message`` / a state-patch's ``messages_before`` /
    ``messages_after``) are stripped to ``None`` before they land, so a fake
    ``<system-reminder>`` tag in model/tool output text is always just text,
    and the origin value in the ledger is always attributable to the engine
    seam (D3 audit depends on this). The content is untouched — only the tag
    field is stripped.
    """
    if message.origin is None:
        return message
    return dc_replace(message, origin=None)


def wrap_tool_result_block(
    call: ToolCall,
    result: ToolResult,
    *,
    tool_output_inline_limit: Optional[int] = None,
) -> ToolResultBlock:
    """Render a tool result as a typed :class:`ToolResultBlock`.

    ``call_id`` pairs back to the original
    :class:`noeta.protocols.decisions.ToolCall.call_id` (also the
    ``ToolUseBlock.call_id`` the LLM emitted). The block keeps the
    inline ``output`` value when the Tool kept it small (4-KB envelope)
    and surfaces ``success`` / ``error`` so the next compose
    can show the model what happened.

    **Truncation:** when
    ``tool_output_inline_limit`` is a positive int, the output is
    stringified (via :func:`_coerce_inline_output`) and capped to the
    first ``limit`` characters, with a deterministic ``…[tool output
    truncated: …]`` suffix. The full body is NOT named in that suffix — it is
    recorded independently as ``ToolResultRecorded.output_ref`` (populated by
    :class:`ToolRuntime` after offload), which is where audit derefs it; the
    model has no ref-deref tool, so a hash in the prompt would be dead weight.
    When the limit is
    ``None`` (the default, host-unspecified) the raw ``result.output``
    is passed through **unchanged** so dict / list / ContentRef shapes
    the tools return stay typed and the 2600-test baseline keeps
    byte-identical behaviour.
    """
    error: Optional[str] = None
    if not result.success:
        error = result.summary or "tool failed"
    raw_output = result.output if result.output is not None else ""

    if tool_output_inline_limit is None:
        # Default path: zero behaviour change. Keep whatever type the
        # tool returned (str / dict / list / ContentRef / bytes).
        inline = raw_output
    else:
        inline = _coerce_inline_output(raw_output)
        inline = truncate_tool_output(inline, tool_output_inline_limit)

    # Image content the tool surfaced (e.g. ``read`` on a .png) rides into the
    # canonical message stream as ``ToolResultBlock.images``; the adapter
    # deref→inlines it at wire time (vision model) or degrades to text (non-
    # vision). Empty ⇒ ``None`` so the canonical form is byte-identical to a
    # pre-image recording (``__canonical_omit_none__``).
    images = (
        [ImageBlock(source=ref) for ref in result.images]
        if result.images
        else None
    )

    return ToolResultBlock(
        call_id=call.call_id,
        output=inline,
        success=result.success,
        error=error,
        images=images,
    )


# ---------------------------------------------------------------------------
# Truncation helper (pure-function contract so it's unit-testable).
# ---------------------------------------------------------------------------


def _coerce_inline_output(raw: Any) -> str:
    """Coerce an arbitrary ToolResult.output to a str for inline display.

    Strings pass through as-is; bytes decode UTF-8 with surrogateescape so
    truncation never crashes on binary output. Any other type uses its
    ``repr`` (which is what the composer / provider adapter already does
    when rendering dict-shaped tool outputs).
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", errors="surrogateescape")
    return repr(raw)


def truncate_tool_output(
    output: str,
    limit: Optional[int],
) -> str:
    """Return ``output`` unchanged unless ``limit`` is positive and the
    output is strictly longer than ``limit`` characters.

    The truncation suffix is a stable pure-ASCII marker with
    machine-scannable fields:
    * ``dropped`` — how many characters were elided
    * ``total`` — the original output length in characters (Python str)

    The marker starts with ``\n…[tool output truncated:`` so callers
    and auditors can recognise the shape without parsing the payload. It does
    NOT name the full body's ref: that is recorded separately as
    ``ToolResultRecorded.output_ref`` (the model has no ref-deref tool, so a
    hash in the prompt is dead weight it could only misread).
    """
    if limit is None:
        return output
    if limit <= 0:
        raise ValueError(
            f"tool_output_inline_limit must be > 0 when non-None; got {limit}"
        )
    total = len(output)
    if total <= limit:
        return output
    dropped = total - limit
    suffix = (
        "\n...[tool output truncated: "
        f"{dropped} of {total} chars dropped]"
    )
    # If the suffix itself is longer than the drop we *still* append it
    # verbatim: the marker is contract and must be present for audit
    # tooling to recognise. The caller is responsible for setting a
    # limit that leaves room (typically 200+ chars).
    return output[:limit] + suffix


def _validate_tool_output_inline_limit(
    limit: Optional[int],
) -> None:
    """Raise ``ValueError`` if ``limit`` is non-``None`` and not positive.

    Centralised so Engine, SdkHost, and CodeSessionConfig all share the
    same check + message.
    """
    if limit is not None and limit <= 0:
        raise ValueError(
            f"tool_output_inline_limit must be > 0 when non-None; got {limit}"
        )


def emit_step_transition(
    ctx: HandlerContext,
    task: Task,
    *,
    reason: TransitionReason,
    lease_id: str,
    trace_id: str,
    attempt: int = 0,
) -> Task:
    """Foundation B (D-B6): emit one ``StepTransitionMarked`` and fold it.

    Module-level so the Engine body stays under its ≤500-line budget — the
    Engine only adds a single call line. Only **non-default** continuations
    should call this (``approval_resume`` today; ②/③ add the retry/overflow/
    compaction reasons); the implicit ``next_turn`` default must never be
    emitted (D-B2). Uses only the typed ``ctx`` callables (``ctx.emit`` +
    ``ctx.apply_event``) so the handler-module AST guard (no Engine /
    EventLog / ToolRuntime imports) keeps holding.
    """
    env = ctx.emit(
        task_id=task.task_id,
        type_="StepTransitionMarked",
        payload=StepTransitionMarkedPayload(reason=reason, attempt=attempt),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    ctx.apply_event(task, env)
    return task


def record_assistant_thinking(
    ctx: HandlerContext,
    task: Task,
    decision: Decision,
    msg: Message,
    *,
    lease_id: str,
    trace_id: str,
) -> None:
    """Extended-thinking end-to-end (Slice B): persist an assistant turn's
    out-of-band thinking into ``ContextState.thinking_by_call_id``.

    Module-level so the Engine body stays under its ≤500-line budget — the
    Engine only adds a single call line. The Policy carries the LLM's
    ThinkingBlocks on the Decision, NOT in ``msg`` (the persisted history
    stays thinking-free so its non-deterministic signature never perturbs
    the stable prompt prefix). We key them by the turn's FIRST ``tool_use``
    ``call_id`` (the stable per-turn identity the Composer re-attaches
    against), stash the blocks in the ContentStore (under the 4 KB cap), then
    emit one ``AssistantThinkingRecorded`` and fold it — so the live task and
    a from-scratch resume write the slice through the SAME handler (single
    writer), exactly like ``ContextPlanComposed``. Decisions without
    thinking (non-reasoning models, ``Finish`` / ``Fail``) no-op. Uses only
    the typed ``ctx`` callables so the handler-module AST guard holds.
    """
    thinking = getattr(decision, "assistant_thinking", ())
    if not thinking:
        return
    call_id = next(
        (b.call_id for b in msg.content if isinstance(b, ToolUseBlock)), None
    )
    if call_id is None:
        # No tool_use to anchor the turn (e.g. a thinking-only end_turn); an
        # Anthropic continuation only needs the thinking preceding a tool_use.
        return
    thinking_ref = ctx.content_store.put(
        to_canonical_bytes(list(thinking)), media_type=_MESSAGES_MEDIA_TYPE
    )
    env = ctx.emit(
        task_id=task.task_id,
        type_="AssistantThinkingRecorded",
        payload=AssistantThinkingRecordedPayload(
            call_id=call_id,
            thinking_ref=thinking_ref,
            block_count=len(thinking),
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    ctx.apply_event(task, env)


def invoke_approved_tool_call(
    ctx: HandlerContext,
    task: Task,
    call: ToolCall,
    *,
    lease_id: str,
    trace_id: str,
) -> Task:
    """Run an already-approved tool call, **bypassing the guard**
    (Phase 4.5 Issue A).

    Mirrors the per-call body + tail of :func:`handle_tool_calls` for a
    single call, minus the guard check — the human already approved, so
    re-running the guard (which returned ``require_approval``) would just
    re-suspend. Emits the normal ``ToolCallStarted → ToolResultRecorded
    → ToolCallFinished`` (via the ToolRuntime) plus one ``MessagesAppended``
    carrying the ``role="tool"`` result, so the byte shape matches a
    normally-allowed single call.
    """
    if ctx.tool_invoker is None:
        raise RuntimeError("Engine got approved tool_call but no ToolRuntime.")
    tool = ctx.resolve_tool(call)
    result = ctx.tool_invoker.invoke(
        tool, call, task_id=task.task_id, lease_id=lease_id, trace_id=trace_id
    )
    batched = Message(
        role="tool",
        content=[
            wrap_tool_result_block(
                call,
                result,
                tool_output_inline_limit=ctx.tool_output_inline_limit,
            )
        ],
    )
    task.runtime.messages.append(batched)
    ctx.emit(
        task_id=task.task_id,
        type_="MessagesAppended",
        payload=put_messages(ctx.content_store, [batched]),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    return task


def maybe_emit_provenance(
    task: Task,
    *,
    name: str,
    resolver: Optional[Callable[[str], Optional[tuple[str, str]]]],
    recorded: Container[str],
    emit: EmitFn,
    apply_event: ApplyEventFn,
    type_: str,
    make_payload: Callable[[str, str, str], Any],
    lease_id: str,
    trace_id: str,
) -> None:
    """Emit first-only provenance: silently skip when the resolver is absent,
    the name is already recorded, or resolution fails.

    ``recorded`` is the fold-owned container the emitted event type writes into
    (legacy: the governance hash dict; generic: the activation-map name
    tuple) — gating against fold keeps re-entrant paths single-emission.
    """
    if resolver is None:
        return
    if name in recorded:
        return
    resolved = resolver(name)
    if resolved is None:
        return
    version, content_hash = resolved
    env = emit(
        task_id=task.task_id,
        type_=type_,
        payload=make_payload(name, version, content_hash),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    apply_event(task, env)


#: The activate_skills patch sugar is the skill kind's
#: dedicated write bridge (retained by design), so its generic emissions
#: carry the skill kind's drift policy: the policy flags a SKILL.md edit
#: without a version bump (a drift-comparison consumer would treat it as the
#: human-error case; that consumer has since been removed).
_SKILL_SUGAR_KIND = "skill"
_SKILL_SUGAR_POLICY = "pinned"


def maybe_emit_skill_content_recorded(
    ctx: HandlerContext,
    task: Task,
    skill_name: str,
    *,
    lease_id: str,
    trace_id: str,
) -> None:
    """Emit first-only content provenance for one ``activate_skills`` name.

    Generation switch: which event TYPE gets
    emitted is keyed on which resolver seam the host wired —

    * ``ctx.skill_hashes`` (the retained LEGACY seam) → the old
      ``SkillContentRecorded``, byte-identical to pre-cutover hosts. Its
      only remaining writer is the resume of a pre-cutover
      recording (``_historical_skill_hashes``), which must re-emit the
      recorded type for the captured stream to stay byte-equal. Gate:
      fold's authoritative ``governance.skill_content_hashes`` (what the
      old event folds into), preserving old-recording emission points
      exactly.
    * ``ctx.content_hashes`` (the generic seam, what live hosts wire) →
      the generic ``ContextContentRecorded`` with kind="skill",
      policy="pinned" — new recordings carry only the generic shape.
      Gate: the generic activation map ``TaskState.active_content``
      (what the generic event folds into).
    * Neither wired (kernel tests / stub demos / old hosts) → no event,
      old byte shapes preserved. Resolver returning ``None`` (unknown
      skill) → no event.

    Placed right *before* the ``TaskStatePatched(activate_skills=…)`` that
    first activates the skill, matching the pre-loop helper's causal order
    (:func:`noeta.core.engine.emit_context_content_recorded` fires before
    ``Engine.apply_state_patch``, lands the name in ``active_content``, so
    this seam sees it present and skips — one event per (task, skill)).
    """
    if ctx.skill_hashes is not None:
        maybe_emit_provenance(
            task,
            name=skill_name,
            resolver=ctx.skill_hashes,
            recorded=task.governance.skill_content_hashes,
            emit=ctx.emit,
            apply_event=ctx.apply_event,
            type_="SkillContentRecorded",
            make_payload=lambda n, v, h: SkillContentRecordedPayload(
                skill_name=n, version=v, content_hash=h
            ),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        return
    generic = ctx.content_hashes
    if generic is None:
        return
    maybe_emit_provenance(
        task,
        name=skill_name,
        resolver=lambda n: generic(_SKILL_SUGAR_KIND, n),
        recorded=task.state.active_content.get(_SKILL_SUGAR_KIND, ()),
        emit=ctx.emit,
        apply_event=ctx.apply_event,
        type_="ContextContentRecorded",
        make_payload=lambda n, v, h: ContextContentRecordedPayload(
            kind=_SKILL_SUGAR_KIND,
            name=n,
            version=v,
            content_hash=h,
            policy=_SKILL_SUGAR_POLICY,
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )


def emit_skill_provenance_for_patch(
    ctx: HandlerContext,
    task: Task,
    patch: TaskStatePatch,
    *,
    lease_id: str,
    trace_id: str,
) -> None:
    """Emit a first-only SkillContentRecorded for each activate_skills name in the patch."""
    for name in patch.activate_skills or ():
        maybe_emit_skill_content_recorded(
            ctx, task, name, lease_id=lease_id, trace_id=trace_id
        )


def append_tool_denial_feedback(
    ctx: HandlerContext,
    task: Task,
    *,
    call_id: str,
    reason: str,
    lease_id: str,
    trace_id: str,
) -> Task:
    """Append a ``role="tool"`` denial-feedback message for a human-denied
    call (Phase 4.5 Issue A — conversation continuity, not a governance
    event).

    The resumed loop must not continue with a dangling assistant
    ``tool_call`` and no matching tool result. This emits one
    ``MessagesAppended`` carrying a ``ToolResultBlock`` with the same
    ``call_id``, ``success=False``, and the human denial ``reason`` as
    the error — so the next compose gives the model deterministic
    feedback that the call was refused. **No tool is invoked**; there is
    no ``ToolCallStarted/ToolResultRecorded/ToolCallFinished``.
    """
    block = ToolResultBlock(
        call_id=call_id, output="", success=False, error=reason
    )
    batched = Message(role="tool", content=[block])
    task.runtime.messages.append(batched)
    ctx.emit(
        task_id=task.task_id,
        type_="MessagesAppended",
        payload=put_messages(ctx.content_store, [batched]),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    return task


# ---------------------------------------------------------------------------
# Shared suspend / terminate helpers (moved from Engine; used by per-Decision
# handlers; not exposed as public API)
# ---------------------------------------------------------------------------


def _suspend(
    ctx: HandlerContext,
    task: Task,
    *,
    wake_on: Any,
    reason: str,
    lease_id: str,
    trace_id: str,
) -> Task:
    """Snapshot + ``TaskSuspended`` shared by every suspend branch."""
    task.status = "suspended"
    task.wake_on = wake_on
    ctx.write_snapshot(task, lease_id=lease_id, trace_id=trace_id)
    ctx.emit(
        task_id=task.task_id,
        type_="TaskSuspended",
        payload=TaskSuspendedPayload(reason=reason, wake_on=wake_on),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    return task


def _terminate(
    ctx: HandlerContext,
    task: Task,
    *,
    type_: str,
    payload: Any,
    lease_id: str,
    trace_id: str,
) -> Task:
    """Snapshot + terminal event. Observer handles parent handoff."""
    task.status = "terminal"
    ctx.write_snapshot(task, lease_id=lease_id, trace_id=trace_id)
    ctx.emit(
        task_id=task.task_id,
        type_=type_,
        payload=payload,
        lease_id=lease_id,
        trace_id=trace_id,
    )
    return task


# ---------------------------------------------------------------------------
# Per-Decision handlers
# ---------------------------------------------------------------------------


def handle_yield_for_human(
    ctx: HandlerContext,
    task: Task,
    decision: YieldForHumanDecision,
    *,
    lease_id: str,
    trace_id: str,
) -> Task:
    """Snapshot + TaskSuspended with ``HumanResponseReceived`` wake.

    ``require_approval`` from any Guard and a direct
    ``yield_for_human`` Decision share this exit path — no separate
    ApprovalRequested event type exists.

    When ``decision.request_anchor`` is present the Policy is posting a
    structured human-input request: the kernel first writes a NEUTRAL
    audit anchor (``UserQuestionRequested`` — generic "a structured human
    prompt was posted", carrying only an opaque ``questions_ref`` + counts
    + ids + a free-form ``reason``; the kernel never decodes the body or
    enforces any schema/caps), folds it into ``governance.pending_questions``,
    then suspends on the anchor's ``handle``. The SDK owns the schema /
    caps / validators / codec behind ``questions_ref``.
    """
    anchor = decision.request_anchor
    if anchor is not None:
        env = ctx.emit(
            task_id=task.task_id,
            type_="UserQuestionRequested",
            payload=UserQuestionRequestedPayload(
                question_id=anchor.request_id,
                call_id=anchor.request_id,
                questions_ref=anchor.questions_ref,
                question_count=anchor.question_count,
                reason=anchor.reason,
            ),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        ctx.apply_event(task, env)
        return _suspend(
            ctx,
            task,
            wake_on=HumanResponseReceived(handle=anchor.handle),
            reason="waiting_human",
            lease_id=lease_id,
            trace_id=trace_id,
        )
    # Byte-equal preservation: pre-refactor used ``uuid.uuid4().hex``
    # for the fallback handle (engine.py:397), not ctx.id_factory.
    handle = decision.prompt or f"yield-{uuid.uuid4().hex}"
    return _suspend(
        ctx,
        task,
        wake_on=HumanResponseReceived(handle=handle),
        reason="waiting_human",
        lease_id=lease_id,
        trace_id=trace_id,
    )


def _yield_for_approval(
    ctx: HandlerContext,
    task: Task,
    *,
    handle: str,
    lease_id: str,
    trace_id: str,
) -> Task:
    """Private helper used by tool_calls / finish / spawn when a Guard
    returns ``require_approval``. Routes through the same suspend
    machinery as ``handle_yield_for_human`` so the resulting wake_on
    and reason match the public path."""
    return _suspend(
        ctx,
        task,
        wake_on=HumanResponseReceived(handle=handle),
        reason="waiting_human",
        lease_id=lease_id,
        trace_id=trace_id,
    )


def _guard_and_route(
    ctx: HandlerContext,
    task: Task,
    action: ProposedAction,
    *,
    approval_handle: str,
    on_deny: Callable[[Optional[str]], Task],
    lease_id: str,
    trace_id: str,
) -> Optional[Task]:
    """Run a single proposed action past its Guard and route the verdict.

    The "ask the Guard, then branch on allow / deny / require_approval"
    plumbing is the same deep logic for every *single-action* exit
    decision (``handle_finish`` / ``handle_spawn_subtask``): only the two
    non-allow tails differ. This helper owns the routing so the callers
    keep just their pure decision body; the locality lives here.

    * **DENY** → call ``on_deny(verdict.reason)`` and return its terminal
      ``Task``. The raw ``verdict.reason`` (possibly ``None``) is passed
      through **unchanged** so each caller keeps its own byte-equal deny
      shape: ``handle_finish`` interpolates it raw
      (``f"finish denied: {reason}"``), ``handle_spawn_subtask`` applies
      its own ``reason or "denied"`` fallback before emitting
      ``SubtaskDenied`` + failing.
    * **REQUIRE_APPROVAL** → suspend through :func:`_yield_for_approval`
      with the caller's ``approval_handle`` (``approval-finish-…`` /
      ``approval-spawn-…``).
    * **ALLOW** → return ``None``; the caller proceeds with its pure body.

    The emitted event sequence / payloads are byte-identical to the
    inlined branches this replaces.
    """
    verdict = ctx.guard(action, task)
    if verdict.verdict is Verdict.DENY:
        return on_deny(verdict.reason)
    if verdict.verdict is Verdict.REQUIRE_APPROVAL:
        return _yield_for_approval(
            ctx,
            task,
            handle=approval_handle,
            lease_id=lease_id,
            trace_id=trace_id,
        )
    return None


def handle_wait_timer(
    ctx: HandlerContext,
    task: Task,
    decision: WaitTimerDecision,
    *,
    lease_id: str,
    trace_id: str,
) -> Task:
    """Snapshot + TaskSuspended with a ``TimerFired`` wake.

    ``fire_at`` follows the pre-refactor formula
    ``ctx.clock() + decision.seconds`` (engine.py:426) byte-equal.
    """
    return _suspend(
        ctx,
        task,
        wake_on=TimerFired(fire_at=ctx.clock() + decision.seconds),
        reason="waiting_timer",
        lease_id=lease_id,
        trace_id=trace_id,
    )


def handle_finish(
    ctx: HandlerContext,
    task: Task,
    decision: FinishDecision,
    *,
    lease_id: str,
    trace_id: str,
) -> Task:
    routed = _guard_and_route(
        ctx,
        task,
        ProposedFinish(answer=decision.answer),
        approval_handle=f"approval-finish-{task.task_id}",
        on_deny=lambda reason: handle_fail(
            ctx,
            task,
            FailDecision(reason=f"finish denied: {reason}"),
            lease_id=lease_id,
            trace_id=trace_id,
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    if routed is not None:
        return routed
    # Phase 1 fallback: when the Policy did not attach its own
    # ``assistant_message`` (Stub policies don't), the Engine
    # synthesises a minimal assistant Message from ``decision.answer``
    # so RuntimeState still surfaces the final answer in the
    # conversation log. A Policy that already attached an
    # assistant_message has had it appended at the top of
    # ``run_one_step`` via ``_apply_decision_assistant_message`` —
    # no duplicate emission here.
    if decision.assistant_message is None:
        msg = Message(
            role="assistant",
            content=[TextBlock(text=str(decision.answer))],
        )
        task.runtime.messages.append(msg)
        ctx.emit(
            task_id=task.task_id,
            type_="MessagesAppended",
            payload=put_messages(ctx.content_store, [msg]),
            lease_id=lease_id,
            trace_id=trace_id,
        )
    # A large answer is spilled to the ContentStore so the terminal
    # event stays under the payload cap (otherwise the write raises
    # PayloadTooLarge and crashes the drain). The full
    # text still lives in the final assistant Message (messages_ref) too.
    inline_answer, answer_ref = _spill_answer(ctx.content_store, decision.answer)
    return _terminate(
        ctx,
        task,
        type_="TaskCompleted",
        payload=TaskCompletedPayload(answer=inline_answer, answer_ref=answer_ref),
        lease_id=lease_id,
        trace_id=trace_id,
    )


def handle_fail(
    ctx: HandlerContext,
    task: Task,
    decision: FailDecision,
    *,
    lease_id: str,
    trace_id: str,
) -> Task:
    return _terminate(
        ctx,
        task,
        type_="TaskFailed",
        payload=TaskFailedPayload(
            reason=decision.reason, retryable=decision.retryable
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )


def handle_spawn_subtask(
    ctx: HandlerContext,
    task: Task,
    decision: SpawnSubtaskDecision,
    *,
    lease_id: str,
    trace_id: str,
) -> Task:
    """Suspend parent on a typed wake condition; bootstrap the child.

    The order matters and is fixed by the issue 03 spec:
    ``SubtaskSpawned`` (parent) → ``TaskCreated`` (child stream) →
    ``TaskSnapshot`` (parent) → ``TaskSuspended`` (parent). Then we
    return parent in ``suspended`` status; the worker releases the
    lease. The ``dispatcher.enqueue`` for the child runs in
    :class:`noeta.core.observers.ChildLifecycleObserver`.
    """
    def _on_deny(raw_reason: Optional[str]) -> Task:
        reason = raw_reason or "denied"
        ctx.emit(
            task_id=task.task_id,
            type_="SubtaskDenied",
            payload=SubtaskDeniedPayload(
                agent_name=decision.agent_name,
                goal=decision.goal,
                reason=reason,
            ),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        return handle_fail(
            ctx,
            task,
            FailDecision(reason=f"subtask denied: {reason}"),
            lease_id=lease_id,
            trace_id=trace_id,
        )

    routed = _guard_and_route(
        ctx,
        task,
        ProposedSpawnSubtask(decision=decision),
        approval_handle=f"approval-spawn-{task.task_id}",
        on_deny=_on_deny,
        lease_id=lease_id,
        trace_id=trace_id,
    )
    if routed is not None:
        return routed

    subtask_id = ctx.id_factory()
    ctx.emit(
        task_id=task.task_id,
        type_="SubtaskSpawned",
        payload=SubtaskSpawnedPayload(
            subtask_id=subtask_id,
            agent_name=decision.agent_name,
            goal=decision.goal,
            inputs=dict(decision.inputs),
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    # Child gets its own stream; trace_id propagates so observers can
    # cross-link the two streams. The narrow ``create_child_task``
    # seam (Engine._create_child_task) preserves the byte-equal
    # ``policy_name="scripted"`` literal from the pre-refactor
    # ``engine.py:723`` inline write.
    ctx.create_child_task(
        child_task_id=subtask_id,
        parent_task_id=task.task_id,
        agent_name=decision.agent_name,
        goal=decision.goal,
        inputs=dict(decision.inputs),
        trace_id=trace_id,
        subtask_depth=task.subtask_depth + 1,  # SR1: child is one deeper
    )
    return _suspend(
        ctx,
        task,
        wake_on=SubtaskCompleted(subtask_id=subtask_id),
        reason="waiting_subtask",
        lease_id=lease_id,
        trace_id=trace_id,
    )


#: The control-tool name a background ``spawn_subagent`` rides on. Spelled
#: locally (a plain string literal) so ``noeta.core`` never imports
#: ``noeta.policies`` (the policy that produces ``SpawnSubtaskDecision``); the
#: value mirrors ``noeta.policies.control_semantics.SPAWN_SUBAGENT_TOOL``.
_SPAWN_SUBAGENT_TOOL = "spawn_subagent"


def _pending_background_spawn_call_id(task: Task) -> Optional[str]:
    """The ``call_id`` of the most recent unpaired ``spawn_subagent``
    ``ToolUseBlock`` on the parent (the tool_use this background spawn answers).

    ``_apply_decision_assistant_message`` appended the assistant turn carrying
    the ``spawn_subagent`` tool_use BEFORE this handler ran, so the call_id is in
    ``runtime.messages``; we pair the "started" tool_result back to it (the same
    pairing the foreground path defers to resume via the drain's
    ``_pending_spawn_call_id``). ``None`` only if the policy somehow produced a
    background ``SpawnSubtaskDecision`` with no matching tool_use — the caller
    then falls back to the foreground spawn."""
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
                and block.tool_name == _SPAWN_SUBAGENT_TOOL
                and block.call_id not in resolved
            ):
                return block.call_id
    return None


def _append_background_spawn_result(
    ctx: HandlerContext,
    task: Task,
    *,
    call_id: str,
    success: bool,
    text: str,
    lease_id: str,
    trace_id: str,
) -> None:
    """Append the parent's ``role="tool"`` result for a background spawn and
    continue the turn (mirrors :func:`append_tool_denial_feedback`, but the
    success/error shape is caller-chosen).

    A successful launch carries the "started #N" acknowledgement (``output``);
    a guard-denied launch carries the denial as the ``error`` — either way one
    ``MessagesAppended`` pairs the dangling ``spawn_subagent`` tool_use so the
    next compose has a matching tool_result and the parent keeps deciding. The
    eventual RESULT of a launched background sub-agent never reuses this slot —
    it arrives later as a Mechanism-C turn-boundary notice."""
    block = ToolResultBlock(
        call_id=call_id,
        output=text if success else "",
        success=success,
        error=None if success else text,
    )
    msg = Message(role="tool", content=[block])
    task.runtime.messages.append(msg)
    ctx.emit(
        task_id=task.task_id,
        type_="MessagesAppended",
        payload=put_messages(ctx.content_store, [msg]),
        lease_id=lease_id,
        trace_id=trace_id,
    )


def handle_spawn_background_subtask(
    ctx: HandlerContext,
    task: Task,
    decision: SpawnSubtaskDecision,
    *,
    lease_id: str,
    trace_id: str,
) -> Optional[Task]:
    """Launch a sub-agent in the background — non-blocking (no barrier).

    docs/adr/background-subagent.md. Unlike :func:`handle_spawn_subtask` (which
    suspends the parent on a ``SubtaskCompleted`` barrier), this:

    1. runs the SAME admission guard (Permission allow-list + Budget);
    2. emits ``BackgroundSubagentStarted`` on the parent stream (the durable
       record — the parent never suspends, so there is no ``SubtaskSpawned`` +
       barrier pair);
    3. creates the child stream with ``background=True`` (so the
       ``ChildLifecycleObserver`` skips it — the driver owns its lifecycle);
    4. appends a "started #N" tool_result paired to the originating
       ``spawn_subagent`` call so the parent's turn CONTINUES (loop-back);
    5. hands the child to ``ctx.launch_background_subagent`` — the executor-driven
       background driver that runs it concurrently and delivers its result at a
       turn boundary (Mechanism C).

    Returns ``None`` to continue the parent's turn (the common path), or a
    terminal/suspended ``Task`` when the guard denies (graceful: a denial
    tool_result + continue is preferred, but a REQUIRE_APPROVAL routes to the
    same approval suspend the foreground path uses). The caller
    (:meth:`Engine.run_one_step`) only invokes this when
    ``ctx.launch_background_subagent`` is wired; otherwise the decision falls
    through to the foreground :func:`handle_spawn_subtask`.
    """
    call_id = _pending_background_spawn_call_id(task)
    if call_id is None:
        # No tool_use to pair the started/result against → fall back to the
        # foreground barrier spawn (correct, just blocking) rather than emit a
        # dangling result. Signalled by returning the foreground handler's Task.
        return handle_spawn_subtask(
            ctx, task, decision, lease_id=lease_id, trace_id=trace_id
        )

    verdict = ctx.guard(ProposedSpawnSubtask(decision=decision), task)
    if verdict.verdict is Verdict.DENY:
        # Graceful: a denied background launch does NOT fail the whole parent
        # conversation (unlike a foreground denial) — the model just gets a
        # denial tool_result and keeps its turn.
        reason = verdict.reason or "denied"
        ctx.emit(
            task_id=task.task_id,
            type_="SubtaskDenied",
            payload=SubtaskDeniedPayload(
                agent_name=decision.agent_name,
                goal=decision.goal,
                reason=reason,
            ),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        _append_background_spawn_result(
            ctx, task, call_id=call_id, success=False,
            text=f"background sub-agent denied: {reason}",
            lease_id=lease_id, trace_id=trace_id,
        )
        return None
    if verdict.verdict is Verdict.REQUIRE_APPROVAL:
        # v1 does not support mid-turn approval for a background launch (no
        # partial-launch + approval-resume); route to the SAME approval suspend
        # the foreground spawn uses, so a human can approve and the model
        # re-issues the spawn on resume.
        return _yield_for_approval(
            ctx,
            task,
            handle=f"approval-spawn-{task.task_id}",
            lease_id=lease_id,
            trace_id=trace_id,
        )

    # Per-session concurrency cap (mirrors the background-shell job cap):
    # CHECK BEFORE any durable write so an over-cap launch leaves NO
    # ``BackgroundSubagentStarted`` / child ``TaskCreated`` — the model just gets
    # a clear "too many" tool_result and keeps its turn (reject, don't queue).
    # Race-free per session: a session's turns are serial under one lease, so two
    # background spawns of one parent never run concurrently.
    if ctx.background_subagent_capacity is not None:
        rejection = ctx.background_subagent_capacity(task.task_id)
        if rejection is not None:
            _append_background_spawn_result(
                ctx, task, call_id=call_id, success=False,
                text=f"background sub-agent not started: {rejection}",
                lease_id=lease_id, trace_id=trace_id,
            )
            return None

    # --- ALLOW: launch in the background ---
    subtask_id = ctx.id_factory()
    ctx.emit(
        task_id=task.task_id,
        type_="BackgroundSubagentStarted",
        payload=BackgroundSubagentStartedPayload(
            subtask_id=subtask_id,
            agent_name=decision.agent_name,
            goal=decision.goal,
            call_id=call_id,
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    # Child gets its own stream marked ``background=True`` so the observer skips
    # its lineage/enqueue/auto-wake; the driver enqueues + drives it instead.
    ctx.create_child_task(
        child_task_id=subtask_id,
        parent_task_id=task.task_id,
        agent_name=decision.agent_name,
        goal=decision.goal,
        inputs=dict(decision.inputs),
        trace_id=trace_id,
        subtask_depth=task.subtask_depth + 1,
        background=True,
    )
    _append_background_spawn_result(
        ctx, task, call_id=call_id, success=True,
        text=(
            f'Background sub-agent "{decision.agent_name}" started '
            f"(id {subtask_id}). It runs concurrently while you keep working; "
            "its result will be delivered to you when it finishes — you do not "
            "need to wait or poll for it."
        ),
        lease_id=lease_id, trace_id=trace_id,
    )
    # Hand off to the executor-driven background driver (Mechanism C). Guarded
    # against a missing seam (defensive — the engine only routes here when wired).
    if ctx.launch_background_subagent is not None:
        ctx.launch_background_subagent(
            parent_task_id=task.task_id, child_task_id=subtask_id
        )
    return None


#: SR2 — max children a single fan-out batch may create.
#: Keeps ``SubtaskGroupCompleted.subtask_ids`` (carried in TaskSuspended /
#: snapshot ``wake_on``) well under the 4 KB envelope.
MAX_FANOUT = 16


def _spec_as_single(spec: SpawnSubtaskSpec) -> SpawnSubtaskDecision:
    """Wrap one fan-out spec as a single-spawn decision so the existing
    Guards (Permission allow-list on ``agent_name``, Budget) can check it
    unchanged."""
    return SpawnSubtaskDecision(
        agent_name=spec.agent_name, goal=spec.goal, inputs=dict(spec.inputs)
    )


def _deny_fanout_batch(
    ctx: HandlerContext,
    task: Task,
    spec: SpawnSubtaskSpec,
    *,
    reason: str,
    lease_id: str,
    trace_id: str,
) -> Task:
    """SR2 (B3/B6) — all-or-none deny: emit one ``SubtaskDenied`` (the
    failing spec for a per-spec deny, the first spec for a global one) +
    fail the parent. **Zero** ``SubtaskSpawned`` / child ``TaskCreated``."""
    ctx.emit(
        task_id=task.task_id,
        type_="SubtaskDenied",
        payload=SubtaskDeniedPayload(
            agent_name=spec.agent_name, goal=spec.goal, reason=reason
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    return handle_fail(
        ctx,
        task,
        FailDecision(reason=f"subtask denied: {reason}"),
        lease_id=lease_id,
        trace_id=trace_id,
    )


def handle_spawn_subtasks(
    ctx: HandlerContext,
    task: Task,
    decision: SpawnSubtasksDecision,
    *,
    lease_id: str,
    trace_id: str,
) -> Task:
    """SR2 — fan out N sub-agents and suspend on an all-of group join.
    **All-or-none admission** (B3): preflight every spec
    (size + duplicate call_id + per-spec guard with simulated
    ``spawned_subtasks = current + i``); on any deny / require_approval the
    whole batch denies (parent fail, zero child). Only after all pass are
    the N ``SubtaskSpawned`` + N child ``TaskCreated`` emitted (member
    order) and the parent suspended on ``SubtaskGroupCompleted``."""
    specs = decision.specs
    if not specs:
        # The policy only emits this for >=2 spawns, so an empty batch is a
        # construction bug, not a model output → fail loud (don't emit a
        # malformed SubtaskDenied).
        raise ValueError("SpawnSubtasksDecision.specs is empty")
    n = len(specs)
    call_ids = [s.call_id for s in specs]

    # --- global preflight (all-or-none): size + duplicate call_id ---
    if not (1 <= n <= MAX_FANOUT):
        return _deny_fanout_batch(
            ctx, task, specs[0],
            reason=f"fanout_batch_size:{n}>{MAX_FANOUT}",
            lease_id=lease_id, trace_id=trace_id,
        )
    if len(set(call_ids)) != n:
        return _deny_fanout_batch(
            ctx, task, specs[0], reason="fanout_batch_duplicate_call_id",
            lease_id=lease_id, trace_id=trace_id,
        )

    # --- per-spec guard preflight, simulated spawned_subtasks (B2) ---
    sim_spawned = task.governance.spawned_subtasks
    for spec in specs:
        verdict = ctx.guard(
            ProposedSpawnSubtask(decision=_spec_as_single(spec)),
            task,
            spawned_subtasks_override=sim_spawned,
        )
        if verdict.verdict is Verdict.DENY:
            return _deny_fanout_batch(
                ctx, task, spec, reason=verdict.reason or "denied",
                lease_id=lease_id, trace_id=trace_id,
            )
        if verdict.verdict is Verdict.REQUIRE_APPROVAL:
            # Approval is unsupported inside a fan-out batch in
            # v1 (no partial create, no per-spec approval-resume) → deny.
            return _deny_fanout_batch(
                ctx, task, spec, reason="approval_unsupported_in_fanout",
                lease_id=lease_id, trace_id=trace_id,
            )
        sim_spawned += 1  # current + i, not the pre-batch counter

    # --- all passed → mint N ids + group_id, emit in member order ---
    subtask_ids = tuple(ctx.id_factory() for _ in specs)
    if len(set(subtask_ids)) != n:  # defensive (B1)
        return _deny_fanout_batch(
            ctx, task, specs[0], reason="fanout_batch_duplicate_subtask_id",
            lease_id=lease_id, trace_id=trace_id,
        )
    group_id = derive_group_id(subtask_ids)
    for spec, sid in zip(specs, subtask_ids):
        # SubtaskSpawnedPayload is UNCHANGED (no call_id field) — the
        # result↔call pairing is positional from the assistant message
        # Canonical bytes of SubtaskSpawned are
        # identical to the SR1 single-child path.
        ctx.emit(
            task_id=task.task_id,
            type_="SubtaskSpawned",
            payload=SubtaskSpawnedPayload(
                subtask_id=sid,
                agent_name=spec.agent_name,
                goal=spec.goal,
                inputs=dict(spec.inputs),
            ),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        ctx.create_child_task(
            child_task_id=sid,
            parent_task_id=task.task_id,
            agent_name=spec.agent_name,
            goal=spec.goal,
            inputs=dict(spec.inputs),
            trace_id=trace_id,
            subtask_depth=task.subtask_depth + 1,  # SR1 depth, uniform
        )
    return _suspend(
        ctx,
        task,
        # fan-out v2: copy the Policy's
        # transient opt-in onto the persisted suspend condition. ``or None`` so
        # a sequential (``False``) group keeps the conditionally-folded field
        # absent → byte-identical to every pre-v2 recording.
        wake_on=SubtaskGroupCompleted(
            group_id=group_id,
            subtask_ids=subtask_ids,
            concurrent=decision.concurrent or None,
        ),
        reason="waiting_subtask_group",
        lease_id=lease_id,
        trace_id=trace_id,
    )


def handle_state_patch(
    ctx: HandlerContext,
    task: Task,
    decision: StatePatchDecision,
    *,
    lease_id: str,
    trace_id: str,
) -> None:
    """Loop-continuing state-write control tool (neutral mechanism).

    Emits the caller-built payload in the fixed order the kernel pins, so a
    control tool's conversation stays well-formed and the loop recomposes
    (no suspend, no terminal):

      1. each ``messages_before`` as its own ``MessagesAppended``
         (e.g. the assistant tool_use),
      2. ``TaskStatePatched`` — **only when** ``patch`` is set
         (e.g. ``set_todos`` / ``set_phase``),
      3. each ``messages_after`` as its own ``MessagesAppended``
         (e.g. the tool-role ack/error).

    The Engine assigns NO meaning to any of it: the Policy
    authored every message and every patch field. Returns ``None`` → the
    Engine continues the compose→decide loop (caller handles snapshot
    bookkeeping).
    """
    for raw in decision.messages_before:
        # Policy-supplied messages cannot carry origin.
        msg = strip_message_origin(raw)
        task.runtime.messages.append(msg)
        ctx.emit(
            task_id=task.task_id,
            type_="MessagesAppended",
            payload=put_messages(ctx.content_store, [msg]),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        # Slice B: a StatePatchDecision carries its out-of-band thinking on
        # the decision itself (mirroring ToolCallsDecision). The Engine's
        # ``_append_assistant_message`` never sees this message (it only
        # handles decisions with a top-level ``assistant_message`` attr),
        # so we record the thinking here against the assistant tool_use msg.
        # A single decision has a single assistant turn → only the FIRST
        # assistant message (role="assistant") is the anchor; any later
        # ones (none produced by the current seam) are ignored.
        if msg.role == "assistant":
            record_assistant_thinking(
                ctx,
                task,
                decision,
                msg,
                lease_id=lease_id,
                trace_id=trace_id,
            )
    if decision.patch is not None:
        # Mid-loop skill activation needs the same
        # SkillContentRecorded→TaskStatePatched causal order pre-loop
        # helpers already produce. Emit per-skill provenance right here
        # (first-only, fold-guarded). All four activation entry points
        # (pre-loop SDK helper, Engine.apply_state_patch,
        # _apply_decision_state_patch, and this handler) converge on
        # exactly one event per (task, skill) because every path
        # gates against fold's authoritative
        # governance.skill_content_hashes dict — no engine-level gate
        # or path-exclusion check is required.
        emit_skill_provenance_for_patch(
            ctx, task, decision.patch, lease_id=lease_id, trace_id=trace_id
        )
        ctx.emit(
            task_id=task.task_id,
            type_="TaskStatePatched",
            payload=TaskStatePatchedPayload(patch=decision.patch.to_dict()),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        decision.patch.apply(task.state)
    for raw in decision.messages_after:
        # Policy-supplied messages cannot carry origin.
        msg = strip_message_origin(raw)
        task.runtime.messages.append(msg)
        ctx.emit(
            task_id=task.task_id,
            type_="MessagesAppended",
            payload=put_messages(ctx.content_store, [msg]),
            lease_id=lease_id,
            trace_id=trace_id,
        )


#: ③ — media type for the persisted summary body (D-3c).
_SUMMARY_MEDIA_TYPE = "application/json"


def handle_compaction_requested(
    ctx: HandlerContext,
    task: Task,
    decision: CompactionRequestedDecision,
    *,
    lease_id: str,
    trace_id: str,
) -> Optional[Task]:
    """③ (D-3 / D-3b): handle one compaction step in the run loop.

    The prune (deterministic, Composer-side) and the summarize LLM
    round-trip (recorded via the Policy's ``RuntimeLLMClient.complete``)
    already happened in the Policy before this decision was returned; this
    handler only writes the durable kernel state for the step:

    1. **anti-spiral** (D-B3, finding 3): escalate to a non-retryable
       ``TaskFailed`` instead of compacting forever — but the judgement is
       **boundary progress**, not a sticky continuation tag. The previous
       ``Compacted`` advanced ``ContextState.summary_boundary`` to the prefix
       it collapsed; this step is only making progress when it would advance
       it further. So we escalate iff the boundary this step is about to write
       does **not** advance past what is already collapsed:

           ``decision.boundary_count <= task.context.summary_boundary``

       (``<= 0`` is the degenerate subset — nothing summarisable at all.)

       This deliberately *replaces* the old
       ``last_transition == "compaction_retry"/"overflow_recovery"`` check,
       which was the false-kill root cause: those tags are written via
       ``StepTransitionMarked`` only on compaction steps and folded
       last-write-wins (``fold._on_step_transition_marked``); a normal
       tool/turn step never re-marks, so the tag stayed sticky across real
       work. A long session that compacts, then does real tool work (raw
       history grows → a *larger* boundary becomes summarisable), then
       legitimately compacts again was being killed even though the second
       compaction strictly advanced the boundary. Reading the durable
       ``summary_boundary`` instead means real progress (history growth →
       boundary growth) is never mistaken for a spiral, while a genuinely
       stuck compaction (boundary cannot move) still terminates.

       Under a good Policy this kernel arm is pure defence: the Policy's own
       self-termination already refuses to emit a ``CompactionRequested``
       whose boundary would not advance (it returns
       ``FailDecision(reason="compaction_no_progress")`` instead). This arm
       only fires if a Policy bypasses that guarantee.
    2. emit the continuation tag (``overflow_recovery`` passive /
       ``compaction_retry`` proactive) so observers/inspect can see the
       continuation kind (no longer load-bearing for anti-spiral).
    3. emit ``CompactionRequested`` (observability anchor).
    4. emit ``Compacted`` (only when the policy produced a ``summary``):
       persist the summary body, carry the boundary; fold writes the
       summary slice onto ``ContextState``.
    5. return ``None`` → the Engine loops back to compose → decide with the
       compacted history.
    """
    passive = decision.reason == "overflow"
    # 1. anti-spiral via boundary progress (NOT sticky tags). A repeated
    # SUMMARIZING compaction that does not push ``summary_boundary`` forward
    # made no progress and would loop forever; escalate. ``summary_boundary``
    # is the cumulative raw-history prefix already collapsed by the previous
    # ``Compacted`` (fold writes it), so a summary whose boundary fails to
    # exceed it cannot shrink the request. ``boundary_count <= 0`` (nothing
    # summarisable) is the degenerate subset of this check. The guard is
    # scoped to ``summary is not None``: a prune-only step (``summary is None``,
    # ``boundary_count == 0``) is the Composer's deterministic tail-prune
    # bringing the estimate under the window — that IS progress and carries no
    # summary boundary to advance, so it must never escalate (D-B3, finding 3).
    if (
        decision.summary is not None
        and decision.boundary_count <= task.context.summary_boundary
    ):
        return _terminate(
            ctx,
            task,
            type_="TaskFailed",
            payload=TaskFailedPayload(
                reason="compaction_overflow_spiral", retryable=False
            ),
            lease_id=lease_id,
            trace_id=trace_id,
        )

    # 2. continuation tag (passive = overflow_recovery, proactive =
    # compaction_retry) — observability only now; folded onto last_transition
    # so inspect can see the continuation kind. NO longer read by the
    # anti-spiral check above (that reads the durable ``summary_boundary``).
    reason: TransitionReason = (
        "overflow_recovery" if passive else "compaction_retry"
    )
    emit_step_transition(
        ctx, task, reason=reason, lease_id=lease_id, trace_id=trace_id
    )

    # 3. observability anchor for *why* this compaction step ran.
    ctx.emit(
        task_id=task.task_id,
        type_="CompactionRequested",
        payload=CompactionRequestedPayload(
            reason=decision.reason,
            estimated_tokens=decision.estimated_tokens,
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )

    # 4. durable result — only when the policy summarized (prune alone may
    # have sufficed → no Compacted event, no summary slice change).
    if decision.summary is not None:
        summary_ref = ctx.content_store.put(
            to_canonical_bytes(decision.summary),
            media_type=_SUMMARY_MEDIA_TYPE,
        )
        env = ctx.emit(
            task_id=task.task_id,
            type_="Compacted",
            payload=CompactedPayload(
                summary_ref=summary_ref,
                boundary_count=decision.boundary_count,
                replaced_count=decision.boundary_count,
                composer_version=decision.composer_version,
            ),
            lease_id=lease_id,
            trace_id=trace_id,
        )
        ctx.apply_event(task, env)
    return None


def handle_tool_calls(
    ctx: HandlerContext,
    task: Task,
    decision: ToolCallsDecision,
    *,
    lease_id: str,
    trace_id: str,
) -> Optional[Task]:
    """Run each call past its Guard, then batch the results.

    Returns ``None`` to signal the compose → decide loop should
    continue (in-place tool_calls iteration); returns a suspended
    ``Task`` only when a Guard returned ``require_approval`` and the
    call converted to ``yield_for_human``. This is the **only**
    Decision handler whose return type is ``Optional[Task]`` —
    routed via ``run_one_step``'s special case, not ``dispatch_exit``.
    """
    if ctx.tool_invoker is None:
        raise RuntimeError("Engine got tool_calls but no ToolRuntime.")

    result_blocks: list[ToolResultBlock] = []
    for idx, call in enumerate(decision.calls):
        verdict = ctx.guard(ProposedToolCall(call=call), task)
        if verdict.verdict is Verdict.DENY:
            reason = verdict.reason or "denied"
            ctx.emit(
                task_id=task.task_id,
                type_="ToolCallDenied",
                payload=ToolCallDeniedPayload(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    reason=reason,
                ),
                lease_id=lease_id,
                trace_id=trace_id,
            )
            # Structural invariant: every tool_use the model emitted must
            # get a matching tool_result before the next compose→decide, or
            # the outgoing request carries a dangling function call that
            # providers (OpenAI Responses / Anthropic) reject with a fatal
            # 400. A denial is feedback, not an omission — surface it as a
            # failed result so history stays balanced and the model can adapt
            # (e.g. wrap up once a budget cap like max_tool_calls is hit).
            result_blocks.append(
                ToolResultBlock(
                    call_id=call.call_id,
                    output=reason,
                    success=False,
                    error=reason,
                )
            )
            continue
        if verdict.verdict is Verdict.REQUIRE_APPROVAL:
            # Issue A: record the blocked call as a durable recovery
            # anchor BEFORE suspending, so resume can reconstruct the
            # exact ToolCall from the log/snapshot after a restart.
            ctx.emit(
                task_id=task.task_id,
                type_="ToolCallApprovalRequested",
                payload=build_tool_call_approval_requested_payload(
                    call, ctx.content_store
                ),
                lease_id=lease_id,
                trace_id=trace_id,
            )
            # Structural invariant (see DENY branch above): every tool_use
            # the model emitted in this assistant turn must get a matching
            # tool_result before the next compose→decide, or the resumed
            # request carries a dangling function call that providers reject
            # with a fatal 400. The assistant message carrying ALL parallel
            # tool_use blocks was already committed before this handler ran,
            # so suspending mid-batch would otherwise leave two classes of
            # dangling blocks:
            #   (1) EARLIER calls already executed into ``result_blocks`` —
            #       their results would be discarded by the early return.
            #   (2) TRAILING calls after this one — never executed at all.
            # The approval-requiring call itself is resolved on resume
            # (``invoke_approved_tool_call`` on approve / ``append_tool_denial_feedback``
            # on deny), so it is the ONLY tool_use left for the resume path
            # to balance. Flush (1) and synthesize skipped results for (2)
            # here so the suspend/resume boundary stays balanced regardless
            # of the human's approve/deny choice.
            for trailing in decision.calls[idx + 1 :]:
                skipped = "skipped: a prior call in this batch awaits approval"
                result_blocks.append(
                    ToolResultBlock(
                        call_id=trailing.call_id,
                        output=skipped,
                        success=False,
                        error=skipped,
                    )
                )
            if result_blocks:
                batched = Message(role="tool", content=list(result_blocks))
                task.runtime.messages.append(batched)
                ctx.emit(
                    task_id=task.task_id,
                    type_="MessagesAppended",
                    payload=put_messages(ctx.content_store, [batched]),
                    lease_id=lease_id,
                    trace_id=trace_id,
                )
            return _yield_for_approval(
                ctx,
                task,
                handle=f"approval-{call.call_id}",
                lease_id=lease_id,
                trace_id=trace_id,
            )
        tool = ctx.resolve_tool(call)
        result = ctx.tool_invoker.invoke(
            tool,
            call,
            task_id=task.task_id,
            lease_id=lease_id,
            trace_id=trace_id,
        )
        result_blocks.append(
            wrap_tool_result_block(
                call,
                result,
                tool_output_inline_limit=ctx.tool_output_inline_limit,
            )
        )

    if not result_blocks:
        return None
    # One MessagesAppended for the whole batch (acceptance: N calls →
    # one event). Per OpenAI / Anthropic convention tool_results
    # travel on a role="tool" message containing a list of
    # ToolResultBlocks; one message per batch.
    batched = Message(role="tool", content=list(result_blocks))
    task.runtime.messages.append(batched)
    ctx.emit(
        task_id=task.task_id,
        type_="MessagesAppended",
        payload=put_messages(ctx.content_store, [batched]),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    return None


# ---------------------------------------------------------------------------
# Exit dispatch
# ---------------------------------------------------------------------------


def dispatch_exit(
    ctx: HandlerContext,
    task: Task,
    decision: Decision,
    *,
    lease_id: str,
    trace_id: str,
) -> Task:
    """Route an exit-class Decision to its typed handler.

    Exit decisions produce a final ``Task`` (terminal or suspended)
    and exit the compose → decide loop. ``ToolCallsDecision`` is NOT
    routed here (it loops back; see ``Engine.run_one_step``'s special
    case). ``WaitExternalDecision`` is also NOT routed (no handler
    exists; preserves pre-refactor ``NotImplementedError`` shape).
    """
    if isinstance(decision, SpawnSubtaskDecision):
        return handle_spawn_subtask(
            ctx, task, decision, lease_id=lease_id, trace_id=trace_id
        )
    if isinstance(decision, SpawnSubtasksDecision):
        return handle_spawn_subtasks(
            ctx, task, decision, lease_id=lease_id, trace_id=trace_id
        )
    if isinstance(decision, FinishDecision):
        return handle_finish(
            ctx, task, decision, lease_id=lease_id, trace_id=trace_id
        )
    if isinstance(decision, FailDecision):
        return handle_fail(
            ctx, task, decision, lease_id=lease_id, trace_id=trace_id
        )
    if isinstance(decision, WaitTimerDecision):
        return handle_wait_timer(
            ctx, task, decision, lease_id=lease_id, trace_id=trace_id
        )
    if isinstance(decision, YieldForHumanDecision):
        return handle_yield_for_human(
            ctx, task, decision, lease_id=lease_id, trace_id=trace_id
        )
    # Byte-equal preservation: Engine.py:405-406 raised this exact
    # message for any unmapped Decision (including WaitExternalDecision).
    # Do NOT rephrase — acceptance test pins the full string.
    raise NotImplementedError(
        f"Unknown decision type: {type(decision).__name__}"
    )
