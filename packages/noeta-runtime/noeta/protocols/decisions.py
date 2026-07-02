"""Decision: the Policy's output, the Engine's input.

Per CONTEXT.md, there are exactly 7 *neutral* Decision
kinds in the runtime — ``tool_calls`` / ``spawn_subtask`` /
``yield_for_human`` / ``wait_timer`` / ``wait_external`` / ``finish`` /
``fail``. Two members generalize the canonical set without adding any
product meaning:

* :class:`SpawnSubtasksDecision` — the fan-out of
  ``spawn_subtask`` (N children in one turn).
* :class:`StatePatchDecision` — the loop-continuing **state-write**
  member of the ``tool_calls`` family: a control-tool call that mutates
  kernel state (via a typed :class:`TaskStatePatch`) and bookkeeps the
  conversation (caller-built messages) instead of invoking a ToolRuntime
  tool, then loop-continues (no suspend, no terminal). The Engine assigns
  ZERO meaning to its payload: the Policy authors every
  message and every patch field.

Each variant carries a typed ``TaskStatePatch`` so policies that need
to update the long-running task memory do so through a single
single-writer channel.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING, Any, Optional, Union

from noeta.protocols.messages import Message, ThinkingBlock
from noeta.protocols.values import ContentRef

if TYPE_CHECKING:
    from noeta.protocols.task import TaskState


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single tool invocation request inside a ``tool_calls`` decision."""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str


# --- TaskStatePatch -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskStatePatch:
    """Typed patch applied to ``TaskState``; the only shape that mutates it.

    PRD §"protocol shape" pins the field set so callers cannot smuggle arbitrary
    keys into ``TaskState``. The single-writer invariant is
    expressed as *one* patch shape + *one* ``apply`` method; the patch
    *author* may be:

    * a Policy via ``Decision.state_patch`` — the main path used by
      ReAct / scripted policies.
    * the narrow operator-driven seam
      :meth:`noeta.core.engine.Engine.apply_state_patch` (Phase 4 B17),
      used by the Noeta-Code runner to deterministically activate skills
      before the first compose.

    Both paths emit ``TaskStatePatched`` and call ``apply``; fold and
    resume handle them identically.

    Phase 0 exercises ``set_goal`` directly; the rest are reserved for
    Phase 1+ Policies. The skill fields are defined here so Phase 1
    Skill activation + Phase 4 runner-driven activation can ship
    without a protocol bump.

    Field semantics applied by :meth:`apply`:

    * ``set_goal`` / ``set_phase`` / ``set_next_action`` — assign when
      non-``None``; otherwise leave the slice field unchanged.
    * ``add_todos`` / ``add_decisions`` — extend the slice list.
    * ``complete_todos`` — for each id, mark the matching
      ``state.todos[i]`` dict with ``completed=True``. Unknown ids are
      ignored.
    * ``set_todos`` — **replace-all** the checklist: ``None`` leaves it
      unchanged, ``[]`` clears it, a list replaces it wholesale. Distinct
      from ``add_todos`` (append). A neutral patch field the SDK drives
      (e.g. a ``todo_write`` control tool emits it via a
      :class:`StatePatchDecision`). Absent from any older recording →
      ``from_dict`` defaults it to ``None`` → no drift (byte-safe optional
      field, like the skill fields).
    * ``activate_skills`` — union with ``state.active_skills`` (order
      preserved, no duplicates).
    * ``deactivate_skills`` — drop matching entries.

    Both skill fields are the skill-specific sugar of the generic content
    channel: after applying, ``state.active_content["skill"]``
    mirrors ``state.active_skills`` so the old patch shape folds into the
    same generic activation map the ``ContextContentRecorded`` event feeds.
    """

    set_goal: Optional[str] = None
    set_phase: Optional[str] = None
    add_todos: list[dict[str, Any]] = field(default_factory=list)
    complete_todos: list[str] = field(default_factory=list)
    add_decisions: list[dict[str, Any]] = field(default_factory=list)
    set_next_action: Optional[str] = None
    activate_skills: list[str] = field(default_factory=list)
    deactivate_skills: list[str] = field(default_factory=list)
    # Replace-all checklist. Optional + last so old recordings (no such
    # key) fold to None via from_dict → byte-safe.
    set_todos: Optional[list[dict[str, Any]]] = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical dict form used as ``TaskStatePatchedPayload.patch``.

        CW18b byte-safety: ``set_todos`` is OMITTED when ``None`` (its
        back-compat default) so a non-``todo_write`` patch (e.g. skill
        activation) serializes **byte-identically to pre-CW18b** — a recording
        made before the field existed has no ``set_todos`` key, and a refold of
        that recording must reproduce the same bytes. ``[]`` (clear) and a
        populated list ARE kept (those only occur in new ``todo_write``
        recordings)."""
        data = asdict(self)
        if self.set_todos is None:
            data.pop("set_todos", None)
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskStatePatch":
        """Inverse of :meth:`to_dict`; tolerates partial payloads.

        Unknown keys raise ``KeyError`` so a stale producer cannot
        silently inject fields the slice does not understand.
        """
        known = {f.name for f in fields(cls)}
        extra = set(raw) - known
        if extra:
            raise KeyError(
                f"unknown TaskStatePatch field(s): {sorted(extra)}"
            )
        return cls(**{k: v for k, v in raw.items() if k in known})

    def apply(self, state: "TaskState") -> None:
        """Mutate ``state`` in-place per this patch.

        The only writer of ``TaskState``; both the live
        Engine path and the fold-resume path call into here so the
        semantics live in exactly one place.
        """
        if self.set_goal is not None:
            state.goal = self.set_goal
        if self.set_phase is not None:
            state.phase = self.set_phase
        if self.set_todos is not None:
            # Replace-all (CW18b). `[]` clears; `None` (above) is "no change".
            state.todos[:] = [dict(t) for t in self.set_todos]
        if self.add_todos:
            state.todos.extend(dict(t) for t in self.add_todos)
        if self.complete_todos:
            ids = set(self.complete_todos)
            for todo in state.todos:
                if todo.get("id") in ids:
                    todo["completed"] = True
        if self.add_decisions:
            state.decisions.extend(dict(d) for d in self.add_decisions)
        if self.set_next_action is not None:
            state.next_action = self.set_next_action
        if self.activate_skills:
            seen = set(state.active_skills)
            for skill in self.activate_skills:
                if skill not in seen:
                    state.active_skills.append(skill)
                    seen.add(skill)
        if self.deactivate_skills:
            drop = set(self.deactivate_skills)
            state.active_skills[:] = [
                s for s in state.active_skills if s not in drop
            ]
        if self.activate_skills or self.deactivate_skills:
            # The skill sugar mirrors into the generic
            # activation map — kind "skill" stays in lockstep with
            # ``active_skills`` (empty ⇒ key absent).
            if state.active_skills:
                state.active_content["skill"] = tuple(state.active_skills)
            else:
                state.active_content.pop("skill", None)


# --- Decision variants -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinishDecision:
    answer: Any
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None


@dataclass(frozen=True, slots=True)
class FailDecision:
    reason: str
    retryable: bool = False
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None


@dataclass(frozen=True, slots=True)
class ToolCallsDecision:
    calls: list[ToolCall]
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None
    #: Extended-thinking end-to-end (Slice B): the ThinkingBlocks the LLM
    #: emitted ahead of this turn's ``tool_use``, carried OUT-OF-BAND from
    #: ``assistant_message`` (which stays thinking-free so ``runtime.messages``
    #: never absorbs a non-deterministic signature). The Engine records them
    #: into ``ContextState.thinking_by_call_id`` via ``AssistantThinkingRecorded``
    #: so the next compose can replay the signature on an Anthropic
    #: continuation request. Empty for non-reasoning models.
    assistant_thinking: tuple[ThinkingBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class SpawnSubtaskDecision:
    agent_name: str
    goal: str
    inputs: dict[str, Any] = field(default_factory=dict)
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None
    #: Extended-thinking end-to-end (Slice B): the ThinkingBlocks the LLM
    #: emitted ahead of this turn's ``spawn_subagent`` tool_use. Mirrors
    #: :attr:`ToolCallsDecision.assistant_thinking`; empty for non-reasoning
    #: models.
    assistant_thinking: tuple[ThinkingBlock, ...] = ()
    #: background sub-agent (see docs/adr/background-subagent.md): when ``True``
    #: the parent does NOT suspend on a barrier — the Engine emits a
    #: ``BackgroundSubagentStarted``, returns a "started" tool_result so the
    #: parent's turn continues, and hands the child to the background-subagent
    #: driver (executor-driven, delivered at a turn boundary via Mechanism C).
    #: Transient Policy→Engine carrier only; it is NOT persisted on this decision
    #: (decisions are not events) — the durable trace is the ``BackgroundSubagent*``
    #: events. ``False`` (default) = the legacy blocking spawn that suspends on
    #: ``SubtaskCompleted``. A background spawn is always a SINGLE child (the
    #: ``SpawnSubtasksDecision`` fan-out group stays foreground-only).
    background: bool = False


@dataclass(frozen=True, slots=True)
class SpawnSubtaskSpec:
    """SR2 — one member of a fan-out batch. ``call_id`` is the originating
    ``spawn_subagent`` tool_use id (used at resume to pair the member's
    result back to the model's call, **positionally** from the assistant
    message — it is NOT persisted on any event payload)."""

    agent_name: str
    goal: str
    call_id: str
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SpawnSubtasksDecision:
    """SR2 — fan out N sub-agents in one turn and suspend on an all-of
    join. ``specs`` order **is** member (spawn) order: it fixes
    the group's ``subtask_ids`` order and the parent-facing result order.
    Produced by ``ReActPolicy`` only for **≥2** ``spawn_subagent`` tool_uses
    in one assistant turn; a single spawn stays :class:`SpawnSubtaskDecision`
    (SR1 path)."""

    specs: tuple[SpawnSubtaskSpec, ...]
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None
    #: Extended-thinking end-to-end (Slice B): the ThinkingBlocks the LLM
    #: emitted ahead of this turn's ``spawn_subagent`` fan-out. Mirrors
    #: :attr:`ToolCallsDecision.assistant_thinking`; empty for non-reasoning
    #: models.
    assistant_thinking: tuple[ThinkingBlock, ...] = ()
    #: fan-out v2 — per-group opt-in
    #: for wall-clock concurrent drain. Transient Policy→Engine carrier only;
    #: it is NOT persisted on this decision (decisions are not events) — the
    #: Engine's ``handle_spawn_subtasks`` copies it onto the persisted
    #: :class:`~noeta.protocols.wake.SubtaskGroupCompleted` suspend condition
    #: (conditionally folded there). ``False`` (default) = legacy sequential
    #: drain; the workflow ``parallel()`` sets it ``True``.
    concurrent: bool = False


@dataclass(frozen=True, slots=True)
class HitlRequestAnchor:
    """Neutral transport for a structured human-input request the Policy
    posts before suspending on ``yield_for_human``.

    The kernel treats every field as **opaque**: it
    never decodes ``questions_ref``, never parses a schema, never enforces
    caps. It only writes the durable audit anchor (a ``HumanInputRequested``
    /``UserQuestionRequested`` event + a ``pending`` governance slice) and
    suspends on ``HumanResponseReceived(handle=handle)``. The SDK owns the
    body behind ``questions_ref`` and the ``handle`` convention.
    """

    questions_ref: ContentRef
    question_count: int
    handle: str
    request_id: str
    reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class YieldForHumanDecision:
    prompt: str
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None
    #: When present, the kernel writes a neutral structured-input request
    #: audit anchor (opaque ``questions_ref`` + counts/ids/reason) before
    #: suspending, and wakes on ``HumanResponseReceived(handle=anchor.handle)``.
    #: Defaulted (last field) → byte-safe for plain yield recordings.
    request_anchor: Optional[HitlRequestAnchor] = None
    #: Extended-thinking end-to-end (Slice B): the ThinkingBlocks the LLM
    #: emitted ahead of this turn's ``ask_user_question`` tool_use. Mirrors
    #: :attr:`ToolCallsDecision.assistant_thinking`; empty for non-reasoning
    #: models.
    assistant_thinking: tuple[ThinkingBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class WaitTimerDecision:
    seconds: float
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None


@dataclass(frozen=True, slots=True)
class WaitExternalDecision:
    """Wait for an external event source (e.g. webhook, bus).

    Suspends on ``ExternalEvent(event_kind=...)``; the host's external
    ingress wakes the Task by delivering the same ``event_kind`` through
    ``Dispatcher.wake``.
    """

    event_kind: str
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None


@dataclass(frozen=True, slots=True)
class StatePatchDecision:
    """Loop-continuing control-tool call: emit caller-built messages + an
    optional :class:`TaskStatePatch` in a fixed order, then recompose.

    The durable-state-write twin of :class:`ToolCallsDecision` — both are
    loop-continuing members of the ``tool_calls`` family (no ToolRuntime
    invocation, no suspend, no terminal). The Engine assigns NO meaning to
    the payload: the Policy authors every message and every
    patch field. The kernel emits, in a deterministic order it fixes:

        messages_before → (TaskStatePatched, only when ``patch`` is set) →
        messages_after

    then returns ``None`` (loop-continue). A Policy expressing a
    ``todo_write`` puts ``[assistant_tool_use]`` in ``messages_before``, a
    ``set_todos`` patch in ``patch`` (``None`` for a malformed input so no
    state is written), and ``[ack_message]`` in ``messages_after``.
    """

    messages_before: tuple[Message, ...] = ()
    patch: Optional[TaskStatePatch] = None
    messages_after: tuple[Message, ...] = ()
    #: Extended-thinking end-to-end (Slice B): the ThinkingBlocks the LLM
    #: emitted ahead of this turn's control-tool call (``todo_write`` /
    #: plan-mode / invalid ``ask_user_question``). Mirrors
    #: :attr:`ToolCallsDecision.assistant_thinking`; empty for non-reasoning
    #: models.
    assistant_thinking: tuple[ThinkingBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class CompactionRequestedDecision:
    """③ — the loop-continuing compaction member of the unified contract.

    The ReActPolicy returns this from BOTH compaction triggers (D-3):

    * **proactive** (``reason="proactive"``) — the policy's deterministic
      pre-call token estimate hit the available window
      (``context_window - max_output - buffer``);
    * **passive** (``reason="overflow"``) — the provider returned a
      :class:`noeta.protocols.errors.ContextOverflowError` (②'s
      ``raw['category'] == 'overflow'``), so the request must be compacted
      before retrying.

    The kernel handles it as a step in the existing run loop (D-3b — NOT a
    background worker): emit ``CompactionRequested`` (anti-spiral check
    first), then ``Compacted`` carrying the summary the policy already
    produced through its recorded ``RuntimeLLMClient.complete`` (so a resume
    re-reads the recording rather than re-calling the LLM), then loop-continue.

    The Policy does the prune (in the Composer, deterministic) + the
    summarize LLM round-trip BEFORE returning this decision, and carries
    the result here:

    * ``summary`` — the summary text (``None`` when prune alone brought the
      estimate under the window and no summarize was needed);
    * ``boundary_count`` — how many leading messages the summary replaces
      (0 when ``summary is None``).

    ``assistant_message`` is unused on this path (a compaction step writes
    no assistant turn) but kept for Decision-family shape symmetry.
    """

    reason: str
    estimated_tokens: int = 0
    summary: Optional[str] = None
    boundary_count: int = 0
    #: The Composer generation that produced the surrounding plan (the sdk
    #: owns this string — e.g. ``"three_segment.v3"``). Recorded on the
    #: ``Compacted`` event so a resume can tell whether it is re-deriving the
    #: plan under a matching Composer generation. Runtime never hard-codes a
    #: Composer version (provider/sdk neutrality): the Policy supplies it here.
    composer_version: str = ""
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None


Decision = Union[
    FinishDecision,
    FailDecision,
    ToolCallsDecision,
    SpawnSubtaskDecision,
    SpawnSubtasksDecision,
    YieldForHumanDecision,
    WaitTimerDecision,
    WaitExternalDecision,
    StatePatchDecision,
    CompactionRequestedDecision,
]
