"""Decision: the Policy's output, the Engine's input.

There are exactly seven *neutral* Decision kinds — ``tool_calls``,
``spawn_subtask``, ``yield_for_human``, ``wait_timer``, ``wait_external``,
``finish``, ``fail`` — and every class here is one of them or a generalization
that adds no product meaning. Each variant carries an optional typed
:class:`TaskStatePatch`, so a Policy updates long-running task state through
one single-writer channel rather than touching ``TaskState`` itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING, Any, Optional, Union

from noeta.protocols.messages import Message, ThinkingBlock, ToolResultBlock
from noeta.protocols.values import ContentRef

if TYPE_CHECKING:
    from noeta.protocols.task import TaskState


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass(frozen=True, slots=True)
class TaskStatePatch:
    """Typed patch applied to ``TaskState``; the only shape that mutates it.

    The field set is pinned so callers cannot smuggle arbitrary keys into
    ``TaskState``, and the single-writer invariant is expressed as *one* patch
    shape plus *one* :meth:`apply`. Two authors reach it — a Policy through
    ``Decision.state_patch``, and the narrow operator seam
    :meth:`noeta.core.engine.Engine.apply_state_patch` — and both emit
    ``TaskStatePatched`` and call ``apply``, so fold and resume handle them
    identically.

    Field semantics applied by :meth:`apply`:

    * ``set_goal`` / ``set_phase`` / ``set_next_action`` — assign when
      non-``None``; otherwise leave the slice field unchanged.
    * ``add_todos`` / ``add_decisions`` — extend the slice list.
    * ``complete_todos`` — for each id, mark the matching
      ``state.todos[i]`` dict with ``completed=True``. Unknown ids are
      ignored.
    * ``set_todos`` — **replace-all** the checklist: ``None`` leaves it
      unchanged, ``[]`` clears it, a list replaces it wholesale. Distinct
      from ``add_todos`` (append).
    * ``activate_skills`` — union with ``state.active_skills`` (order
      preserved, no duplicates).
    * ``deactivate_skills`` — drop matching entries.

    The skill fields are skill-specific sugar over the generic content channel:
    after applying, ``state.active_content["skill"]`` mirrors
    ``state.active_skills``, the same map ``ContextContentRecorded`` feeds.
    """

    set_goal: Optional[str] = None
    set_phase: Optional[str] = None
    add_todos: list[dict[str, Any]] = field(default_factory=list)
    complete_todos: list[str] = field(default_factory=list)
    add_decisions: list[dict[str, Any]] = field(default_factory=list)
    set_next_action: Optional[str] = None
    activate_skills: list[str] = field(default_factory=list)
    deactivate_skills: list[str] = field(default_factory=list)
    set_todos: Optional[list[dict[str, Any]]] = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical dict form used as ``TaskStatePatchedPayload.patch``.

        ``set_todos`` is OMITTED when ``None``: a patch that does not touch the
        checklist must not add a key to the payload bytes, because refolding a
        recording has to reproduce them exactly. ``[]`` (clear) and a populated
        list ARE kept."""
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

        The only writer of ``TaskState``: the live Engine path and the
        fold-resume path both come through here, so the semantics live in
        exactly one place.
        """
        if self.set_goal is not None:
            state.goal = self.set_goal
        if self.set_phase is not None:
            state.phase = self.set_phase
        if self.set_todos is not None:
            # `[]` clears, `None` means "no change" — hence `is not None`.
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
            # Keep kind "skill" in lockstep with ``active_skills`` (empty ⇒ key
            # absent), preserving any provenance hash a ``ContextContentRecorded``
            # already folded. A skill activated with no recorded hash carries ""
            # — skills are pinned, so the renderer keys off the registry.
            if state.active_skills:
                prev = state.active_content.get("skill", {})
                state.active_content["skill"] = {
                    s: prev.get(s, "") for s in state.active_skills
                }
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
    #: ThinkingBlocks the LLM emitted ahead of this turn's ``tool_use``, carried
    #: OUT-OF-BAND from ``assistant_message`` — which stays thinking-free so
    #: ``runtime.messages`` never absorbs a non-deterministic signature. The
    #: Engine records them into ``ContextState.thinking_by_call_id`` via
    #: ``AssistantThinkingRecorded`` so the next compose can replay the
    #: signature on an Anthropic continuation request. Empty for non-reasoning
    #: models.
    assistant_thinking: tuple[ThinkingBlock, ...] = ()
    #: Tool-result blocks for tool_use blocks a CONTROL tool already answered
    #: in this same turn (e.g. a ``TodoWrite`` acked by its translator while
    #: the remaining calls run through the ToolRuntime). ``handle_tool_calls``
    #: seeds the batched result message with them, so every tool_use the model
    #: emitted still gets exactly one result. Empty for ordinary turns.
    preacked_results: tuple[ToolResultBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class SpawnSubtaskDecision:
    agent_name: str
    goal: str
    inputs: dict[str, Any] = field(default_factory=dict)
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None
    #: Thinking blocks emitted ahead of this turn's ``spawn_subagent`` tool_use.
    #: Mirrors :attr:`ToolCallsDecision.assistant_thinking`.
    assistant_thinking: tuple[ThinkingBlock, ...] = ()
    #: When ``True`` the parent does NOT suspend on a barrier: the Engine emits
    #: ``BackgroundSubagentStarted``, returns a "started" tool_result so the
    #: parent's turn continues, and hands the child to the background-subagent
    #: driver, which delivers the result at a turn boundary. ``False`` suspends
    #: on ``SubtaskCompleted``. A transient Policy→Engine carrier: decisions are
    #: not events, so the durable trace is the ``BackgroundSubagent*`` events. A
    #: background spawn is always a SINGLE child — the
    #: :class:`SpawnSubtasksDecision` fan-out stays foreground-only.
    background: bool = False


@dataclass(frozen=True, slots=True)
class SpawnSubtaskSpec:
    """One member of a fan-out batch.

    ``call_id`` is the originating ``spawn_subagent`` tool_use id, used at
    resume to pair the member's result back to the model's call **positionally**
    from the assistant message — it is NOT persisted on any event payload.

    ``member_index`` is the member's position within its originating call: one
    call carrying a ``spawns`` array yields contiguous specs sharing a
    ``call_id``, numbered 0..k-1, while one-call-one-member forms leave the
    default 0. Batch admission therefore checks the (call_id, member_index)
    layout instead of bare call_id uniqueness."""

    agent_name: str
    goal: str
    call_id: str
    inputs: dict[str, Any] = field(default_factory=dict)
    member_index: int = 0


@dataclass(frozen=True, slots=True)
class SpawnSubtasksDecision:
    """Fan out N sub-agents in one turn and suspend on an all-of join.

    ``specs`` order **is** member (spawn) order: it fixes the group's
    ``subtask_ids`` order and the parent-facing result order. ``ReActPolicy``
    produces this only for **≥2** ``spawn_subagent`` tool_uses in one assistant
    turn; a single spawn stays :class:`SpawnSubtaskDecision`."""

    specs: tuple[SpawnSubtaskSpec, ...]
    state_patch: Optional[TaskStatePatch] = None
    assistant_message: Optional[Message] = None
    #: Thinking blocks emitted ahead of this turn's ``spawn_subagent`` fan-out.
    #: Mirrors :attr:`ToolCallsDecision.assistant_thinking`.
    assistant_thinking: tuple[ThinkingBlock, ...] = ()
    #: Per-group opt-in to a wall-clock concurrent drain; ``False`` drains
    #: sequentially. A transient Policy→Engine carrier: decisions are not
    #: events, so ``handle_spawn_subtasks`` copies it onto the persisted
    #: :class:`~noeta.protocols.wake.SubtaskGroupCompleted` suspend condition.
    concurrent: bool = False


@dataclass(frozen=True, slots=True)
class HitlRequestAnchor:
    """Neutral transport for a structured human-input request the Policy posts
    before suspending on ``yield_for_human``.

    The kernel treats every field as **opaque**: it never decodes
    ``questions_ref``, never parses a schema, never enforces caps. It writes the
    durable audit anchor (a ``HumanInputRequested`` / ``UserQuestionRequested``
    event plus a ``pending`` governance slice) and suspends on
    ``HumanResponseReceived(handle=handle)``. The SDK owns the body behind
    ``questions_ref`` and the ``handle`` convention.
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
    #: When present, the kernel writes a neutral structured-input request audit
    #: anchor before suspending and wakes on
    #: ``HumanResponseReceived(handle=anchor.handle)``.
    request_anchor: Optional[HitlRequestAnchor] = None
    #: Thinking blocks emitted ahead of this turn's ``ask_user_question``
    #: tool_use. Mirrors :attr:`ToolCallsDecision.assistant_thinking`.
    assistant_thinking: tuple[ThinkingBlock, ...] = ()
    #: Recorded as ``TaskSuspended.reason`` in place of the default
    #: ``waiting_human``, so a suspend standing in for something other than
    #: "waiting for the next thing the human types" says so in the ledger
    #: instead of dissolving into a generic pause — the multi-turn wrapper sets
    #: it when it parks a *failed* turn.
    suspend_reason: Optional[str] = None
    #: The terminal answer of the turn this suspend stands in for, when the
    #: suspend replaced a ``FinishDecision``. Multi-turn conversations park on
    #: the next-goal handle instead of completing, so without this the value a
    #: ``FinishDecision`` carried — including an ``output_schema``-deserialized
    #: dict — had nowhere to go, and a host wanting a structured result had to
    #: re-parse the assistant text. ``None`` means "this suspend produced no
    #: answer", which is every suspend that was not standing in for a finish
    #: (a pending question, a parked failure).
    answer: Any = None


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
    """Loop-continuing control-tool call: emit caller-built messages plus an
    optional :class:`TaskStatePatch` in a fixed order, then recompose.

    The durable-state-write twin of :class:`ToolCallsDecision` — both are
    loop-continuing members of the ``tool_calls`` family (no ToolRuntime
    invocation, no suspend, no terminal). The Engine assigns NO meaning to the
    payload: the Policy authors every message and every patch field. The one
    thing the kernel fixes is the emission order:

        messages_before → (TaskStatePatched, only when ``patch`` is set) →
        messages_after

    A Policy expressing a ``todo_write`` puts ``[assistant_tool_use]`` in
    ``messages_before``, a ``set_todos`` patch in ``patch`` (``None`` for a
    malformed input, so no state is written), and ``[ack_message]`` in
    ``messages_after``.
    """

    messages_before: tuple[Message, ...] = ()
    patch: Optional[TaskStatePatch] = None
    messages_after: tuple[Message, ...] = ()
    #: Thinking blocks emitted ahead of this turn's control-tool call. Mirrors
    #: :attr:`ToolCallsDecision.assistant_thinking`.
    assistant_thinking: tuple[ThinkingBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class CompactionRequestedDecision:
    """The loop-continuing compaction member of the Decision family.

    The ReActPolicy returns this from both compaction triggers:

    * **proactive** (``reason="proactive"``) — its deterministic pre-call token
      estimate hit the available window
      (``context_window - max_output - buffer``);
    * **passive** (``reason="overflow"``) — the provider raised
      :class:`noeta.protocols.errors.ContextOverflowError`, so the request must
      be compacted before it can be retried.

    The kernel handles it as a step of the run loop, not a background worker:
    emit ``CompactionRequested`` (anti-spiral check first), then ``Compacted``
    carrying the summary the Policy already produced through its recorded
    ``RuntimeLLMClient.complete`` — so a resume re-reads the recording instead
    of re-calling the LLM — then loop-continue.

    The Policy runs the deterministic prune and the summarize round-trip BEFORE
    returning this decision, and carries the result here: ``summary`` is
    ``None`` when the prune alone brought the estimate under the window, and
    ``boundary_count`` (how many leading messages the summary replaces) is then
    0. ``assistant_message`` is unused on this path — a compaction step writes
    no assistant turn — but kept for Decision-family shape symmetry.
    """

    reason: str
    estimated_tokens: int = 0
    summary: Optional[str] = None
    boundary_count: int = 0
    #: The Composer generation that produced the surrounding plan; the SDK owns
    #: this string. Recorded on the ``Compacted`` event so a resume can tell
    #: whether it is re-deriving the plan under a matching generation. The
    #: runtime never hard-codes a Composer version — the Policy supplies it.
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
