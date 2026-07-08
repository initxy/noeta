"""Event envelope and Phase-0 event payload dataclasses.

The envelope is the universal record format on an EventLog stream. The
payload is a typed dataclass attached per ``type``. Phase 0 ships only the
event types exercised by the minimal task happy path; other types are
added incrementally by later issues.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal, Optional

from noeta.protocols.canonical import from_canonical_bytes, register
from noeta.protocols.content_store import ContentStore
from noeta.protocols.messages import Usage
from noeta.protocols.values import ContentRef
from noeta.protocols.wake import SubtaskResult, WakeCondition


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


#: Typed source-of-write marker. Names the Noeta role that appended the
#: event to its stream, orthogonal to ``actor`` (which carries the
#: writer's *identity*; the same observer class might run under different
#: actor labels in tests). Surfaced as descriptive provenance in the audit
#: trail (``AuditObserver``) and the events HTTP/JSON API; the read model
#: and front-end can show which role wrote each event.
EventOrigin = Literal["engine", "llm", "observer", "tool", "system"]


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Universal envelope wrapping any event on an EventLog stream.

    ``seq`` is assigned by the log on append. Construct envelopes with
    ``seq=0`` (or any placeholder) and the log will return a stamped copy.
    """

    id: str
    task_id: str
    seq: int
    type: str
    schema_version: int
    occurred_at: float
    actor: str
    trace_id: str
    correlation_id: str
    causation_id: Optional[str]
    payload: Any
    origin: EventOrigin = "engine"

    def with_seq(self, seq: int) -> "EventEnvelope":
        return replace(self, seq=seq)

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        type: str,
        payload: Any,
        id: str = "evt-pending",
        actor: str = "engine",
        trace_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        schema_version: int = 1,
        occurred_at: float = 0.0,
        origin: EventOrigin = "engine",
    ) -> "EventEnvelope":
        """Build a pre-append envelope with sensible defaults.

        Centralises the field defaults every EventLog backend would
        otherwise re-spell:

        * ``seq=0`` (the log stamps the real value on append),
        * ``correlation_id=task_id`` (each stream is its own correlation
          for now),
        * ``trace_id`` falls back to ``"trace-unknown"``,
        * ``origin="engine"`` (the most common writer; LLM / Observer /
          Tool call sites override).

        Callers override only the fields that actually vary per backend:
        ``id`` (mint policy), ``occurred_at`` (clock), and
        ``schema_version`` (envelope generation).
        """
        return cls(
            id=id,
            task_id=task_id,
            seq=0,
            type=type,
            schema_version=schema_version,
            occurred_at=occurred_at,
            actor=actor,
            trace_id=trace_id or "trace-unknown",
            correlation_id=task_id,
            causation_id=causation_id,
            payload=payload,
            origin=origin,
        )


# ---------------------------------------------------------------------------
# Phase-0 event payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskCreatedPayload:
    """The genesis event of a Task stream.

    Carries the immutable header (goal, principal, contract, budget) that
    later ``fold`` calls use to bootstrap empty state. Phase 0 keeps the
    shape minimal; more fields land alongside their consumers.
    """

    goal: str
    policy_name: str
    agent_name: str = "unnamed"
    parent_task_id: Optional[str] = None
    inputs: dict[str, Any] = field(default_factory=dict)
    #: SR1 — delegation depth decided at creation: root tasks carry 0, a
    #: child carries ``parent.subtask_depth + 1``. Recorded (not derived by
    #: walking the parent chain) so audit / fold / snapshot / resume all see
    #: it directly. Old recordings without the key restore to 0.
    subtask_depth: int = 0
    #: background sub-agent (docs/adr/background-subagent.md): ``True`` marks a
    #: child spawned by ``spawn_subagent(background=True)``. The
    #: ``ChildLifecycleObserver`` reads it to SKIP this child's lineage / enqueue
    #: / auto-wake (the parent never suspended on it — the background-subagent
    #: driver owns its lifecycle instead). ``None`` (default) is the ordinary
    #: foreground child; ``__canonical_omit_none__`` drops the absent key so every
    #: pre-existing recording is byte-identical (same rule as ``answer_ref``).
    background: Optional[bool] = None

    __canonical_omit_none__ = frozenset({"background"})


@dataclass(frozen=True, slots=True)
class TaskStartedPayload:
    """Emitted by the Engine before the first decision is dispatched."""

    lease_id: str


@dataclass(frozen=True, slots=True)
class TaskStatePatchedPayload:
    """Records a typed ``TaskStatePatch`` applied to the ``state`` slice.

    The patch author is normally a Policy (``Decision.state_patch``);
    Phase 4 (B17) adds one narrow operator-driven author,
    :meth:`noeta.core.engine.Engine.apply_state_patch`, used by the
    Noeta-Code runner for pre-loop skill activation. Both authors emit a
    byte-equal ``TaskStatePatchedPayload`` (same ``patch`` dict + same
    canonical encoding), so fold / resume treat them
    identically and the single-writer rule is preserved
    through one shape.

    The patch is stored as a plain dict so the canonical encoding does
    not depend on the live :class:`noeta.protocols.decisions.TaskStatePatch`
    dataclass; :meth:`TaskStatePatch.from_dict` is the typed inverse.
    """

    patch: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MessagesAppendedPayload:
    """Records new messages appended to ``RuntimeState.messages``.

    Issue 14 reshape: the message bodies live in ContentStore behind
    ``messages_ref`` (canonical-serialized ``list[Message]``); the
    envelope only carries the ref + a count so a single
    ``MessagesAppended`` envelope stays well under the 4 KB
    ceiling no matter how large the message bodies are.

    fold dereferences ``messages_ref`` to rebuild
    ``RuntimeState.messages``. The ref is content-addressed, so the same
    message bodies always hash to the same ``messages_ref``.
    """

    messages_ref: ContentRef
    count: int


@dataclass(frozen=True, slots=True)
class TaskSnapshotPayload:
    """Pointer to the full 4-slice serialized state body in ContentStore."""

    state_ref: ContentRef


@dataclass(frozen=True, slots=True)
class TaskRewoundPayload:
    """Conversation rewind as a snapshot-shaped marker event.

    Rewinding to a target ``seq`` does NOT truncate or rewrite the log
    (append-only). The driver folds the state to ``target_seq``,
    serialises it (the SAME 4-slice body :class:`TaskSnapshotPayload`
    points at), stores it in the ContentStore, and **appends** this marker.
    fold treats the latest ``TaskRewound`` exactly like the latest
    ``TaskSnapshot`` — its ``state_ref`` is the rebuild baseline (same code
    path, ``find_latest_snapshot`` returns whichever has the higher seq) —
    so the ``target_seq+1..M`` segment becomes folded-over dead history
    (still on the stream, still auditable). ``target_seq`` is recorded for
    the read model / front-end timeline truncation; the baseline itself is
    ``state_ref``. Absent from any historical recording → byte-safe (same
    additive-event rule as ``ModelBound`` / ``TaskCancelled``)."""

    target_seq: int
    state_ref: ContentRef


@dataclass(frozen=True, slots=True)
class StepAttemptAbandonedPayload:
    """Crash-recovery seal over an interrupted decide→act attempt.

    A worker crash mid-step leaves the attempt's partial events on the
    stream (a ``ContextPlanComposed`` with no reachable suspend/terminal
    behind it). Recovery folds the state to just before that attempt's
    ``ContextPlanComposed``, serialises it (the same 4-slice body
    :class:`TaskSnapshotPayload` points at), stores it in the
    ContentStore, and **appends** this marker — the snapshot-shaped
    re-base pattern of :class:`TaskRewoundPayload`, scoped to one
    attempt. fold treats it as a rebuild baseline (same
    ``find_latest_snapshot`` path), so the partial attempt becomes
    folded-over dead history (still on the stream, still auditable) and
    the re-driven attempt's events continue from a clean state.

    ``abandoned_from_seq`` is the seq of the interrupted attempt's anchor
    — its ``ContextPlanComposed`` (the implicit attempt-start record) or,
    for an interrupted approval execution, the first activity event of
    the plan-less window — for the read model / trace timeline.
    ``reason`` says why the seal was written: ``"auto_redrive"`` (a
    provably safe attempt, re-driven with no human),
    ``"unsafe_tool_activity"`` (parked — unattended-unsafe activity in
    the tail), ``"interrupted_approval"`` (parked — the crash hit a
    human-approved tool execution; the seal restores the pending
    approval) or ``"abandon_cap"`` (the consecutive-abandon cap forced a
    park). Absent from any historical recording → byte-safe (same
    additive-event rule as ``TaskRewound``)."""

    abandoned_from_seq: int
    state_ref: ContentRef
    reason: str


@dataclass(frozen=True, slots=True)
class ContextPlanComposedPayload:
    """Issue 14: Engine emits this per step in front of the LLM round-trip.

    ``plan_ref`` points at the canonical bytes of the
    :class:`noeta.protocols.context_plan.ContextPlan` body in
    ContentStore. ``fold`` writes ``task.context.plan_ref`` from this
    event (single-writer: Engine, not Composer).

    ``plan_ref`` is ``None`` when the composer produced no stored plan
    (the protocols-only ``PassthroughComposer`` fallback). The event is
    emitted **unconditionally once per Engine step** either way — it is
    the step-boundary event ``fold`` counts ``governance.iterations``
    from, so the ``BudgetGuard.max_iterations`` cap must not depend on
    which composer is wired (core #2). Byte-safety: the shipped
    ``ThreeSegmentComposer`` always sets ``plan_ref``, so every
    historical recording carries a non-``None`` value and refolds
    byte-identically; only Passthrough-composed steps (which previously
    emitted nothing) write the new ``null`` shape.
    """

    plan_ref: Optional[ContentRef] = None


@dataclass(frozen=True, slots=True)
class TaskCompletedPayload:
    """Terminal event: Task finished successfully with ``answer``.

    A large ``answer`` is spilled to the ContentStore (unbounded
    bodies must not live inline in an event payload, which is capped at
    ``EVENT_PAYLOAD_MAX_BYTES``) — ``answer`` then holds ``None`` and the full
    value is reachable via ``answer_ref``. Small answers stay inline with
    ``answer_ref=None``; ``__canonical_omit_none__`` drops the absent ref so a
    small-answer event is byte-identical to a pre-spill recording. Read the
    full value with :func:`answer_from_payload`."""

    answer: Any
    answer_ref: Optional[ContentRef] = None

    __canonical_omit_none__ = frozenset({"answer_ref"})


def answer_from_payload(
    payload: "TaskCompletedPayload", content_store: ContentStore
) -> Any:
    """The full answer for a ``TaskCompleted`` payload: derefs ``answer_ref``
    (set when the answer was spilled to the ContentStore) or returns the inline
    ``answer``. The single reader every consumer should use so the spill is
    transparent."""
    if payload.answer_ref is not None:
        return from_canonical_bytes(content_store.get(payload.answer_ref))
    return payload.answer


@dataclass(frozen=True, slots=True)
class TaskFailedPayload:
    """Terminal event: Task failed with ``reason``."""

    reason: str
    retryable: bool = False


# -- Tool events ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCallStartedPayload:
    """Marks the start of a single tool invocation.

    The call's arguments are captured verbatim so the recorded stream is the
    durable truth of the call (a fold reads them back). Arguments that fit the
    EventLog's 4-KB payload ceiling
    stay inline in ``arguments``; oversized arguments are
    offloaded to the ContentStore and referenced by ``arguments_ref``
    instead — exactly one of the two is populated. Build payloads via
    :func:`noeta.protocols.tool_args.build_tool_call_started_payload` and read the
    arguments back via :func:`noeta.protocols.tool_args.resolve_tool_call_arguments`
    so both the inline and offloaded shapes are handled in one place.
    """

    call_id: str
    tool_name: str
    arguments: dict[str, Any] | None = None
    arguments_ref: ContentRef | None = None


@dataclass(frozen=True, slots=True)
class FileBaseline:
    """The rewind baseline for ONE file, this turn's first edit.

    The authoritative "what did this file look like BEFORE the AI first touched
    it this turn" record. ``path`` is workspace-relative; ``content_ref`` points
    at the PRE-edit bytes in the ContentStore (content-addressed → dedup'd:
    an unchanged file across turns costs nothing). ``content_ref is None`` means
    the file did NOT exist before (AI created it) → a rewind past this turn
    DELETES it. Recorded ONLY on the gate-miss (the turn's first edit of this
    path), so "which files did this turn change, and how to undo them" folds
    straight out of the event stream — no resume-time disk read."""

    path: str
    content_ref: Optional[ContentRef] = None

    __canonical_tag__ = "file_baseline"
    __canonical_omit_none__ = frozenset({"content_ref"})


register("file_baseline", lambda f: FileBaseline(**f))


@dataclass(frozen=True, slots=True)
class ToolResultRecordedPayload:
    """Records the outcome of a tool call.

    The full output body is in ContentStore — only the ``output_ref`` is
    inline, satisfying the 4-KB ceiling for arbitrarily large
    results. ``summary`` is a short human/agent-readable description;
    ``artifacts`` lists any extra ContentRefs the tool produced;
    ``side_effects`` is a list of typed claims (e.g. file writes, HTTP
    calls) for the audit trail.

    ``file_baselines`` carries the rewind baseline for each file
    this tool call edited for the FIRST time this turn (the per-turn gate
    deduped repeats). Empty on every non-fs / dry-run / repeat-edit call →
    ``__canonical_omit_none__`` drops it so a recording without checkpoints is
    byte-identical to a pre-0043 one. ``fold`` reads it to project "the files
    this turn changed"; the live rewind restore writes those baselines back.
    """

    call_id: str
    success: bool
    output_ref: ContentRef
    summary: str
    artifacts: list[ContentRef] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    file_baselines: Optional[list[FileBaseline]] = None

    __canonical_omit_none__ = frozenset({"file_baselines"})


@dataclass(frozen=True, slots=True)
class ToolCallFinishedPayload:
    """Marks the end of a tool invocation (success or controlled failure)."""

    call_id: str


# -- Subtask + suspend events ----------------------------------------------


@dataclass(frozen=True, slots=True)
class SubtaskSpawnedPayload:
    """Parent stream: parent's Policy asked to spawn a child Task.

    The full child spec is captured inline (Phase 0 inputs stay well under
    the 4-KB envelope ceiling; larger inputs would need a ContentRef).
    """

    subtask_id: str
    agent_name: str
    goal: str
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubtaskCompletedPayload:
    """Parent stream: a previously-spawned child Task reached terminal.

    Engine emits this on the parent's stream as part of the
    child-completion handoff. The body is small — large outputs from
    the child remain in their own ContentStore refs.
    """

    subtask_id: str
    result: SubtaskResult


@dataclass(frozen=True, slots=True)
class TaskSuspendedPayload:
    """Engine pauses the Task and releases the lease.

    ``reason`` is a short tag (``waiting_subtask`` / ``waiting_human`` /
    ``waiting_timer`` / ``waiting_external``); ``wake_on`` is the typed
    WakeCondition that the Dispatcher matches against incoming wake
    events.
    """

    reason: str
    wake_on: WakeCondition


@dataclass(frozen=True, slots=True)
class TaskWokenPayload:
    """Engine re-leased the Task after a matching wake event arrived.

    ``wake_event`` is the WakeCondition shape that fired (e.g. the
    ``SubtaskCompleted`` payload). Phase 0 only emits this in the
    spawn_subtask → wake path; later issues use the same payload for
    HITL / timer / external wakes.
    """

    wake_event: WakeCondition


# -- Guard verdict events --------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCallDeniedPayload:
    """A Guard denied a single ``tool_call`` inside a ``tool_calls`` batch.

    The Engine still processes the rest of the batch — only the denied
    call is skipped — so the parent stream records one
    ``ToolCallDenied`` event per blocked call rather than a single
    batch-level rejection. ``reason`` is the Guard's free-form
    explanation.
    """

    call_id: str
    tool_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ToolCallApprovalRequestedPayload:
    """A Guard returned ``require_approval`` for a single ``tool_call``;
    the task is about to suspend for human approval (Phase 4.5 Issue A).

    This is the **durable recovery anchor**: it records the blocked call
    *before* the suspend so the exact ``ToolCall`` can be reconstructed
    from the EventLog (+ snapshot) on resume — on a fresh process or a
    different worker — without relying on in-memory runner state. Field
    shape mirrors :class:`ToolCallStartedPayload`: arguments that fit the
    4-KB payload ceiling stay inline in ``arguments``; oversized arguments
    are offloaded to the (equally durable) ContentStore and referenced by
    ``arguments_ref`` — exactly one of the two is populated. Build via
    :func:`noeta.protocols.tool_args.build_tool_call_approval_requested_payload`
    and read back via
    :func:`noeta.protocols.tool_args.resolve_tool_call_arguments`.
    """

    call_id: str
    tool_name: str
    arguments: dict[str, Any] | None = None
    arguments_ref: ContentRef | None = None


@dataclass(frozen=True, slots=True)
class ToolCallApprovalResolvedPayload:
    """The human approve/deny decision for a pending tool-call approval
    (Phase 4.5 Issue A).

    The single authoritative record of the resolution — a deny does
    **not** also emit a ``ToolCallDenied`` event. Fold appends every
    resolution to ``governance.approvals`` (audit) and, when
    ``approved`` is ``False``, also to ``governance.denied`` (the
    established governance counter/effect). ``reason`` / ``resolver`` are
    optional: ``resolver`` is a host/identity tag (``"host"`` by default,
    a user id from a future approval API).
    """

    call_id: str
    tool_name: str
    approved: bool
    reason: Optional[str] = None
    resolver: Optional[str] = None


@dataclass(frozen=True, slots=True)
class UserQuestionRequestedPayload:
    """Neutral durable audit anchor for a structured human-input request.

    A generic HITL audit event — "a structured human prompt was posted".
    The kernel never decodes the body or enforces any schema/caps: the full
    request body lives in ContentStore behind the opaque ``questions_ref``
    (so the EventLog payload stays below the 4 KB envelope cap), and
    the SDK owns the schema/validators behind it. Produced by the neutral
    ``yield_for_human`` suspend-with-anchor path
    (:class:`~noeta.protocols.decisions.HitlRequestAnchor`).
    """

    question_id: str
    call_id: str
    questions_ref: ContentRef
    question_count: int
    reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class UserQuestionAnsweredPayload:
    """Neutral human answer audit for a pending structured human-input
    request. ``answered_by`` is a host/identity tag (``"host"`` by default)."""

    question_id: str
    call_id: str
    answers_ref: ContentRef
    answer_count: int
    answered_by: Optional[str] = None


# -- LLM events -------------------------------------------------------------
#
# Phase 0 ships only StubPolicy paths that never invoke a real LLM, so
# these payloads are not produced in the kernel happy path. They are
# defined here so the protocol surface listed in PRD §"Event catalog (Phase 0)"
# is complete and so Phase 1's real LLM adapter slots in without
# touching ``noeta.protocols``. Once a Phase 1 adapter
# starts emitting these events, fixtures upgrade from raw-dict payloads
# to these typed shapes with no callsite churn.


@dataclass(frozen=True, slots=True)
class MessageSelection:
    """Provenance of the policy's message selection for one LLM round-trip
    (MS1). **Event-only metadata** — it is recorded on
    :class:`LLMRequestStartedPayload`, never placed on the provider-facing
    :class:`noeta.protocols.messages.LLMRequest` and never hashed into
    ``request_ref``. Provider adapters / fakes must not import it.

    All-scalar (4 KB-trivial). v1 records counts + the truncation
    ``strategy`` + the ``limit``; per-message identity of dropped messages
    is deferred (derivable from the folded ``MessagesAppended`` history).

    Presence semantics: a policy that does message selection (ReAct)
    records this on **every** round-trip — even a non-truncating one
    (``dropped == 0``). So ``selection is not None`` means "a policy
    selection summary was recorded," NOT "truncation happened" — truncation
    happened iff ``dropped > 0``. A policy that does no selection records
    ``None``.

    ③ (D-3f) adds two additive counters for the compaction path:

    * ``pruned`` — number of tool-result outputs nullified outside the
      protected tail window (deterministic prune; the message stays in the
      history with its ``output`` blanked, so ``selected`` is unchanged).
    * ``summarized`` — number of messages collapsed into a single summary
      message by the summarize pass (0 when no summary happened this turn).

    Both default 0 and the canonical / sqlite restorers read them via
    ``.get`` so a pre-③ body restores unchanged — additive, no
    ``schema_version`` bump (the MS1 convention). They are observability
    metadata only and never enter ``request_ref``. ``strategy`` widens to
    include ``"prune"`` / ``"summarize"`` for these turns.
    """

    strategy: str       # e.g. "tail_window" / "prune" / "summarize"
    candidates: int     # messages available before selection
    selected: int       # messages kept (== len(request.messages))
    dropped: int        # candidates - selected
    limit: int          # the policy's max_history_messages
    pruned: int = 0     # ③: tool outputs nullified outside the tail window
    summarized: int = 0  # ③: messages collapsed into a summary this turn

    __canonical_tag__ = "message_selection"


def _restore_message_selection(fields: dict[str, Any]) -> "MessageSelection":
    """Canonical restorer — fail loud on a missing v1 field; tolerate a
    missing ③ field via ``.get`` (additive, byte-safe for old bodies)."""
    return MessageSelection(
        strategy=fields["strategy"],
        candidates=fields["candidates"],
        selected=fields["selected"],
        dropped=fields["dropped"],
        limit=fields["limit"],
        pruned=fields.get("pruned", 0),
        summarized=fields.get("summarized", 0),
    )


register("message_selection", _restore_message_selection)


@dataclass(frozen=True, slots=True)
class LLMRequestStartedPayload:
    """Marks the start of an LLM round-trip.

    ``request_ref`` is the ContentStore hash of the canonicalized View
    that was sent (body lives in ContentStore so the 4-KB envelope cap
    is respected). ``model`` is the model id (e.g.
    ``claude-opus-4-7``); ``input_tokens`` is the adapter's pre-call
    token count (or 0 if unknown).

    ``selection`` (MS1) is the policy's message-selection provenance for
    this round-trip — counts + truncation strategy. Defaulted ``None`` so
    pre-MS1 payloads restore cleanly; it is additive observability metadata
    and is **not** part of ``request_ref`` (the request bytes / hash are
    unchanged by MS1).
    """

    call_id: str
    model: str
    request_ref: ContentRef
    input_tokens: int = 0
    selection: Optional[MessageSelection] = None


@dataclass(frozen=True, slots=True)
class LLMResponseRecordedPayload:
    """Records the LLM's response body for resume.

    The full response body is in ContentStore behind ``response_ref``;
    the inline payload stays under 4 KB even for very long completions.
    ``stop_reason`` is the Noeta-shape terminal signal
    (``tool_use`` / ``end_turn`` / ``max_tokens`` / ``error``) —
    adapters normalise their vendor's wording before recording.
    """

    call_id: str
    response_ref: ContentRef
    stop_reason: str
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AssistantThinkingRecordedPayload:
    """Records the extended-thinking blocks of one assistant turn (Slice B).

    Emitted alongside the assistant turn's ``MessagesAppended`` whenever the
    LLM produced ``ThinkingBlock``s ahead of its ``tool_use``. The blocks
    themselves live in ContentStore behind ``thinking_ref`` (a
    canonical-encoded ``list[ThinkingBlock]``) so the inline payload stays
    under the 4 KB cap even for long reasoning; ``call_id`` is the
    turn's first ``tool_use`` id, the stable key fold writes them under in
    ``ContextState.thinking_by_call_id``. ``block_count`` is inline
    provenance for inspect (mirrors ``MessagesAppended.count``).

    Why a dedicated event rather than re-using ``LLMResponseRecorded``: the
    response is emitted inside the LLM client (buried under the Policy), so
    its ``response_ref`` never reaches the Engine — but the Engine DOES
    process the Policy's ``Decision``, which now carries the thinking. This
    keeps the slice's single writer the Engine/fold pair, exactly
    like ``ContextPlanComposed``, with the 12 ``runtime.messages`` append
    sites untouched.
    """

    call_id: str
    thinking_ref: ContentRef
    block_count: int = 0


@dataclass(frozen=True, slots=True)
class LLMRequestFinishedPayload:
    """Marks the end of an LLM round-trip (success or controlled failure).

    Pairs 1:1 with ``LLMRequestStarted`` via ``call_id``. ``success``
    distinguishes "the adapter received a usable response" from
    "transport / parse failure"; cost is in USD when the adapter can
    compute it, else 0. ``latency_ms`` is the wall-clock duration of
    the provider round-trip (clock-injected and non-deterministic — every
    re-run produces a different value).

    ``usage`` (Foundation A) is the typed token accounting for this round-trip,
    which fold accumulates into per-token counters on
    ``GovernanceState``. Defaulted to empty ``Usage()`` so pre-Foundation-A
    recordings (whose payload dict has no ``usage`` key) restore cleanly
    via the dataclass default — additive, byte-safe, no
    ``schema_version`` bump (the ModelBound/AgentBound "new field absent
    on old recording" pattern).
    """

    call_id: str
    success: bool
    cost_usd: float = 0.0
    latency_ms: int = 0
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class LLMRetryScheduledPayload:
    """A transient LLM failure was scheduled for a live retry.

    Emitted by ``RuntimeLLMClient`` between attempts of one logical request —
    BEFORE the backoff sleep — so a live consumer (the web chat via the SSE
    envelope stream) can show "rate-limited, retrying" instead of a silent
    multi-second stall. Observational only: fold registers it as a no-op
    (no state slice changes), the Started/Recorded/Finished trio still fires
    exactly once per logical request, and the type is simply absent from old
    recordings (the additive-event pattern → no ``schema_version`` bump).

    ``attempt`` is the 1-based index of the retry being scheduled, out of
    ``max_retries``; ``delay_seconds`` is the backoff actually chosen
    (``Retry-After`` verbatim when the provider sent one, else the jittered
    exponential); ``error`` is the transient failure's message, truncated at
    the emit site so the inline payload stays far under the 4 KB cap.
    """

    call_id: str
    attempt: int
    max_retries: int
    delay_seconds: float
    category: str
    error: str = ""


@dataclass(frozen=True, slots=True)
class StepTransitionMarkedPayload:
    """Foundation B — tags *why* a step had a next step (README D-B1).

    A NEW independent event (not a reuse of ``TaskWoken`` / ``TaskSuspended``)
    so old recordings stay byte-equal: the type is simply absent from them
    (the ModelBound/AgentBound "new event type absent on old recording"
    pattern → no ``schema_version`` bump). The Engine emits it **only** for a
    non-default continuation (``approval_resume`` and, in ②/③, the
    retry/overflow/compaction reasons); the implicit ``next_turn`` default is
    never emitted (D-B2). Fold projects ``reason`` onto
    ``RuntimeState.last_transition`` so the recovery guards read it O(1).

    ``reason`` is a plain string carrying a
    :data:`noeta.protocols.step_transition.TransitionReason` value — stored as
    ``str`` (not the ``Literal``) so a future producer can write a reason this
    runtime version does not know yet and fold tolerates it (D-B5) instead of
    crashing the restore. ``attempt`` is reserved for ②'s retry ladder
    (defaults 0; Foundation B never increments it), additive and byte-safe.
    """

    reason: str
    attempt: int = 0


# -- ③ memory management — unified compaction (README D-3) ------------------


@dataclass(frozen=True, slots=True)
class CompactionRequestedPayload:
    """③ — a compaction step ran (observability anchor, fold no-op).

    The unified compaction contract (D-3): a ``CompactionRequested``
    decision is handled in the Engine's existing step loop, NOT a
    background worker. This event records *why* the step ran so inspect
    can see it; fold derives no state from it (the actual state
    change is the paired ``Compacted`` event).

    ``reason`` is a neutral string ∈ {``"overflow"`` (passive — the
    provider returned ``ContextOverflowError``, ②'s ``category=overflow``)
    , ``"proactive"`` (the policy's pre-call estimate hit the available
    window)}. ``estimated_tokens`` is the deterministic estimate (D-3d)
    that drove the trigger, for diagnostics only. A NEW event type → absent
    from old recordings → byte-equal, no ``schema_version`` bump.
    """

    reason: str
    estimated_tokens: int = 0


@dataclass(frozen=True, slots=True)
class CompactedPayload:
    """③ — the durable result of one compaction step (fold writes state).

    The summarize pass replaced the first ``boundary_count`` messages of
    the rolling history with a single summary message whose body lives
    behind ``summary_ref`` (ContentStore, so the envelope stays under the
    4 KB cap). Fold projects ``summary_ref`` + the boundary onto
    ``ContextState`` (single writer); the Composer reads that
    slice to swap the covered prefix for the summary on the next compose.

    ``replaced_count`` records how many messages were collapsed (==
    ``boundary_count`` in the MVP single-pass replacement, D-3c).
    ``composer_version`` ties the result to the Composer generation that
    produced the surrounding plan (so a resume can tell whether it is
    re-deriving the plan under a matching Composer generation). New event
    type → byte-safe on old streams.
    """

    summary_ref: ContentRef
    boundary_count: int
    replaced_count: int
    composer_version: str


# -- Lifecycle / lease events (issue 06) -----------------------------------


@dataclass(frozen=True, slots=True)
class ModelBoundPayload:
    """The model selector that was authorized + bound for the next turn(s).

    Issue 06. Writer is the **Engine** (under a
    driver command, exactly like ``TaskWoken`` / ``TaskStarted`` — *not* a
    policy ``Decision``). Emitted (a) once at task start = the opening
    binding, and (b) on **each per-turn model switch** so a conversation can
    change models mid-stream (mirroring Claude Code's ``/model``).

    Why a **new event type** and not a field on ``TaskCreatedPayload``:
    adding a field to the genesis event drifts the canonical bytes of
    *every* historical recording (the MS1/SR2 moat). A new type is
    simply *absent* from old recordings → they fold to the local/⊤
    principal with no drift. Per-turn switches also **cannot** live in the
    immutable ``TaskCreated`` — that is precisely why they need their own
    event.

    Both fields are plain strings (canonical-safe, well under the 4-KB
    envelope cap). ``model`` is the bound selector/model id (the same value
    that then drives ``LLMRequestStartedPayload.model``);
    ``principal_identity`` is the authorizing :class:`Principal`'s
    ``identity`` — the durable audit link from a binding back to *who*
    sanctioned it. The full ``Principal`` (with its ``allowed_models`` set)
    is **never** serialized here; validation happened in the driver/server
    *before* this event was emitted.

    (I4): ``provider`` folds the
    session-level provider into this same binding pair — provider and model are
    naturally a pair, chosen and switched together — so we add **no** separate
    ``ProviderBound`` event, just a provider-name field on ModelBound. ``None``
    means an old recording (no provider dimension) or a turn that switched only
    the model: the resolver falls back to the host's default provider. Only the
    **name** is recorded (the instance carries secrets/connections and never
    enters the event log); the ``None`` default lets old recordings deserialize
    unchanged (byte-safe, same rule as adding a new field to AgentBound).
    """

    model: str
    principal_identity: str
    provider: Optional[str] = None


@dataclass(frozen=True, slots=True)
class AgentBoundPayload:
    """The Agent identity bound to a Task, recorded durably.

    Writer is the **Engine**, emitted **once** atomically inside
    ``create_task`` right after ``TaskCreated`` — the Agent is immutable per
    Task (fixed by ``TaskCreated.agent_name``), so unlike ``ModelBound`` this
    never re-emits per turn. It is *not* a policy ``Decision``.

    ``agent_name`` is the durable, self-describing record of which Agent was
    bound (mirroring ``ModelBound`` carrying its own ``model``); the resolver
    binds a resumed task back to its Agent by this name. The earlier
    ``agent_fingerprint`` field was retired along with the test
    infrastructure that consumed it — an old recording that still carries it
    deserializes cleanly via ``restore_dataclass`` (the key is dropped).
    """

    agent_name: str


@dataclass(frozen=True, slots=True)
class TaskHostBoundPayload:
    """The server host identity bound to a Task, recorded durably.

    Writer is the **Engine**, emitted **once** atomically inside ``create_task``
    right after ``AgentBound`` (and before any ``ModelBound``) on the **server
    product creation path** only — the host is fixed for a Task's life, so like
    ``AgentBound`` this never re-emits. It is *not* a policy ``Decision``.

    Why a **new event type**, additive: a per-task event is the bounded unit and
    stays byte-safe — absent from old / non-server recordings → they fold to
    ``None`` with zero drift (same rule as ``AgentBound`` / ``ModelBound``).

    ``host_id`` names the host that bound the task. The earlier
    ``host_config_fingerprint`` / ``registry_fingerprint`` digests were retired
    along with the test infrastructure that consumed them; an old recording that
    still carries them deserializes cleanly via ``restore_dataclass`` (the keys
    are dropped).

    ``workspace_dir`` is the
    per-session workspace **absolute path**, welded as the durable session fs
    root — the load-bearing payload of this event. The agent layer resolves the
    registry once at ``POST /tasks``, expands it to an absolute path, and writes
    it here; the resolver reads this path directly and never touches the registry
    or base pool again. ``None`` on an old / non-session recording → the resolver
    falls back to its host-fixed default workspace dir, byte-equal.
    """

    host_id: str
    workspace_dir: Optional[str] = None

    #: The sandbox execution backend this session is bound to — the per-session
    #: container's ``"{base_url}#{sandbox_id}"`` ref (D4). Welded here so a
    #: resumed / **reclaimed** session (possibly on another host) reconnects to
    #: the SAME container by reading this address rather than the folding host's
    #: own config, which may differ. Addressing only — the API key is NEVER
    #: recorded (D5); it is re-read from the reconnecting host's env at connect
    #: time. ``None`` (every local / non-sandbox recording) → the resolver uses
    #: the local host. The ref is a **flat string** (packed by
    #: ``noeta.client.sandbox_provider.encode_exec_env_ref`` and split on the last
    #: ``#``); an attach-one-container provider mints no ``sandbox_id`` so its ref
    #: is a bare ``base_url``, byte-identical to a v1 recording.
    exec_env_ref: Optional[str] = None

    #: ``workspace_dir`` / ``exec_env_ref`` are OMITTED from the canonical form
    #: when ``None`` so a TaskHostBound written before either field existed keeps
    #: byte-equal canonical bytes — the default never enters the stream
    #: (same idiom as ``Message.origin``). The eventlog restorer
    #: tolerates the key being absent; old "name-style" records that had a
    #: ``workspace`` key (now superseded) simply omit
    #: ``workspace_dir`` → fold to ``None`` → host default.
    __canonical_omit_none__ = frozenset({"workspace_dir", "exec_env_ref"})


@dataclass(frozen=True, slots=True)
class ConversationClosedPayload:
    """A conversation was closed / archived by a human (issue 08).

    Writer is the **Engine** (under the driver's ``close`` command, exactly
    like ``TaskWoken`` / ``ModelBound`` — *not* a policy ``Decision``).
    "Closed" is a lifecycle dimension **orthogonal to** ``task.status``: an
    interactive conversation naturally rests at a trailing next-goal
    ``suspended`` and never ``TaskCompleted``. Closing it must NOT
    manufacture a terminal (that would fake a
    ``Decision``), so this event leaves ``task.status`` =
    ``suspended`` and only folds into ``GovernanceState.closed`` for the
    sessions-list / inspect hot path (read by **fold**, never from an
    Observer — Observers are projections, not state of record).

    Why a **new event type** and not a field on ``TaskCreatedPayload`` (or any
    existing event): adding a field to a historical payload drifts the
    canonical bytes of *every* recording (the MS1/SR2 moat). A new
    type is simply *absent* from old recordings → they fold to ``closed =
    False`` with zero drift. Same byte-safe rule as ``ModelBound`` (issue 06).

    "Closed" is **advisory, not a lock**: a new goal on a closed+suspended
    Task reopens it (the driver may emit the symmetric
    :class:`ConversationReopenedPayload` for audit). Both fields are plain
    strings (canonical-safe, under the 4-KB cap); ``closed_by`` is the
    identity that closed it, ``reason`` an optional human note.
    """

    closed_by: str
    reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ConversationReopenedPayload:
    """A previously-closed conversation was reopened (issue 08).

    The optional audit-symmetric counterpart to
    :class:`ConversationClosedPayload`. Reopen is **advisory**: sending a new
    goal to a closed+suspended Task already works unchanged (the close flag
    never gates the driver), so this event is emitted purely so the lifecycle
    audit trail records the reopen explicitly. Writer is the **Engine**, folds
    into ``GovernanceState.closed = False``, and — like its sibling — leaves
    ``task.status`` untouched. A new type, absent from old recordings → no
    canonical-byte drift.
    """

    reopened_by: str
    reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class TaskCancelledPayload:
    """The Task was cancelled before reaching its own terminal Decision.

    ``cascade=True`` documents that the cancel should propagate to any
    in-flight subtasks (the actual cascade mechanism lands with the
    Worker daemon — Phase 0 just records the intent).
    """

    reason: str
    cascade: bool = False


@dataclass(frozen=True, slots=True)
class LeaseGrantedPayload:
    """A Worker successfully leased a Task.

    Named ``LeaseGranted`` rather than the legacy ``RunLeased``:
    the kernel forbids ``Run``.
    The Worker daemon that emits it does so alongside ``LeaseHeartbeat`` /
    ``LeaseExpired`` / ``TaskRequeued`` for the remaining lifecycle moments.
    """

    lease_id: str
    worker_id: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class SubtaskDeniedPayload:
    """A Guard denied a ``spawn_subtask`` decision.

    The parent Task transitions to terminal/failed; the child is never
    bootstrapped. This is the spawn analogue of ``ToolCallDenied``.
    """

    agent_name: str
    goal: str
    reason: str


@dataclass(frozen=True, slots=True)
class ToolSchemaRecordedPayload:
    """Per-task, per-tool schema-hash provenance.

    Emitted **once** per task, immediately *before* a tool's first
    ``ToolCallStarted``. A new additive event type:
    absent from old recordings → zero canonical drift; adding a field to
    ``ToolCallStarted`` instead would drift every historical envelope.

    ``version`` is the declared ``ToolRef.version`` this task was wired
    against; ``schema_hash`` is ``sha256(canonical_bytes(input_schema))``
    (computed by the caller — the kernel treats both as compare-only
    strings, so noeta-runtime never imports noeta-sdk). The recorded pair is
    passive provenance — it would catch "schema changed but version didn't",
    but the drift-comparison consumer has since been removed.
    """

    tool_name: str
    version: str
    schema_hash: str


@dataclass(frozen=True, slots=True)
class SkillContentRecordedPayload:
    """Per-task, per-skill content-hash provenance.

    Emitted **once** per activated declarative skill, right before the
    durable ``TaskStatePatched(activate_skills=…)`` that flips it on.
    Additive event → old recordings stay byte-identical.

    ``version`` is the declared ``ComponentRef.version`` — the same
    ``[name, version]`` pair the AgentSpec identifies a skill by;
    ``content_hash`` is ``sha256(SKILL.md full bytes)``
    (caller-computed, compare-only string), recorded as passive provenance:
    it captures content-changed-but-version-stayed, which the version pair
    alone cannot.
    """

    skill_name: str
    version: str
    content_hash: str


# -- Background shell events (issue 01) --------------------------
#
# A background ``shell_run`` is a host-layer effect, NOT a subtask: the
# spawned process has no Policy. The off-ledger ``ProcessRegistry``
# (a runtime accelerator, like the cancel registry) is the live state; these
# three events are the durable record. Bytes never inline — stdout/stderr live
# in ContentStore behind the snapshot refs, so each payload stays
# trivially under the 4-KB cap. New event types ⇒ absent from old
# recordings ⇒ zero canonical-byte drift (same rule as ModelBound).


@dataclass(frozen=True, slots=True)
class BackgroundShellStartedPayload:
    """A background process was spawned.

    Emitted on the ``spawned_by_task_id`` stream (issue 01 keys jobs by the
    launching task; issue 04 re-keys lifetime to the session root). ``ref`` is
    the empty content-addressed snapshot minted at spawn — every later
    snapshot grows from it. ``command`` is the raw command string (audit /
    front-end label); ``pid`` is the OS pid for best-effort recovery (issue 06).
    """

    job_id: str
    command: str
    spawned_by_task_id: str
    pid: int
    ref: ContentRef


@dataclass(frozen=True, slots=True)
class BackgroundShellPolledPayload:
    """The model pulled a job's output.

    Records the exact prefix the model saw at that moment: ``ref`` is a fresh
    content-addressed snapshot of the buffer and ``offset`` its byte length.
    Resume reads ``ref`` back and reproduces that prefix verbatim — the
    process's later output never bleeds into a historical poll.

    ``truncated`` tells the model the off-ledger buffer
    overflowed ``output_cap`` and the snapshot is the tail (oldest output
    dropped). ``None`` (the default) is omitted from the canonical bytes
    (``__canonical_omit_none__``) so a poll on an un-truncated job is
    byte-identical to a pre-07 recording; a real overflow records ``True``.
    """

    job_id: str
    ref: ContentRef
    offset: int
    truncated: Optional[bool] = None

    __canonical_omit_none__ = frozenset({"truncated"})


@dataclass(frozen=True, slots=True)
class BackgroundShellExitedPayload:
    """A background process reached terminal — reaped by the watcher (D5).

    ``final_ref`` is the snapshot of the complete (cap-truncated) output;
    ``exit_code`` the process return code (``-1`` on signal/abnormal exit);
    ``summary`` a one-line human/agent description for the front-end and the
    completion wake (issue 02 pushes it, issue 01 only records it).

    ``truncated``: ``True`` when the buffer overflowed
    ``output_cap`` so ``final_ref`` is the tail. Same canonical-omit-when-None
    rule as :class:`BackgroundShellPolledPayload` keeps un-truncated exits
    byte-identical to pre-07 recordings."""

    job_id: str
    exit_code: int
    final_ref: ContentRef
    summary: str
    truncated: Optional[bool] = None

    __canonical_omit_none__ = frozenset({"truncated"})


@dataclass(frozen=True, slots=True)
class BackgroundShellKilledPayload:
    """A background process was killed (issue 03).

    The TERMINAL lifecycle event for a job that ``shell_kill`` (or the human
    emergency-stop) ended — recorded by the watcher's reap path **instead of**
    ``BackgroundShellExited`` (exactly one terminal event per job; the
    ``_JobHandle.notified`` dedup guarantees a kill racing a near-simultaneous
    natural exit still records one terminal event + one push). ``signal`` is
    the POSIX signal that actually reaped the process — ``SIGTERM`` (15) when
    the grace request sufficed, ``SIGKILL`` (9) when the grace elapsed and the
    registry escalated. Like its siblings it rides ``origin="observer"`` — an
    observer-origin event that the fold folds normally, even when it lands while
    the session is suspended."""

    job_id: str
    signal: int


@dataclass(frozen=True, slots=True)
class BackgroundShellLostPayload:
    """A background job was orphaned by a host crash/restart.

    The TERMINAL audit event for a job whose ``BackgroundShellStarted`` has NO
    later terminal (``Exited`` / ``Killed``) on its session-root stream: the
    host crashed/restarted, its in-memory ``ProcessRegistry`` is gone, and the
    OS process was reparented to ``init`` (an orphan). On the next startup
    :meth:`noeta.runtime.background_shell.ProcessRegistry.recover_orphans` scans
    the persisted streams and emits one of these per orphan so the read model
    (and the model) stop showing the job as forever-"running".

    The Lost mark is MANDATORY (it stands regardless of the PID outcome); the
    conservative best-effort PID kill is a separate, irreversible side effect of
    the live recovery scan and is NEVER resumed (the registry is untouched on
    resume — recovery only runs at live startup). The payload carries only the
    ``job_id`` (the durable key the fold handler flips to ``status="lost"``);
    like its siblings it rides ``origin="observer"`` — an observer-origin event
    the fold folds normally, even when it lands while the session is suspended.
    New event type ⇒ absent from old recordings ⇒ zero canonical-byte drift (same
    rule as ModelBound)."""

    job_id: str


# -- background sub-agent lifecycle (docs/adr/background-subagent.md) ---------
# A sub-agent launched with ``spawn_subagent(background=True)``: unlike a
# foreground spawn (``SubtaskSpawned`` + ``TaskSuspended`` on a barrier) the
# parent does NOT suspend, so these two events ARE the durable record. Both ride
# ``origin="observer"`` and fold into the session's append-only
# ``background_subagents`` audit. New event types ⇒ absent from old recordings ⇒
# zero canonical-byte drift (same rule as ModelBound).


@dataclass(frozen=True, slots=True)
class BackgroundSubagentStartedPayload:
    """A sub-agent was launched in the background.

    Emitted on the PARENT (spawning) task's stream the moment
    ``spawn_subagent(background=True)`` is handled. ``subtask_id`` is the child
    task's id — it gets its own stream / ``TaskCreated`` like any subtask, and
    the background-subagent driver drives it to terminal on the shared executor.
    ``agent_name`` / ``goal`` label it for the front-end. ``call_id`` is the
    originating ``spawn_subagent`` tool_use id; the parent already received a
    "started" tool_result for it, so the child's eventual result is delivered via
    a Mechanism-C turn-boundary notice (``BackgroundSubagentDelivered``), never
    this call's result slot.
    """

    subtask_id: str
    agent_name: str
    goal: str
    call_id: str


@dataclass(frozen=True, slots=True)
class BackgroundSubagentDeliveredPayload:
    """A background sub-agent's result was delivered to the parent (Mechanism C).

    Emitted ONCE on the parent stream after the background child reached terminal
    and its result was injected as a turn-boundary notice. This is the
    exactly-once DELIVERY ANCHOR: fold flips the child's audit entry to
    ``status`` and records it delivered, so a resume never re-injects the notice.
    ``result_ref`` is the ContentStore snapshot of the child's final result (the
    model derefs it for the full text); ``summary`` is the one-line description
    carried inline in the notice; ``status`` is the child's terminal disposition
    (``"completed"`` / ``"failed"``).
    """

    subtask_id: str
    result_ref: ContentRef
    summary: str
    status: str


#: Drift policies a ``ContextContentRecorded`` may carry.
#: ``pinned``: content hash changing without a version bump is a hard
#: failure (skills). ``evolving``: the hash is recorded but allowed to
#: drift (memory). The policy travels WITH the recording as passive
#: provenance — the runtime never hard-codes per-kind rules, and the
#: drift-comparison consumer that read it has since been removed.
CONTENT_DRIFT_POLICIES = ("pinned", "evolving")


@dataclass(frozen=True, slots=True)
class ContextContentRecordedPayload:
    """Per-task, per-content-item content-hash provenance (issue 02).

    Generic successor of :class:`SkillContentRecordedPayload`: the same
    ``(name, version, content_hash)`` shape plus ``kind`` (the content
    channel resident's species — ``skill``, ``memory``, …; semantics live
    entirely in the SDK registry) and ``policy`` (one of
    ``CONTENT_DRIFT_POLICIES``, recorded as passive provenance — it states
    how drift would be judged, but the drift-comparison consumer has since
    been removed). Additive event type: absent from old
    recordings → zero canonical drift; the old event stays fold-readable.
    Fold merges ``name`` into the generic activation map
    ``TaskState.active_content[kind]``.
    """

    kind: str
    name: str
    version: str
    content_hash: str
    policy: str


# -- MCP connection lifecycle (issue 03) ----------------------


@dataclass(frozen=True, slots=True)
class McpServerSkippedPayload:
    """An enabled MCP server could not be connected and was SKIPPED (D7).

    Emitted (origin ``observer``) at task-start build time when one enabled MCP
    server fails to connect / handshake / ``tools/list`` — the offending server
    is dropped, its tools never enter the (now frozen) tool set, and the task
    continues with the remaining servers' tools (skip-on-failure, option B). One
    event per skipped server so the front-end / read model can show the user
    exactly which connector failed and why.

    ``alias`` is the server's host-side alias (a clean name, never a url/token —
    credentials never enter any event, D3); ``reason`` is the typed
    ``McpError`` / ``McpConfigError`` message string (a transport / handshake
    fault description, no user content). New event type ⇒ absent from old
    recordings ⇒ zero canonical-byte drift (same rule as ModelBound). The
    recording faithfully captures that this server was skipped this run; R-1
    keeps resume reconnect-free (the recorded tool spec is the durable truth and
    a skipped server simply contributed no tool spec)."""

    alias: str
    reason: str


@dataclass(frozen=True, slots=True)
class McpProvenanceRecordedPayload:
    """The per-task MCP provenance: which connectors + tool subsets, no creds (D11).

    Emitted (origin ``observer``, actor ``mcp``) ONCE at task-start connect time,
    in the pre-loop window (after ``TaskCreated`` / before ``TaskStarted``) so
    the fold rebuilds the
    same ``GovernanceState.mcp_provenance`` — the durable audit answer to "what MCP
    connectors + which of their tools was this task given this run". Only emitted
    when at least one enabled alias resolved to a host-side spec; a task with no
    MCP carries no such event → folds to ``[]`` with zero canonical drift (same
    additive-event rule as ``ModelBound`` / ``McpServerSkipped``).

    ``servers`` is the credential-FREE record from
    :func:`noeta.tools.mcp.mcp_provenance_from_specs` — a list of
    ``{"alias": str, "tools": list[str]}`` dicts, alias-sorted, each ``tools`` the
    ticked raw-name subset (sorted) or ``[]`` for "all advertised". It records
    **only names** — never a url / token / header — so credentials never enter any
    recording (D3). The tools' actual shape / behaviour is NOT carried here; that
    stays R-1's job (the recorded ``request_ref`` tool spec is the durable truth
    a resume reads back).
    Plain JSON lists (not tuples) so the event-log / snapshot round-trip is
    byte-stable. Well under the 4-KB envelope cap (a handful of short names)."""

    servers: list[dict[str, Any]]
