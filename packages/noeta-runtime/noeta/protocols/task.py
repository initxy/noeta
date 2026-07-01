"""Task and its four state slices.

A Task is the only first-class primitive in Noeta. Its mutable
state is split into four typed slices, each with a single writer:

* ``RuntimeState``   — Engine writes
* ``TaskState``      — mutated only by ``TaskStatePatch.apply`` (called
                       by Engine + fold). The patch author is normally
                       a Policy via ``Decision.state_patch``; Phase 4
                       (B17) adds one narrow operator-driven seam,
                       :meth:`noeta.core.engine.Engine.apply_state_patch`,
                       used by the Noeta-Code runner for pre-loop skill
                       activation. Both paths emit the same
                       ``TaskStatePatched`` event and call the same
                       ``apply`` — single-writer is preserved. Fold
                       also adds content-record handlers
                       (``SkillContentRecorded`` /
                       ``ContextContentRecorded`` merge into
                       ``active_content``) as the second fold-owned
                       write route into this slice.
* ``ContextState``   — Composer writes
* ``GovernanceState``— Engine writes (folded from events)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from noeta.protocols.canonical import from_canonical, to_canonical
from noeta.protocols.messages import Message, ThinkingBlock
from noeta.protocols.step_transition import TransitionReason
from noeta.protocols.values import ContentRef
from noeta.protocols.wake import SubtaskResult


TASK_STATUSES = ("pending", "running", "suspended", "terminal")


@dataclass
class RuntimeState:
    """Engine-owned slice. Holds the rolling LLM message log and usage.

    Phase 1 ``messages`` is a list of :class:`Message` (typed Block
    content); Phase 0 carried plain dicts (per the docstring of that
    era which previewed the upgrade).
    """

    messages: list[Message] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    # Foundation B (D-B3) — the most recent non-default continuation tag, projected
    # by fold from the latest ``StepTransitionMarked`` event. The anti-spiral
    # recovery guards (② / ④) read this O(1) instead of piling logic into the
    # Engine body. ``None`` on a fresh task and on any old recording (no such
    # event) → snapshot ``state_dict`` byte-equal, no drift. Appended LAST so
    # an old snapshot dict (missing the key) rebuilds via this default (the
    # 'optional + last' byte-safe convention).
    last_transition: Optional[TransitionReason] = None
    # The REAL input-token usage the provider reported for the
    # MOST RECENT LLM round-trip (``Usage.input`` = uncached+cache_read+
    # cache_write), projected by fold from the latest ``LLMRequestFinished``.
    # Distinct from ``GovernanceState.input_tokens`` (which ACCUMULATES across
    # the whole task for cost accounting) — this is the SINGLE last-turn value,
    # the precise size the whole prompt cost last time. The compaction trigger
    # reads it as the deterministic history baseline (instead of a pure
    # chars/4 estimate that systematically under-counts cache / structured
    # blocks / images), then adds a chars/4 estimate of only the messages
    # APPENDED since. We store the bare ``int`` (not a ``Usage`` — that is an
    # untagged dataclass that would not round-trip typed through the snapshot
    # canonical walker) so the field is byte-safe by construction. ``0`` on a
    # fresh task and on any old recording (LLMRequestFinished without usage)
    # → snapshot ``state_dict`` byte-equal; appended LAST so an old snapshot
    # dict (missing the key) rebuilds via this default (the 'optional + last'
    # byte-safe convention).
    last_input_tokens: int = 0


@dataclass
class TaskState:
    """Policy-owned slice. Holds long-running task memory.

    Phase 0 keeps the shape minimal; richer fields land with the
    typed ``TaskStatePatch`` in issue 02+.

    ``active_content`` is the generic activation map of the
    context content channel: kind → resident name tuple. Three fold
    routes converge here — the old ``SkillContentRecorded`` event, the
    ``activate_skills`` patch sugar (both skill-specific, retained
    read-only), and the generic ``ContextContentRecorded`` event. The
    runtime stores names only; what a kind means is SDK-registry
    territory. Appended LAST with an empty default so an old snapshot
    dict (missing the key) rebuilds via this default (the
    'optional + last' byte-safe convention).
    """

    goal: str = ""
    phase: Optional[str] = None
    todos: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    next_action: Optional[str] = None
    active_skills: list[str] = field(default_factory=list)
    active_content: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Snapshot bodies serialise the name tuples as JSON lists;
        # normalise back so a rehydrated slice is type-identical with a
        # from-scratch fold.
        if self.active_content:
            self.active_content = {
                kind: tuple(names)
                for kind, names in self.active_content.items()
            }


@dataclass
class ContextState:
    """Composer-owned slice. Phase 0 holds the most recent ContextPlan ref.

    ③ (D-3) adds a compaction summary slice, written ONLY by fold's
    ``Compacted`` handler (single writer): when set, the first
    ``summary_boundary`` messages of the rolling history have been
    collapsed into a single summary message whose body lives behind
    ``summary_ref``. The Composer reads this slice to swap the covered
    prefix for the summary on the next compose, keeping ``stable_prefix``
    intact. Both default to "no summary yet" so an old snapshot (missing
    the keys) rebuilds via these defaults — the 'optional + last'
    byte-safe convention; an old recording with no ``Compacted`` event
    folds to the same defaults → snapshot ``state_dict`` byte-equal.
    """

    plan_ref: Optional[ContentRef] = None
    summary_ref: Optional[ContentRef] = None
    summary_boundary: int = 0
    # ⑥ compaction thrashing detection (D6.2).
    # Written ONLY by fold's ``Compacted`` handler (single writer) and
    # reconstructed deterministically from the ``Compacted`` event stream, so
    # detection never depends on react.py's in-memory ``_step_count`` (a Policy
    # instance attribute fold cannot see). ``last_compaction_marker`` is the
    # ``GovernanceState.iterations`` value (the per-compose turn counter)
    # recorded at the previous ``Compacted``; ``close_compaction_run`` is the
    # count of consecutive compactions whose turn-gap to the previous one was
    # ``<= _THRASH_CLOSE_TURNS``; ``compaction_thrashing`` latches True once that
    # run reaches ``_THRASH_RUN_LIMIT`` and clears the moment a non-close
    # compaction resets the run. All default to "no compaction yet" so an old
    # recording (no ``Compacted`` event) folds to the same defaults → snapshot
    # ``state_dict`` byte-equal, no drift.
    last_compaction_marker: Optional[int] = None
    close_compaction_run: int = 0
    compaction_thrashing: bool = False
    # Extended-thinking end-to-end (Slice B/C): the ThinkingBlocks that
    # ``react._strip_thinking`` removed from the persisted assistant turn,
    # keyed by that turn's FIRST ``tool_use`` ``call_id`` (the stable
    # per-turn identity). Written ONLY by fold's
    # ``AssistantThinkingRecorded`` handler (single writer); the
    # Composer reads it to re-attach the thinking ahead of the tool_use on
    # the next compose, so an Anthropic continuation request replays the
    # signature verbatim. Thinking deliberately never enters
    # ``runtime.messages`` (its signature is non-deterministic across live
    # runs and would perturb the stable prompt prefix); it lives here, outside
    # the persisted message stream, and is re-attached only into the transient
    # View. Defaults empty so an OpenAI / non-reasoning / old recording (no
    # such event) folds to the same empty dict → snapshot ``state_dict``
    # byte-equal, no drift.
    thinking_by_call_id: dict[str, list[ThinkingBlock]] = field(
        default_factory=dict
    )


@dataclass
class GovernanceState:
    """Engine-folded slice. Cost, iteration counts, denied actions, and
    the running list of subtask outcomes.

    Real accounting for the numeric fields lands in issue 18 alongside
    the built-in BudgetGuard: fold accumulates ``iterations`` from
    ``ContextPlanComposed``, ``tool_calls`` from ``ToolCallStarted``,
    ``spawned_subtasks`` from ``SubtaskSpawned``, and ``cost_usd`` from
    ``LLMRequestFinished.cost_usd``. ``denied`` collects records from
    the three deny event types (``ToolCallDenied / SubtaskDenied /
    TaskCancelled``); ``finish-denied`` does not enter this list —
    it materialises as a ``TaskFailed`` event only. ``subtask_results``
    is filled by fold from ``SubtaskCompleted`` events on the parent
    stream — per CONTEXT.md, "derived data (subtask_results / ...) does not go
    into TaskState; it is folded from the EventLog and kept in GovernanceState".
    """

    cost_usd: float = 0.0
    tool_calls: int = 0
    iterations: int = 0
    spawned_subtasks: int = 0
    # Foundation A (D-A3) — per-token accounting folded from
    # ``LLMRequestFinished.usage``, at the finest granularity so ① pricing
    # can apply distinct cache-read / cache-write unit prices without
    # re-touching this slice (and its snapshot bytes) later. All default 0:
    # an old recording (LLMRequestFinished without a ``usage`` field) folds
    # to 0 → snapshot state_dict byte-equal, no drift.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    denied: list[dict[str, Any]] = field(default_factory=list)
    subtask_results: list[SubtaskResult] = field(default_factory=list)
    # Phase 4.5 Issue A — human-in-the-loop tool-call approval.
    # ``pending_approvals`` is keyed by ``call_id`` and holds the
    # blocked call's ``{tool_name, arguments}`` between
    # ``ToolCallApprovalRequested`` (inserts) and
    # ``ToolCallApprovalResolved`` (deletes) — the durable recovery
    # anchor that lets the approval-resume path reconstruct the exact
    # call from the EventLog/snapshot after a restart. ``approvals`` is
    # the append-only audit of every resolution.
    pending_approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    # CW18d — durable structured ask-user-question HITL. Pending entries are
    # keyed by question_id and hold only refs/counts/small metadata; full bodies
    # are ContentStore blobs. Answers is append-only audit.
    pending_questions: dict[str, dict[str, Any]] = field(default_factory=dict)
    question_answers: list[dict[str, Any]] = field(default_factory=list)
    # Issue 06 — the current model binding,
    # folded from the latest ``ModelBound`` event. ``model_binding`` is the
    # bound selector/model id the resolver keys the Engine on
    # ``(agent_name, model)``; ``principal_identity`` is the authorizing
    # Principal's identity (the audit link from binding → who sanctioned
    # it); ``model_bindings`` is the append-only audit of every binding +
    # switch. All ``None`` / empty on an old recording (no ``ModelBound``)
    # → the resolver falls back to its host-fixed default model, byte-equal.
    model_binding: Optional[str] = None
    principal_identity: Optional[str] = None
    model_bindings: list[dict[str, Any]] = field(default_factory=list)
    # (I4) — the provider name folded onto the same
    # ModelBound binding (``None`` on an old recording / a pure model switch
    # means keep the host default provider). The resolver looks up
    # ``providers[name]`` for the instance and adds it as the fifth dimension of
    # the engine cache key. Provider binding reuses the ModelBound route (no
    # separate ProviderBound event).
    provider_binding: Optional[str] = None
    # The server host identity bound at task open, folded from the
    # single ``TaskHostBound`` event (server product path only). ``None`` on a
    # non-server / old recording. The earlier ``host_config_fingerprint`` /
    # ``registry_fingerprint`` / ``agent_fingerprint`` digests were retired along
    # with the test infrastructure that consumed them (an old snapshot still
    # carrying them rehydrates via ``restore_dataclass``, which drops the keys).
    host_id: Optional[str] = None
    # The per-session workspace **absolute path** welded
    # into durable state, folded from the single ``TaskHostBound`` event
    # (``workspace_dir`` field). The resolver passes this directly as the
    # session's fs root (a resume reproduces the same root dir from here).
    # ``None`` on an old / non-session recording → the resolver falls back to
    # its host-fixed default workspace dir, byte-equal.
    # Legacy "name-style" TaskHostBound records fold to None (D7 break).
    workspace: Optional[str] = None
    # Issue 08 — the conversation
    # close/archive lifecycle, folded from ``ConversationClosed`` /
    # ``ConversationReopened`` events. ``closed`` is the queryable flag the
    # sessions-list / inspect hot path reads (via fold, NEVER from an
    # Observer); it is **orthogonal** to ``Task.status`` — a closed
    # conversation stays ``suspended`` (no synthesized terminal). ``closed_by``
    # / ``close_reason`` carry the latest close's attribution for inspect;
    # ``conversation_lifecycle`` is the append-only audit of every
    # close/reopen. All default to "not closed" on an old recording (no such
    # event) → byte-equal, no drift.
    closed: bool = False
    closed_by: Optional[str] = None
    close_reason: Optional[str] = None
    conversation_lifecycle: list[dict[str, Any]] = field(default_factory=list)
    # Per-tool schema-hash provenance, folded from
    # the single ``ToolSchemaRecorded`` per tool emitted right before its
    # first ``ToolCallStarted``. tool_name → sha256(canonical input_schema)
    # and tool_name → declared ``ToolRef.version`` so drift diagnostics can
    # tell "schema changed but version didn't" apart from a normal bump.
    # Empty defaults on an old recording (no such event) → byte-equal.
    tool_schema_hashes: dict[str, str] = field(default_factory=dict)
    tool_schema_versions: dict[str, str] = field(default_factory=dict)
    # Per-skill content-hash provenance, folded from
    # the single ``SkillContentRecorded`` per activated skill. skill_name →
    # sha256(SKILL.md bytes) / declared ``ComponentRef.version``.
    skill_content_hashes: dict[str, str] = field(default_factory=dict)
    skill_content_versions: dict[str, str] = field(default_factory=dict)
    # (issue 05) — the session's background-shell jobs, folded from
    # the ``BackgroundShell*`` observer events (which issue 04 emits on the
    # SESSION ROOT stream, so folding the root surfaces every job incl. the
    # subtask-spawned ones). Append-only audit, mirror of ``subtask_results``:
    # a job is APPENDED on ``Started`` and its entry only UPDATED (never
    # removed) on poll / exit / kill so the trail survives for inspect + the
    # front-end drill-in. Each entry::
    #
    #     {job_id, command, status: "running"|"exited"|"killed",
    #      spawned_by_task_id, exit_code?, signal?, ref?}
    #
    # ``ref`` is the latest known snapshot ContentRef (the front-end derefs it
    # for the freshest recorded output). Empty default so an old recording (no
    # such event) folds to ``[]`` → snapshot ``state_dict`` byte-equal; appended
    # LAST so an old snapshot dict (missing the key) rebuilds via this default
    # (the 'optional + last' byte-safe convention).
    background_jobs: list[dict[str, Any]] = field(default_factory=list)
    # (issue 07) — the per-task MCP provenance, folded from the
    # single ``McpProvenanceRecorded`` event emitted at connect time. A
    # credential-FREE list of ``{"alias", "tools"}`` dicts (alias-sorted, each
    # ``tools`` the ticked raw-name subset or ``[]`` for all): the durable audit
    # answer to "which MCP connectors + which of their tools was this task given
    # this run". Names ONLY — never a url / token / header (those stay host-side,
    # D3) — so credentials never enter the durable state. The tools' actual
    # shape / behaviour is R-1's job (the recorded ``request_ref`` spec is the
    # durable truth a resume reads back), NOT this provenance. Empty default so a
    # task with no MCP (or an
    # old recording with no such event) folds to ``[]`` → snapshot ``state_dict``
    # byte-equal; appended LAST so an old snapshot dict (missing the key) rebuilds
    # via this default (the 'optional + last' byte-safe convention).
    mcp_provenance: list[dict[str, Any]] = field(default_factory=list)
    # background sub-agents (docs/adr/background-subagent.md) — the session's
    # append-only audit of sub-agents launched with
    # ``spawn_subagent(background=True)``. Each entry:
    #     {subtask_id, agent_name, goal, status: "running"|"completed"|"failed",
    #      call_id, result_ref?, summary?}
    #
    # ``BackgroundSubagentStarted`` appends a ``running`` entry;
    # ``BackgroundSubagentDelivered`` flips it to the child's terminal ``status``
    # (and records ``result_ref`` + ``summary``) — the latter doubling as the
    # exactly-once delivery anchor (fold reads it so a resume never re-injects the
    # turn-boundary notice). Empty default so an old recording (no such event)
    # folds to ``[]`` → snapshot ``state_dict`` byte-equal; appended LAST so an
    # old snapshot dict (missing the key) rebuilds via this default (the
    # 'optional + last' byte-safe convention).
    background_subagents: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Task:
    """A single agent execution instance.

    The Task object is itself simple. The interesting properties — its
    history, audit trail, snapshots — live in the EventLog and ContentStore.
    """

    task_id: str
    status: str = "pending"
    parent_task_id: Optional[str] = None
    #: SR1 — delegation depth (root=0, child=parent+1). Set from the genesis
    #: ``TaskCreated.subtask_depth`` at fold/bootstrap; carried through the
    #: snapshot body. Immutable for the task's life.
    subtask_depth: int = 0
    runtime: RuntimeState = field(default_factory=RuntimeState)
    state: TaskState = field(default_factory=TaskState)
    context: ContextState = field(default_factory=ContextState)
    governance: GovernanceState = field(default_factory=GovernanceState)
    # When suspended, what wakes the task back up.
    wake_on: Any = None

    def state_dict(self) -> dict[str, Any]:
        """Return a canonical-dict serialization of the 4 slices + status.

        Used as the Snapshot body. Walks each slice through
        :func:`noeta.protocols.canonical.to_canonical` so tagged value
        types (ContentRef, WakeCondition variants, SubtaskResult,
        Message + Block) keep their tag, then restores the tagged
        values back to typed instances via
        :func:`noeta.protocols.canonical.from_canonical` so the result
        matches the typed shape ``deserialize_task_state`` would yield —
        the snapshot deserializer is the consumer of this dict and
        symmetry between the two paths is what keeps the snapshot-
        accelerated fold byte-equal with the from-scratch fold.
        """
        return {
            "task_id": self.task_id,
            "status": self.status,
            "parent_task_id": self.parent_task_id,
            "subtask_depth": self.subtask_depth,
            "runtime": from_canonical(to_canonical(self.runtime)),
            "state": from_canonical(to_canonical(self.state)),
            "context": from_canonical(to_canonical(self.context)),
            "governance": from_canonical(to_canonical(self.governance)),
            "wake_on": from_canonical(to_canonical(self.wake_on)),
        }
