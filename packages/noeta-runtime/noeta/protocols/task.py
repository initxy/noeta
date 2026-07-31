"""Task — the only first-class primitive — and its four state slices.

Each slice has exactly one writer: the Engine writes ``RuntimeState`` and folds
``GovernanceState`` from events, ``TaskState`` changes only through
``TaskStatePatch.apply``, and the Composer owns ``ContextState``. Every field
here is snapshot-serialised, so a new one must be appended LAST with a default
that reproduces the behaviour of a stream that never sets it — otherwise a
rehydrated snapshot stops matching a from-scratch fold byte for byte.
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
    """Engine-owned slice: the rolling LLM message log and usage."""

    messages: list[Message] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    # The most recent non-default continuation tag, projected by fold from the
    # latest ``StepTransitionMarked``. The anti-spiral recovery guards read it
    # in O(1) instead of accreting branch logic inside the Engine body.
    last_transition: Optional[TransitionReason] = None
    # The input-token count the provider reported for the MOST RECENT
    # round-trip, projected by fold from the latest ``LLMRequestFinished``.
    # Distinct from ``GovernanceState.input_tokens``, which accumulates over the
    # whole task: this single last-turn value is what the whole prompt actually
    # cost, so the compaction trigger uses it as a deterministic baseline and
    # adds a chars/4 estimate of only the messages appended since — a pure
    # chars/4 estimate systematically under-counts cache reads, structured
    # blocks and images. Stored as a bare ``int`` because ``Usage`` is an
    # untagged dataclass that would not round-trip typed through the snapshot
    # canonical walker.
    last_input_tokens: int = 0


@dataclass
class TaskState:
    """Policy-owned slice: long-running task memory.

    ``active_content`` is the context content channel's activation map,
    kind → ``{name: content_hash}``. Three fold routes converge on it
    (``ContextContentRecorded``, ``SkillContentRecorded``, and the
    ``activate_skills`` patch sugar) and the hash is **last-write-wins**, so a
    re-record under a new hash reads as a refresh rather than a conflict. What
    a kind means is SDK-registry territory, not the kernel's.
    """

    goal: str = ""
    phase: Optional[str] = None
    todos: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    next_action: Optional[str] = None
    active_skills: list[str] = field(default_factory=list)
    active_content: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Snapshot bodies carry the inner ``{name: hash}`` maps as JSON objects;
        # normalise them back to plain dicts so a rehydrated slice is
        # type-identical with a from-scratch fold.
        if self.active_content:
            self.active_content = {
                kind: dict(name_hashes)
                for kind, name_hashes in self.active_content.items()
            }


@dataclass
class ContextState:
    """Composer-owned slice: the latest ContextPlan ref plus compaction state.

    The summary fields are written only by fold's ``Compacted`` handler: when
    set, the first ``summary_boundary`` messages of the rolling history have
    been collapsed into a single message whose body lives behind
    ``summary_ref``. The Composer swaps that covered prefix for the summary on
    the next compose, leaving ``stable_prefix`` intact.
    """

    plan_ref: Optional[ContentRef] = None
    summary_ref: Optional[ContentRef] = None
    summary_boundary: int = 0
    # Compaction-thrashing detection, written only by fold's ``Compacted``
    # handler so it reconstructs deterministically from the event stream —
    # detection must not hang off a Policy instance's in-memory step counter,
    # which fold cannot see. ``last_compaction_marker`` is the
    # ``GovernanceState.iterations`` value at the previous ``Compacted``;
    # ``close_compaction_run`` counts consecutive compactions whose turn-gap was
    # ``<= _THRASH_CLOSE_TURNS``; ``compaction_thrashing`` latches True once
    # that run reaches ``_THRASH_RUN_LIMIT`` and clears the moment a non-close
    # compaction resets the run.
    last_compaction_marker: Optional[int] = None
    close_compaction_run: int = 0
    compaction_thrashing: bool = False
    # ThinkingBlocks stripped from the persisted assistant turn, keyed by that
    # turn's FIRST ``tool_use`` ``call_id`` (its stable per-turn identity) and
    # written only by fold's ``AssistantThinkingRecorded`` handler. Thinking
    # must never enter ``runtime.messages``: its signature is non-deterministic
    # across live runs and would perturb the stable prompt prefix. Holding it
    # outside the persisted message stream still lets the Composer re-attach it
    # ahead of the tool_use in the transient View, so an Anthropic continuation
    # replays the signature verbatim.
    thinking_by_call_id: dict[str, list[ThinkingBlock]] = field(
        default_factory=dict
    )
    # ``"<kind>:<name>" → rolling-history length at activation``, written only
    # by fold's activation handlers, first-write-wins. The Composer reads it to
    # place a resident: an anchor at or before the first assistant message keeps
    # it in ``semi_stable``; a later anchor renders it inside the dynamic suffix
    # at that point. See docs/adr/anchored-content-placement.md.
    content_anchors: dict[str, int] = field(default_factory=dict)


@dataclass
class GovernanceState:
    """Engine-folded slice: cost, counters, denied actions, and provenance.

    Everything here is derived from the event stream, never patched: derived
    data such as ``subtask_results`` deliberately stays out of ``TaskState``.
    ``denied`` collects the three deny events (``ToolCallDenied`` /
    ``SubtaskDenied`` / ``TaskCancelled``); a denied finish is not one of them
    and materialises as ``TaskFailed`` only.
    """

    cost_usd: float = 0.0
    tool_calls: int = 0
    iterations: int = 0
    spawned_subtasks: int = 0
    # Per-token accounting folded from ``LLMRequestFinished.usage``, kept at the
    # finest granularity so pricing can apply distinct cache-read / cache-write
    # unit prices later without re-touching this slice and its snapshot bytes.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    denied: list[dict[str, Any]] = field(default_factory=list)
    subtask_results: list[SubtaskResult] = field(default_factory=list)
    # ``pending_approvals`` is keyed by ``call_id`` and holds the blocked call's
    # ``{tool_name, arguments}`` between ``ToolCallApprovalRequested`` (inserts)
    # and ``ToolCallApprovalResolved`` (deletes). It is the durable recovery
    # anchor: without it the approval-resume path could not reconstruct the
    # exact call after a restart. ``approvals`` is the append-only audit of
    # every resolution.
    pending_approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    # Structured ask-user-question state. Pending entries are keyed by
    # ``question_id`` and hold only refs / counts / small metadata — full bodies
    # stay in the ContentStore so durable state never grows with them.
    pending_questions: dict[str, dict[str, Any]] = field(default_factory=dict)
    question_answers: list[dict[str, Any]] = field(default_factory=list)
    # The current model binding, folded from the latest ``ModelBound``.
    # ``model_binding`` is the selector / model id the resolver keys the Engine
    # on; ``principal_identity`` is the authorizing Principal, i.e. the audit
    # link from a binding to whoever sanctioned it; ``model_bindings`` is the
    # append-only audit of every binding and switch. ``None`` leaves the
    # resolver on its host-fixed default model.
    model_binding: Optional[str] = None
    principal_identity: Optional[str] = None
    model_bindings: list[dict[str, Any]] = field(default_factory=list)
    # The provider name folded onto that same ``ModelBound`` binding — provider
    # binding deliberately rides the model-binding route rather than owning a
    # separate event. The resolver looks up ``providers[name]`` and adds it as a
    # dimension of the engine cache key; ``None`` keeps the host default.
    provider_binding: Optional[str] = None
    # The server host identity bound at task open, folded from the single
    # ``TaskHostBound`` event.
    host_id: Optional[str] = None
    # The per-session workspace **absolute path**, folded from the same
    # ``TaskHostBound`` event and passed straight through as the session's fs
    # root, so a resume reproduces the identical root dir instead of
    # re-deriving it from whatever host happens to be folding.
    workspace: Optional[str] = None
    # The sandbox container ``base_url`` this session is bound to. A resumed or
    # reclaimed session — possibly on another host — must reconnect to the SAME
    # container by this address rather than trust the folding host's own config.
    exec_env_ref: Optional[str] = None
    # The conversation close/archive lifecycle, folded from
    # ``ConversationClosed`` / ``ConversationReopened``. ``closed`` is
    # **orthogonal** to ``Task.status``: a closed conversation stays
    # ``suspended`` and no terminal is synthesised. Listing and inspect read the
    # flag through fold, never from an Observer, so the answer stays derivable
    # from the stream alone. ``conversation_lifecycle`` is the append-only audit.
    closed: bool = False
    closed_by: Optional[str] = None
    close_reason: Optional[str] = None
    conversation_lifecycle: list[dict[str, Any]] = field(default_factory=list)
    # Per-tool provenance folded from the single ``ToolSchemaRecorded`` emitted
    # before a tool's first call: tool_name → sha256(canonical input_schema) and
    # tool_name → declared ``ToolRef.version``. Keeping both lets drift
    # diagnostics tell "schema changed but version didn't" from a normal bump.
    tool_schema_hashes: dict[str, str] = field(default_factory=dict)
    tool_schema_versions: dict[str, str] = field(default_factory=dict)
    # Per-skill provenance folded from the single ``SkillContentRecorded`` per
    # activated skill: skill_name → sha256(SKILL.md bytes) / declared
    # ``ComponentRef.version``.
    skill_content_hashes: dict[str, str] = field(default_factory=dict)
    skill_content_versions: dict[str, str] = field(default_factory=dict)
    # The session's background-shell jobs, folded from the ``BackgroundShell*``
    # events. Those are emitted on the ROOT-TASK stream, so folding the root
    # surfaces subtask-spawned jobs too. Append-only: an entry is appended on
    # ``Started`` and only ever UPDATED — never removed — on poll / exit / kill,
    # so the trail survives for inspect. Each entry::
    #
    #     {job_id, command, status: "running"|"exited"|"killed",
    #      spawned_by_task_id, exit_code?, signal?, ref?}
    #
    # ``ref`` is the latest known snapshot ContentRef of the job's output.
    background_jobs: list[dict[str, Any]] = field(default_factory=list)
    # Per-task MCP provenance folded from ``McpProvenanceRecorded``: an
    # alias-sorted list of ``{"alias", "tools"}`` dicts, each ``tools`` the
    # ticked raw-name subset or ``[]`` for all. Names ONLY — never a url, token
    # or header, which stay host-side — so credentials cannot leak into durable
    # state. This answers "which connectors and which of their tools was the
    # task given"; the tools' actual shape is the recorded request spec's job.
    mcp_provenance: list[dict[str, Any]] = field(default_factory=list)
    # Append-only audit of sub-agents launched with
    # ``spawn_subagent(background=True)``. Each entry::
    #
    #     {subtask_id, agent_name, goal, status: "running"|"completed"|"failed",
    #      call_id, result_ref?, summary?}
    #
    # ``BackgroundSubagentStarted`` appends a ``running`` entry;
    # ``BackgroundSubagentDelivered`` flips it to the child's terminal status.
    # That second event doubles as the exactly-once delivery anchor — fold reads
    # it so a resume never re-injects the turn-boundary notice.
    # See docs/adr/background-subagent.md.
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
    #: Delegation depth (root=0, child=parent+1), set from the genesis
    #: ``TaskCreated.subtask_depth`` and immutable for the task's life.
    subtask_depth: int = 0
    runtime: RuntimeState = field(default_factory=RuntimeState)
    state: TaskState = field(default_factory=TaskState)
    context: ContextState = field(default_factory=ContextState)
    governance: GovernanceState = field(default_factory=GovernanceState)
    # The WakeCondition that resumes the task while it is suspended.
    wake_on: Any = None

    def state_dict(self) -> dict[str, Any]:
        """Serialize the four slices plus status as the Snapshot body.

        Each slice makes a round trip through ``to_canonical`` and back through
        ``from_canonical`` so tagged value types keep their tag and the result
        matches the typed shape ``deserialize_task_state`` yields. That symmetry
        between writer and reader is what keeps a snapshot-accelerated fold
        byte-equal with a from-scratch one.
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
