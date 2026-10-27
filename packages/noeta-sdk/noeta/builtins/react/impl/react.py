"""ReActPolicy — the ReAct loop policy.

Bridges a typed
``LLMRequest / LLMResponse`` round-trip into the typed ``Decision``
surface the Engine consumes. The hot path is::

    LLM produced tool_use      → ToolCallsDecision(calls, assistant_message)
    LLM produced end_turn      → FinishDecision(answer, assistant_message)
    LLM produced max_tokens    → its tool calls run as for tool_use; with no
                                 tool call, the partial text is kept and an
                                 output-limit nudge continues the turn
                                 (StatePatchDecision), up to
                                 MAX_OUTPUT_TOKENS_RECOVERY in a row, then
                                 FailDecision(reason="llm_truncated")
    LLM produced error         → FailDecision(reason="llm_error",
                                              retryable=False,
                                              assistant_message=None)
    step counter at ceiling    → FailDecision(reason="react_max_steps_exceeded",
                                              retryable=False)
    step counter at step_limit → one tool-free round-trip asking for a
                                 report, then FinishDecision(that report)

The Engine writes the assistant message to ``RuntimeState.messages``
(single writer); ReActPolicy never touches RuntimeState
directly. The full LLM content (Thinking + Text + ToolUse blocks,
whatever the provider returned) round-trips through
``assistant_message`` so reasoning models do not lose their
``ThinkingBlock`` continuation between turns.

Scope:

* Beyond the three core variants
  (``ToolCallsDecision / FinishDecision / FailDecision``), control-tool calls
  translate into their neutral decisions (spawn-subtask, yield-for-human,
  wait-timer) via ``translate_control_tool``.
* ``model`` is a constant per Policy instance.
* The instance holds **configuration only** — no per-task or per-turn loop
  state. A ReActPolicy lives inside a per-turn Engine (built for one task's
  turn, rebuilt for the next), so anything that must span turns does not
  live on ``self``: the step cap reads ``StepContext.steps_in_turn``
  (Engine-threaded, resets every driven turn), and the compaction-trigger
  baselines live in a :class:`TriggerBaselines` table the host keeps in
  the task's local state and hands each turn's instance (a bare instance
  keeps its own, bounded per ``task_id``).

Layering: this module ships in noeta-sdk's ``react`` built-in
and reaches the kernel across the wheel boundary — so it imports only the
kernel's PUBLIC surface: ``noeta.protocols.*``, ``noeta.runtime.*``, and the
control mechanism band (``noeta.policies.control_semantics``). No
underscore-private kernel name is imported here: a private name carries no
compatibility contract, and a skewed ``noeta-runtime`` would then fail at
policy construction rather than at install. It never imports a provider
adapter (provider injection happens at the call site, behind a
``RuntimeLLMClient``) and never imports ``noeta.testing.*`` (per
``production-cannot-import-testing``).
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from typing import Any, Optional, Protocol, Sequence

from noeta.policies.control_semantics import (
    SPAWN_SUBAGENT_TOOL,
    ControlToolSpec,
    translate_control_tool,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import (
    CompactionRequestedDecision,
    Decision,
    FailDecision,
    FinishDecision,
    StatePatchDecision,
    ToolCall,
    ToolCallsDecision,
)
from noeta.protocols.errors import CATEGORY_OVERFLOW
from noeta.protocols.events import MessageSelection
from noeta.protocols.token_estimate import estimate_messages_tokens
from noeta.protocols.messages import (
    Block,
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    is_host_injected,
)
from noeta.protocols.resources import load_markdown
from noeta.protocols.step_context import StepContext
from noeta.protocols.tool import Tool, ToolContext
from noeta.protocols.view import View

from .call_routing import route_tool_calls


__all__ = [
    "ReActPolicy",
    "SPAWN_SUBAGENT_TOOL",
    "extract_safety_constraints",
    "enforce_verbatim_constraints",
    "looks_like_summary_note",
    "SUMMARY_FAILED_REASON",
]

#: ``SPAWN_SUBAGENT_TOOL`` is defined in ``noeta.policies.control_semantics``
#: (the kernel's reserved control vocabulary) and re-exported here for this
#: module's own readers. It is NOT this module's public address: control tools
#: are kernel vocabulary. Each control-tool SCHEMA function lives in its own
#: built-in — ``todo_write`` / ``ask_user_question`` / ``spawn_subagent``,
#: ``skill`` (skills), and ``run_workflow`` / ``structured_output`` (this
#: built-in's ``control_tool`` module).

#: The compaction summarize instruction, externalized to ``summarize.md``
#: beside this module. It rides the summarize request once, as a trailing
#: ``user`` turn after the history; the request keeps the task's own system
#: prompt so it shares the main loop's cached prefix (see
#: :meth:`ReActPolicy._summary_prompt_request`). Its closing HARD RULE
#: is the model-facing half of the verbatim rule, restricted to constraints the
#: user or the system stated (never one found in a tool result): the
#: deterministic post-check
#: (:func:`enforce_verbatim_constraints`) is the actual guarantee; the prompt
#: nudges the model so the common case produces a clean summary without the
#: appended block.
_SUMMARIZE_PROMPT = load_markdown(__package__, "summarize")

#: The trailing user turn of the step-limit round-trip. ``{n}`` is the limit.
_STEP_LIMIT_PROMPT = (
    "You have reached the limit of {n} steps for this task, and no more tool "
    "calls will run. Stop here and reply with your final report: what you "
    "did, what you found, where the work stands, and what is left to do."
)

#: Output-limit recovery, copied from Claude Code (2.1.300): a response cut by
#: the output cap that carries no tool call keeps its partial content and the
#: turn continues with this user turn instead of failing.
OUTPUT_LIMIT_RESUME_NUDGE = (
    "Output token limit hit. Resume directly \u2014 no apology, no recap of "
    "what you were doing. Pick up mid-thought if that is where the cut "
    "happened. Break remaining work into smaller pieces."
)

#: The same recovery when the cap was spent on reasoning alone: thinking is
#: never recorded, so there is nothing to resume and the model is told so.
OUTPUT_LIMIT_THINKING_NUDGE = (
    "Output token limit hit while you were still reasoning, before this "
    "response produced any text or tool call. That reasoning was not kept and "
    "is not part of this conversation, so there is nothing to resume. "
    "Continue from the conversation above: reason much more briefly and "
    "produce your answer or next tool call well within the output limit."
)

#: Consecutive recoveries allowed before a cut response fails the turn
#: (Claude Code's limit). The count restarts after a tool round or a new user
#: message, and is re-derived from the recorded history, never kept here.
MAX_OUTPUT_TOKENS_RECOVERY = 3

_OUTPUT_LIMIT_NUDGES = (OUTPUT_LIMIT_RESUME_NUDGE, OUTPUT_LIMIT_THINKING_NUDGE)

#: Claude Code's post-compaction file restore (2.1.300): at most this many
#: recently read files come back after a compaction, each within this many
#: tokens (larger ones come back as a note), all of them within the budget.
POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
POST_COMPACT_TOKEN_BUDGET = 50_000

#: The file tools whose successful calls mark a file as recently used.
_READ_TOOL = "Read"
_FILE_TOOLS = (_READ_TOOL, "Edit", "Write")
#: Instruction files ride the instructions resident; restoring them would
#: repeat it.
_INSTRUCTION_FILE_NAMES = frozenset({"NOETA.md", "AGENTS.md", "CLAUDE.md"})

#: Opens the answer a step-limit stop finishes with, so whoever reads it —
#: the host, or the model on a later turn — knows the work was cut off.
_STEP_LIMIT_HEAD = "Stopped at the {n}-step limit before finishing."

#: Claude Code's answer to a summarize request that is itself too long: up
#: to this many retries, each dropping about a fifth of the oldest rounds
#: behind :data:`_SUMMARY_TRUNCATION_MARKER`.
MAX_SUMMARY_OVERFLOW_RETRIES = 3
_SUMMARY_TRUNCATION_MARKER = "[earlier conversation truncated for compaction retry]"

#: The stable head of every summarize-failure reason. A host branches on the
#: prefix (``reason.startswith(SUMMARY_FAILED_REASON)``); the free text after
#: ``": "`` carries the response facts for a human — see
#: :func:`_summary_failure`.
SUMMARY_FAILED_REASON = "compaction_summary_failed"

#: ``_TaskTriggerBaseline.last_input_tokens_at_call`` sentinel: a compaction
#: collapsed the history, so no recorded input count describes it any more.
#: Distinct from ``0`` ("no round-trip yet — fall back to the ctx baseline")
#: because after a compaction the ctx baseline is stale too and must NOT be
#: fallen back to. See :meth:`ReActPolicy._real_baseline`.
_BASELINE_INVALIDATED = -1


@dataclass(slots=True)
class _TaskTriggerBaseline:
    """One task's compaction-trigger baseline pair.

    ``last_estimate_at_call`` is the chars/4 estimate of the request this task
    LAST actually sent to the provider. The compaction trigger mixes the real
    recorded usage of that round-trip with the chars/4 delta since (``current
    estimate − this``). Advanced ONLY right after a successful
    ``_llm.complete`` (never on a proactive/early return), so it always tracks
    the last request that actually consumed tokens.

    ``last_input_tokens_at_call`` is the provider's REAL input count for the
    task's last main round-trip, ``0`` before the first one, or
    ``_BASELINE_INVALIDATED`` right after a compaction. Pinned beside
    ``last_estimate_at_call`` (same call site) and read by
    ``_trigger_estimate`` in preference to ``StepContext.last_input_tokens``:
    the ctx projection only moves when the LLM client's ``apply_event``
    callback is wired, so this pair is the guaranteed-live intra-turn
    baseline. Most recent wins — never ``max``: after a compaction the real
    count DROPS, and a max would pin the trigger to a stale pre-compaction
    high and re-fire forever.

    Both fields are keyed **per ``(task, model)``** in a
    :class:`TriggerBaselines` table. The Engine — and so this policy — is
    built afresh every turn, and a pair that lived on the instance would
    reset at every turn boundary, dropping the trigger to the pure chars/4
    estimate for each turn's first request; so the host keeps the table in
    the task's local state (``noeta.runtime.task_local``) and hands it to
    each turn's instance through the factory's ``task_slot``. Keying by task
    keeps one conversation's near-window baseline out of another's trigger
    (a bare instance without a host slot may serve several tasks); keying by
    model keeps a real token count from one tokenizer from describing a
    request to another. ``0`` on a fresh entry; because ``decide()`` re-runs
    in the same order on resume, the values reconstruct identically (no
    persisted state needed). Process-local housekeeping — never composed,
    never recorded.
    """

    last_estimate_at_call: int = 0
    last_input_tokens_at_call: int = 0


#: Upper bound on the per-task baseline table. Entries beyond it evict oldest
#: first; an evicted task that later decides again starts from a fresh pair,
#: which reproduces exactly the fresh-instance fallback (pure chars/4 estimate
#: until its next round-trip records real usage) — degraded precision for one
#: trigger, never a wrong decision.
_MAX_TRACKED_TASK_BASELINES = 1024

#: The task-local slot the host keeps a task's :class:`TriggerBaselines` in.
TRIGGER_BASELINES_SLOT = "react.trigger_baselines"


class TriggerBaselines:
    """A bounded ``(task_id, model) → _TaskTriggerBaseline`` table (see
    :class:`_TaskTriggerBaseline` for why it is not a policy attribute).

    The lock guards only the table's structure (get-or-create / LRU reorder /
    eviction); a single entry is only ever mutated by its task's one driving
    thread. The host keeps one per task in the task's local state; a bare
    policy (direct construction, a host without the slot) keeps its own.
    """

    def __init__(self, *, max_tasks: int = _MAX_TRACKED_TASK_BASELINES) -> None:
        self._max = max_tasks
        self._table: OrderedDict[tuple[str, str], _TaskTriggerBaseline] = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    def state(self, task_id: str, model: str) -> _TaskTriggerBaseline:
        """Get-or-create ``(task_id, model)``'s entry (LRU-refreshed)."""
        key = (task_id, model)
        with self._lock:
            state = self._table.get(key)
            if state is None:
                state = _TaskTriggerBaseline()
                self._table[key] = state
            else:
                self._table.move_to_end(key)
            while len(self._table) > self._max:
                self._table.popitem(last=False)
            return state

    def __len__(self) -> int:
        with self._lock:
            return len(self._table)

    def keys(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(self._table)

#: Sanity band for :meth:`ReActPolicy._observed_density` — real provider tokens
#: per chars/4 estimated token. The heuristic's honest spread on real payloads
#: runs from ~0.5 (long-word prose) through ~2 (dense JSON) up to ~4–7 for
#: pure-CJK text (the chars/4 estimator counts code points, and CJK tokenizes
#: at ~1–1.7 tokens per character), so the band must sit ABOVE that spread: a
#: clamp that binds on a genuine CJK measurement over-protects the tail by the
#: clamped factor and makes CJK-heavy sessions compact more often than needed.
#: The band exists only to stop a NON-measurement from propagating: the
#: numerator is whatever the provider reported, and a gateway that reports
#: ``input_tokens=10`` for a ~2600-token request yields ~0.004 — which divides
#: the tail budget by 250, protects a "tail" bigger than the whole history,
#: pins the summary boundary at 0, and kills every compaction with
#: ``compaction_no_progress``. Kept numerically identical to the Composer's
#: ``_density`` band (they clamp the same quantity on different inputs) but
#: deliberately NOT imported from it: the two bands do not import each other.
_DENSITY_MIN = 0.25
_DENSITY_MAX = 8.0


def _carries_tool_result(message: Message) -> bool:
    """True when ``message`` is a tool-result turn (a ``role="tool"`` message,
    which the Engine batches one-per-assistant-turn, or any message carrying a
    :class:`ToolResultBlock`). Such a turn's matching ``tool_use`` lives on the
    preceding ``assistant`` message, so a summary boundary must never split the
    two — see :meth:`ReActPolicy._summary_boundary`."""
    if message.role == "tool":
        return True
    return any(isinstance(b, ToolResultBlock) for b in message.content)


class _LLMClientP(Protocol):
    """The slice of :class:`noeta.runtime.llm.RuntimeLLMClient` the policy
    uses. ``RuntimeLLMClient`` matches this Protocol so the policy stays
    decoupled from the concrete client."""

    def complete(
        self,
        req: LLMRequest,
        ctx: StepContext,
        *,
        selection: Optional[MessageSelection] = None,
        allow_stream: bool = True,
    ) -> LLMResponse: ...


class ReActPolicy:
    """Minimal Noeta ReAct policy.

    Safe to share across tasks and turns: the instance carries configuration
    only. The ``max_steps`` ceiling reads the Engine-threaded
    ``StepContext.steps_in_turn`` (so a runaway loop is bounded per driven
    turn and the budget renews every turn), and the compaction-trigger
    baselines live in a :class:`TriggerBaselines` table keyed per
    ``(task_id, model)`` — the host's task-local one when it hands one in,
    so the calibration spans the task's turns; the instance's own otherwise.
    """

    def __init__(
        self,
        *,
        llm: _LLMClientP,
        tools: dict[str, Tool],
        system_prompt: str,
        model: str,
        #: Per-turn decide() ceiling — a runaway-loop backstop, not a working
        #: budget. Deliberately enormous by default: real turns end on their
        #: own long before it, and a tight default has already bricked long
        #: conversations once (the counter used to live on the instance).
        max_steps: int = 1_000_000,
        #: Per-turn step count at which the turn is wound up instead of
        #: continued: one more round-trip, tools forbidden, asks the model for
        #: a report of where the work got to, and the turn finishes with it.
        #: A working budget, unlike ``max_steps``; ``None`` ⇒ none.
        step_limit: Optional[int] = None,
        max_history_messages: Optional[int] = None,
        #: The routing-ordered control-tool translate specs.
        #: Each spec's ``translate`` closure carries its own build inputs (the
        #: skill mount captures its menu), so the policy holds no ``*_enabled``
        #: flags or ``skill_menu_names``: mounting a spec IS enablement, and an
        #: empty tuple means no control tool is offered. The kernel builder's mount
        #: loop produces them; ``()`` for a bare policy (e.g. a subtask).
        control_translate_specs: tuple[ControlToolSpec, ...] = (),
        content_store: Optional[ContentStore] = None,
        context_window: Optional[int] = None,
        max_output_tokens: int = 0,
        compaction_buffer: int = 0,
        tail_token_budget: int = 0,
        composer_version: str = "",
        # Wiring-only LLM request overrides. Omitted from canonical bytes
        # on the wire when ``None`` (LLMRequest.__canonical_omit_none__),
        # so a host that leaves them unset keeps a stable prompt
        # prefix (prefix-cache friendly).
        output_schema: Optional[dict[str, Any]] = None,
        thinking: Optional[str] = None,
        effort: Optional[str] = None,
        #: Optional cheaper model for the summarize round-trip ONLY (decide
        #: turns always use ``model``). Already alias-resolved by the host, for
        #: the same reason ``model`` is: the alias table lives in the providers
        #: built-in and this module never imports it.
        compaction_model: Optional[str] = None,
        #: The compaction model's OWN output ceiling (catalog-derived by the
        #: host, like ``compaction_model`` itself). The summarize request's
        #: ``max_tokens`` must be valid for the model that serves it —
        #: forwarding the MAIN model's cap to a smaller summarizer is a
        #: provider 400. ``None`` (no compaction model / old caller) keeps
        #: the main model's cap.
        compaction_max_output_tokens: Optional[int] = None,
        #: The compaction-trigger baseline table (see
        #: :class:`_TaskTriggerBaseline`). The host passes the task's own so
        #: the calibration outlives this per-turn instance; ``None`` (a bare
        #: policy) keeps a private one.
        trigger_baselines: Optional[TriggerBaselines] = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._system_prompt = system_prompt
        self._model = model
        self._max_steps = max_steps
        self._step_limit = step_limit
        self._max_history_messages = max_history_messages
        self._baselines = (
            trigger_baselines if trigger_baselines is not None else TriggerBaselines()
        )
        # ③ compaction trigger configuration. When ``context_window``
        # is None compaction is OFF (no proactive trigger,
        # an overflow error stays a FailDecision). When set, the available
        # window is ``context_window - max_output_tokens - compaction_buffer``
        # and BOTH triggers (proactive estimate / passive overflow) return a
        # ``CompactionRequestedDecision``. The window/output/buffer come from
        # the catalog ModelSpec but are INJECTED here — react.py never
        # imports the providers built-in (provider neutrality). The
        # ``tail_token_budget`` is the protected tail window the summarize
        # boundary is computed against; ``composer_version`` is recorded on
        # the Compacted event (the sdk owns the string — runtime stays
        # composer-version-agnostic).
        self._context_window = context_window
        self._max_output_tokens = max_output_tokens
        self._compaction_buffer = compaction_buffer
        self._tail_token_budget = tail_token_budget
        self._composer_version = composer_version
        # CW18d: the ask_user_question path builds a ContentStore-backed
        # ``questions_ref`` for the HITL request anchor it carries on the
        # neutral ``YieldForHumanDecision``. Required only when
        # ``ask_user_question_enabled``; the runner threads the same
        # store the recording lives in so a resumed run rebuilds identical refs.
        self._content_store = content_store
        # The routing-ordered translate specs the mount
        # loop produced. Each spec's ``translate`` closure carries its own build
        # inputs (the ``skill`` mount captures the indexed menu; ``spawn_subagent``
        # / ``todo_write`` / ``ask_user_question`` / ``run_workflow`` carry
        # nothing extra), so the policy holds no per-tool ``*_enabled`` flag or
        # ``skill_menu_names`` — a spec's PRESENCE is its enablement, and its
        # POSITION is its routing priority. The schema half rides the composer's
        # ``control_action_schemas`` (built from the same mounts). ``()`` ⇒ no
        # control tool offered.
        self._control_translate_specs = control_translate_specs
        # Structured output and reasoning controls. Carried verbatim into
        # every LLMRequest so adapters can map them to their vendor wire
        # format (see providers/anthropic.py and providers/openai_compat.py).
        self._output_schema = output_schema
        self._thinking = thinking
        self._effort = effort
        # The summarize round-trip's model, when the host opted into a cheaper
        # one. ``None`` ⇒ ``self._model``, i.e. exactly the pre-knob request.
        # Pure configuration pinned at construction, so a resumed run rebuilds
        # the identical summarize request (determinism is what makes the
        # recorded summary reproducible).
        self._compaction_model = compaction_model
        # The summarize request's output cap: the compaction model's own
        # ceiling when a compaction model is set (and the host supplied it),
        # else the main model's. Pinned here so ``_summary_prompt_request``
        # stays a pure function of construction state — same determinism
        # argument as ``_compaction_model`` above.
        self._summary_max_tokens = (
            compaction_max_output_tokens if compaction_model else None
        ) or max_output_tokens

    # ------------------------------------------------------------------
    # Policy Protocol
    # ------------------------------------------------------------------

    def _baseline_state(self, task_id: str) -> _TaskTriggerBaseline:
        """``(task_id, model)``'s trigger-baseline entry — see
        :class:`TriggerBaselines`."""
        return self._baselines.state(task_id, self._model)

    def decide(self, ctx: StepContext, view: View) -> Decision:
        # The per-turn ceiling: ``ctx.steps_in_turn`` is the Engine's count of
        # decide() calls already made this driven turn, reset every turn by
        # construction. Comparing the Engine-threaded count (instead of an
        # instance counter) is what keeps the budget PER TURN — the instance
        # is cached across turns and tasks, so an instance counter accumulated
        # for the cache entry's whole life and eventually failed every turn
        # sharing it.
        if ctx.steps_in_turn >= self._max_steps:
            return FailDecision(
                reason="react_max_steps_exceeded", retryable=False
            )
        if self._step_limit is not None and ctx.steps_in_turn >= self._step_limit:
            return self._step_limit_decision(ctx, view)
        state = self._baseline_state(ctx.task_id)
        request, selection = self._build_request_and_selection(view)
        # ③ (D-3a) proactive trigger. The trigger size is no
        # longer a pure chars/4 estimate of the whole request — that
        # systematically under-counts cache / structured blocks / images, and
        # the token gate is the ONLY compaction entry, so its
        # precision matters. We mix the REAL recorded usage of the previous
        # round-trip (``ctx.last_input_tokens``) with a chars/4 estimate of only
        # what was appended since (see ``_trigger_estimate``). The bare chars/4
        # ``estimated`` is still computed and carried on the Decision /
        # ``Compacted`` event as the provider-neutral size signal.
        estimated = estimate_messages_tokens(request.messages)
        trigger_size = self._trigger_estimate(ctx, estimated, state)
        if self._compaction_triggered(trigger_size):
            # Compact FIRST — do not spend the main LLM call on a doomed
            # request. (No LLM call this step, so ``last_estimate_at_call``
            # is intentionally NOT advanced — the mix baseline only moves when
            # a real round-trip records fresh usage.)
            return self._compaction_decision(
                ctx, view, state, reason="proactive", estimated=estimated
            )
        response = self._llm.complete(request, ctx, selection=selection)
        # a real round-trip just happened, so the next turn's
        # "appended since" delta is measured from THIS request's estimate.
        # Pin it AFTER the call so a proactive/early return never advances it.
        state.last_estimate_at_call = estimated
        # …and pin the real count that came back with it, so the next step in
        # THIS tool loop mixes against a live baseline instead of a frozen
        # fold projection. Only a SUCCESSFUL main round-trip counts: the
        # summarize call inside ``_compaction_decision`` measures a different
        # request, and an errored response must not move the baseline either.
        # The exception-shaped errors do carry an empty Usage (``input == 0``),
        # but a 200-shape overflow arrives as ``stop_reason="error"`` WITH the
        # provider's real count for a request that is about to be compacted
        # away — pinning it would leave correctness hanging on the downstream
        # ``_BASELINE_INVALIDATED`` reset, so gate on the stop_reason here.
        if response.stop_reason != "error" and response.usage.input > 0:
            state.last_input_tokens_at_call = response.usage.input
        # ③ (D-3b) passive trigger: the provider returned an overflow error
        # (②'s category). Compact before retrying instead of failing. The mix
        # baseline (``ctx.last_input_tokens``) is the LAST SUCCESSFUL turn's
        # usage, so the passive path — the final safety net for an estimate
        # that came in low — is preserved exactly (red line).
        if (
            self._context_window is not None
            and response.stop_reason == "error"
            and (response.raw or {}).get("category") == CATEGORY_OVERFLOW
        ):
            return self._compaction_decision(
                ctx, view, state, reason="overflow", estimated=estimated
            )
        return self._response_to_decision(response, view)

    def _step_limit_decision(self, ctx: StepContext, view: View) -> Decision:
        """Wind the turn up at ``step_limit``: the normal request plus a
        trailing user turn asking for a report, with ``tool_choice: none``
        (the history still holds tool blocks, so the schemas must ride), and
        finish with whatever text comes back under :data:`_STEP_LIMIT_HEAD`.

        The instruction is not recorded as a message — only the answer is —
        exactly like the summarize instruction. A model that answers with a
        tool call anyway (a gateway that ignores ``tool_choice``) has its
        calls dropped; with no text at all the head stands alone. An errored
        round-trip fails the turn as any errored round-trip does.
        """
        assert self._step_limit is not None
        request, _selection = self._build_request_and_selection(view)
        instruction = Message(
            role="user",
            content=[TextBlock(text=_STEP_LIMIT_PROMPT.format(n=self._step_limit))],
        )
        response = self._llm.complete(
            dc_replace(
                request,
                messages=[*request.messages, instruction],
                metadata={**request.metadata, "tool_choice": "none"},
            ),
            ctx,
        )
        if response.stop_reason == "error":
            return self._error_to_decision(response)
        text = _joined_text(response).strip()
        head = _STEP_LIMIT_HEAD.format(n=self._step_limit)
        answer = f"{head}\n\n{text}" if text else head
        return FinishDecision(
            answer=answer,
            assistant_message=Message(
                role="assistant", content=[TextBlock(text=answer)]
            ),
        )

    # ------------------------------------------------------------------
    # ③ compaction (D-3)
    # ------------------------------------------------------------------

    def _available_window(self) -> Optional[int]:
        """The usable history window in estimated tokens, or ``None`` when
        compaction is disabled (no ``context_window`` injected)."""
        if self._context_window is None:
            return None
        return max(
            0,
            self._context_window
            - self._max_output_tokens
            - self._compaction_buffer,
        )

    def _trigger_estimate(
        self, ctx: StepContext, estimated: int, state: _TaskTriggerBaseline
    ) -> int:
        """The size the proactive trigger compares against the window.

        Compaction mix: ``real previous-turn usage + chars/4 of what was
        appended since``. ``ctx.last_input_tokens`` is the provider's REAL
        input count for the previous round-trip (already recorded — read-only,
        never re-counted live → a resumed run derives the same trigger). The
        "appended since" delta is approximated as ``current chars/4 estimate −
        the chars/4 estimate of the request we last actually sent``
        (``state.last_estimate_at_call``): a turn that only appended a tool
        result grows the estimate by that result's size, which is exactly the
        delta we want to add on top of the real baseline.

        Two byte-safe fallbacks keep this monotone and never WORSE than the old
        pure estimate:

        * **First turn / no recorded usage** (``last_input_tokens <= 0``): we
          have no real baseline yet, so fall back to the pure chars/4
          ``estimated`` — identical to the pre-compaction behaviour.
        * **Estimate shrank** (right after a compaction the request gets
          smaller, so ``delta < 0``): clamp the delta to ``0`` so we never
          subtract below the real baseline.

        Finally we ``max`` the mixed value with the pure ``estimated`` so the
        mix can only ever raise the trigger size, never mask a genuinely huge
        raw history that the real baseline happens to under-represent.
        """
        baseline = self._real_baseline(ctx, state)
        if baseline <= 0:
            return estimated
        delta = max(0, estimated - state.last_estimate_at_call)
        return max(estimated, baseline + delta)

    def _real_baseline(
        self, ctx: StepContext, state: _TaskTriggerBaseline
    ) -> int:
        """The most recent REAL input count available, or ``0`` if none is.

        ``state.last_input_tokens_at_call`` (this task's own last main
        round-trip, pinned inside ``decide``) is at least as recent as
        ``ctx.last_input_tokens`` (fold-projected; live mid-turn only when the
        Engine wires the ``apply_event`` callback), so it wins whenever it is
        set. A fresh entry has none and falls back to ctx — the cross-turn
        baseline. Both derive from an already-recorded ``Usage``, so a resumed
        run re-derives the same number: we never count tokens live.

        ``_BASELINE_INVALIDATED`` (a compaction landed) suppresses BOTH: no
        recorded count describes the collapsed history, so the caller falls
        back to the pure estimate rather than trust a stale high.
        """
        if state.last_input_tokens_at_call == _BASELINE_INVALIDATED:
            return 0
        if state.last_input_tokens_at_call > 0:
            return state.last_input_tokens_at_call
        return ctx.last_input_tokens

    def _compaction_triggered(self, estimated: int) -> bool:
        window = self._available_window()
        return window is not None and estimated >= window

    def _summary_prompt_request(
        self,
        history: list[Message],
        tools: list[dict[str, Any]],
        *,
        system: Optional[Message] = None,
        context: Sequence[Message] = (),
        forbid_tools: bool = False,
    ) -> LLMRequest:
        """Build the deterministic summarize round-trip request.

        **The request is the main loop's request with the instruction as a
        trailing ``user`` turn.** ``system`` is the task's own system prompt
        (the View's ``stable_prefix``), ``context`` its ``semi_stable`` blocks,
        and ``tools`` its live schemas — the exact head a normal step sends —
        so the summarize call reads the cached prefix the main loop already
        wrote instead of paying a fresh write for the whole window. How much
        of it a provider can reuse is the provider's business: an automatic
        prefix cache reuses the history too, and so does Anthropic's message
        tier as long as the request carries no ``tool_choice`` (a changed
        ``tool_choice`` resets that tier — which is why the first attempt
        sends none; see ``forbid_tools`` below).

        The instruction rides ONLY as the trailing user turn. A model handed a
        tool-result-ending history and a system-only instruction can weigh the
        conversation's momentum over the system text and simply continue the
        task. Observed on a relay-served frontier model: every proactive
        compaction either came back with no content at all (a loud
        ``compaction_summary_failed``) or with a one-line "next step"
        narration that then silently replaced the whole collapsed prefix. The
        identical request with the instruction as the final user message is
        answered with the note — which is also how Claude Code sends its
        compaction prompt. A system copy on top of it would both duplicate
        the instruction and swap out the prefix the cache rides on.

        A fixed structured-section instruction over the
        history-to-be-collapsed. The sections are adopted from Claude Code's
        compaction template. Noeta only ever summarises the OLD PREFIX
        (everything before the protected verbatim tail window), so the recent
        state already sits verbatim in that tail — which is why Current Work
        and Next Step (sections 8 and 9) are the two the instruction has the
        model REWRITE from the newest messages rather than carry forward from
        an earlier note: an earlier note's version of them describes a moment
        the tail has already moved past (see ``summarize.md``).

        The "Files & Code" section keeps a PATH LIST only, never the file
        bodies. Re-injecting bodies is a false need here: re-reading disk breaks
        determinism, and a ContentStore snapshot would be a STALE copy for an
        agent that is actively editing — so we record the relevant paths and let
        the model fetch the CURRENT version with ``read`` when it needs them.

        ``tools`` is the SAME provider tool schema list a normal turn carries
        (``view.provider_tool_schemas``), and sending it is load-bearing rather
        than cosmetic. The collapsed prefix still contains the ``tool_use`` /
        ``tool_result`` blocks the model actually produced — stripping them
        would rewrite what was said and lose the grounding the note is built
        from — and a provider handed tool blocks with no tool definitions
        rejects the whole request (Anthropic: 400), which the empty-summary
        guard below then turns into a non-retryable
        ``compaction_summary_failed``: the FIRST real proactive compaction would
        kill the task. Carrying the live schemas also keeps the summarize call
        on the same cached system+tools prefix as the main loop instead of
        forfeiting it for one round-trip. Sending the schemas has a flip side,
        though: a summarizer that SEES live tools may answer with a tool call
        instead of the note — a history ending on a tool result invites exactly
        that — and an only-``tool_use`` response has no text, so the same
        empty-summary guard would kill the task. The prompt's closing no-tools
        instruction is the first line of defence, and the first attempt relies
        on it alone: it carries no ``tool_choice``, so on Anthropic the whole
        prefix — messages tier included — is served from the main loop's
        cache. Only when that attempt answers with a tool call (or no text) does
        ``_compaction_decision`` retry once with ``forbid_tools=True``, which
        sets ``metadata["tool_choice"] = "none"``: the first-party adapters map
        it to their vendor spelling and close the door at the wire, at the
        price of the messages-tier cache for that one retry.

        The MODEL is the one thing this request may not share with a normal
        turn: ``compaction_model`` (when the host set it) routes the summarize
        call to a cheaper model, because condensing text the strong model
        already produced is the one mechanical step in the loop. It is a
        per-session constant, so determinism is unaffected — but note the
        trade-off it makes on purpose: a different model means a different
        cached prefix for this one round-trip, paying a cache miss to save on
        rate. ``None`` keeps the main model and the shared prefix.

        Fully deterministic (same history + same tools → same request) so a
        resumed run rebuilds the identical summarize call and the prompt prefix
        stays stable (prefix-cache friendly). Provider-neutral: the wording
        names no vendor and no vendor-specific mechanism — it works for any
        provider. Goes through the same ``_llm`` as a normal turn, so it records
        LLMRequestStarted/Recorded/Finished.
        """
        instruction_turn = Message(
            role="user",
            content=[TextBlock(text=_SUMMARIZE_PROMPT)],
        )
        return LLMRequest(
            model=self._compaction_model or self._model,
            messages=[*context, *history, instruction_turn],
            tools=list(tools),
            system=system,
            # Only the retry forbids tool calls at the wire (see the
            # docstring): a first attempt without ``tool_choice`` keeps the
            # Anthropic message-tier cache entry; first-party adapters map the
            # retry's value to their vendor tool_choice.
            metadata={"tool_choice": "none"} if forbid_tools else {},
            # Forward an output ceiling for the model that SERVES this request.
            # Without one, a gateway that caps output at a stingy default when
            # the client sends no ``max_tokens`` (the aidp Responses gateway
            # caps at 1000) starves the summary: a reasoning model spends the
            # whole default budget on hidden reasoning and comes back
            # ``stop_reason="max_tokens"`` with an EMPTY text body, which the
            # empty-summary guard below then (correctly) turns into a
            # ``compaction_summary_failed`` FailDecision — killing the task on
            # every proactive compaction. But the MAIN model's cap is only
            # valid on the main model: when ``compaction_model`` routes this
            # call to a smaller summarizer, a larger cap is a provider 400 with
            # the same task-killing outcome, so ``_summary_max_tokens`` is the
            # compaction model's own catalog ceiling whenever one is set. ``0``
            # (host didn't inject) ⇒ ``None`` ⇒ omitted from canonical bytes,
            # so a session's summarize prompt prefix is byte-identical and the
            # request stays deterministic on resume.
            max_tokens=self._summary_max_tokens or None,
        )

    def _previous_summary_message(self, view: View) -> Optional[Message]:
        """The summary message standing in for the already-collapsed prefix, or
        ``None`` when nothing has been collapsed yet.

        The bounded summarize input (previous summary + delta) needs the
        previous summary TEXT, and it is already on the View the Policy holds:
        when ``ContextState.summary_ref`` is set, the Composer's
        ``_apply_summary`` replaces ``rolling_history[:summary_boundary]`` with
        exactly ONE message at the HEAD of the dynamic suffix, and the anchored
        placement pass clamps every mid-task resident to index ≥ 1 precisely so
        nothing can precede it (the tail prune clears outputs in place and drops
        no message; the compose-time reminders append at the tail).
        ``summary_boundary > 0`` is the View-visible signal that the swap
        happened — the boundary only ever advances through a ``Compacted``, and
        a ``Compacted`` always carries a summary.

        Reading it off the View is what keeps the Policy pure: no ContentStore
        deref, and no Policy-private carry-over (neither a resumed run — which
        may build a fresh instance — nor a cache-evicted-and-rebuilt engine
        holds the old instance state, so any state consulted here would
        silently change the summarize request across those boundaries — the
        one thing determinism forbids). Anything that does not look like that
        message returns ``None`` and the caller falls back to summarising the
        whole raw prefix: lossless, merely unbounded, i.e. the safe direction
        to degrade in.
        """
        if view.summary_boundary <= 0 or len(view.segments) < 3:
            return None
        dynamic = view.segments[2].content
        if not dynamic:
            return None
        head = dynamic[0]
        if head.role != "user" or head.origin is not None:
            return None
        if len(head.content) != 1 or not isinstance(head.content[0], TextBlock):
            return None
        return head

    def _compaction_decision(
        self,
        ctx: StepContext,
        view: View,
        state: _TaskTriggerBaseline,
        *,
        reason: str,
        estimated: int,
    ) -> Decision:
        """Run the summarize round-trip and return the unified decision.

        D-3c MVP single-pass replacement: everything before the protected
        tail window is collapsed into one summary message. **Finding 2**: the
        boundary is computed over ``view.rolling_history`` — the RAW
        ``task.runtime.messages`` the Composer slices with — NOT over the
        request's ``iter_messages()`` projection (which is post-summary,
        post-prune, skill-prefixed and tail-truncated). That way the boundary
        the policy records is a raw-history index that the Composer's
        ``_apply_summary`` (which does ``rolling_history[:boundary]``) applies
        to exactly the same messages.

        **Bounded summarize input (previous summary + delta).** The recorded
        boundary stays cumulative-from-zero — it is the Composer's slice index
        and nothing here changes that — but what is SENT to the summarizer is
        the previous summary message followed by
        ``rolling_history[previous_boundary:boundary]``, i.e. a
        summary-of-summary. Re-summarising ``[:boundary]`` from index 0 on every
        compaction is what makes the Nth summarize input grow with N: the raw
        history is never trimmed while the trigger compares the *composed*
        (post-summary) size, so by the second or third compaction the summarize
        call itself exceeds the window and dies as
        ``compaction_summary_failed`` — a non-retryable task death. Feeding the
        previous note back in keeps every pass roughly one window wide and loses
        nothing that the previous note preserved. The input is still a pure
        function of the composed View, so a resumed run rebuilds it identically;
        when no previous summary is reachable the whole raw prefix is sent, as
        before.

        **The summarizer sees what the main loop sees.** Every tool result the
        composed View has cleared goes into the summarize input cleared too
        (``_as_composed``). The raw history keeps every body, and by the time
        the trigger fires the clearing valves have long been at work, so a raw
        input would bring back everything they cleared — a request far past
        the window that the main loop never sent. Should the request still
        overflow, it is retried up to :data:`MAX_SUMMARY_OVERFLOW_RETRIES`
        times, each dropping the oldest fifth of the rounds
        (``_drop_oldest_rounds``) — Claude Code's behavior.

        The boundary is computed deterministically from ``tail_token_budget``
        (the same budget the Composer prunes against), so a resumed run derives
        the same boundary. The summarize LLM call goes through ``self._llm``
        (recorded; a resumed run re-reads the recorded response).

        **Finding 3 (anti-spiral, primary judge — boundary monotonic
        progress)**: the trigger may fire when there is nothing *new* left to
        summarise. ``view.summary_boundary`` is the cumulative raw-history
        prefix already collapsed behind the current summary; a fresh boundary
        only makes progress when it strictly exceeds it (``boundary >
        view.summary_boundary``) — i.e. there is a new, not-yet-summarised
        prefix to fold. When the trigger fires but ``boundary <=
        view.summary_boundary`` (including ``boundary <= 0``, the degenerate
        case where even the whole history is inside the protected tail), a
        ``CompactionRequested`` would collapse the same prefix again and loop
        forever (compose → over window → compact-no-op → compose …). We fail
        fast with a non-retryable ``FailDecision(compaction_no_progress)``
        instead.

        Because every emitted ``CompactionRequested`` therefore strictly
        advances ``summary_boundary``, and the boundary is bounded above by
        ``len(history)``, the compaction loop is guaranteed to terminate — yet
        a long session that grows its raw history between compactions (real
        tool work) keeps producing a *larger* summarisable prefix, so a
        legitimate later compaction is never refused. The complementary
        handler-side check (``handle_compaction_requested``) is pure defence
        for a Policy that bypasses this guarantee.
        """
        history = view.rolling_history
        boundary = self._summary_boundary(history, state)
        if boundary <= view.summary_boundary:
            # No NEW prefix beyond what is already collapsed — compaction would
            # re-summarise the same prefix and spin forever. Fail closed; this
            # also covers the ``boundary <= 0`` case.
            return FailDecision(
                reason="compaction_no_progress", retryable=False
            )
        previous_summary = self._previous_summary_message(view)
        to_summarize: list[Message]
        if previous_summary is None:
            # First compaction (or a View whose head is not the Composer's
            # summary message): the whole raw prefix is the input.
            to_summarize = list(history[:boundary])
        else:
            # Summary-of-summary: the note already covering
            # ``[:view.summary_boundary]`` plus only the messages appended
            # since. The slice starts on a self-contained turn — the previous
            # boundary was itself snapped forward past every tool-result
            # message — so the delta never opens on an orphan ``tool_result``.
            to_summarize = [
                previous_summary,
                *history[view.summary_boundary : boundary],
            ]
        to_summarize = _as_composed(to_summarize, view)
        summary_resp, summary = self._summarize(ctx, view, to_summarize)
        attempt = to_summarize
        for _ in range(MAX_SUMMARY_OVERFLOW_RETRIES):
            if not _is_overflow(summary_resp):
                break
            # The summarize request itself is over the window: drop the
            # oldest rounds and try again. Each retry is a recorded
            # round-trip and whether it happens is a pure function of the
            # recorded response before it, so a resume takes the same path.
            # The previous note is kept — it is small and covers everything
            # before the delta.
            shorter = _drop_oldest_rounds(
                attempt, keep_first=previous_summary is not None
            )
            if shorter is None:
                break
            attempt = shorter
            summary_resp, summary = self._summarize(ctx, view, attempt)
        # The summarize round-trip can come back failed — an ``error``
        # stop_reason (the LLM client's transient retries already exhausted, or
        # a fatal/overflow error) — empty (a model that emitted only a
        # thinking block / whitespace), or as something that is NOT a note (a
        # model that ignored the instruction and narrated its next step
        # instead). Recording ANY of these as a ``Compacted`` would set
        # ``summary_ref`` to it and then let the Composer's ``_apply_summary``
        # REPLACE the whole collapsed prefix with it: the early intent and
        # accumulated context are destroyed silently, AND an empty ``user``
        # text block is itself rejected with a 400 on the very next request.
        # Fail the step cleanly instead, leaving the durable history untouched
        # — a bad summary is strictly worse than no compaction. The reason
        # carries the response facts (see ``_summary_failure``) so the host's
        # receipt says what came back, not just that it was refused.
        # Deterministic on resume: the recorded summarize response replays
        # identically, so the same branch is taken.
        failure = _summary_failure(summary_resp, summary)
        if failure is not None:
            return FailDecision(
                reason=f"{SUMMARY_FAILED_REASON}: {failure}", retryable=False
            )
        # the model is *asked* (via the summarize prompt) to keep
        # safety/permission directives verbatim, but cannot be trusted to. Run
        # the deterministic post-check over the SAME input we summarized: any
        # detected constraint that the model dropped or paraphrased is appended
        # back word-for-word. That input begins with the previous summary
        # whenever one exists, so a constraint first stated long before the
        # current delta — and preserved by the earlier pass — is re-detected and
        # keeps binding across arbitrarily many compactions. Pure over
        # ``(to_summarize, summary)`` so the recorded summary stays a
        # deterministic function of the recorded response → a resumed run
        # rebuilds the identical summary.
        summary = enforce_verbatim_constraints(
            summary, extract_safety_constraints(to_summarize)
        )
        # This decision collapses the prefix, so every real input count we hold
        # describes a history that is about to stop existing — including ctx's
        # fold projection, which was taken before the compaction and is frozen
        # for the rest of the turn. Keeping either would pin the trigger to a
        # stale pre-compaction high: the next step would re-fire on a history
        # that just shrank and then die on ``compaction_no_progress`` (its
        # boundary can no longer advance). Invalidate both and let the pure
        # estimate carry the trigger until the next round-trip records a count
        # for the NEW history.
        state.last_input_tokens_at_call = _BASELINE_INVALIDATED
        return CompactionRequestedDecision(
            reason=reason,
            estimated_tokens=estimated,
            summary=summary,
            boundary_count=boundary,
            composer_version=self._composer_version,
            reattached=self._recent_files_to_reattach(history, boundary),
        )

    def _summarize(
        self, ctx: StepContext, view: View, to_summarize: list[Message]
    ) -> tuple[LLMResponse, str]:
        """One summarize round-trip over ``to_summarize``, with its one
        tool-free retry; returns the response and its joined text."""
        system = view.segments[0].content[0] if view.segments else None
        context = view.segments[1].content if len(view.segments) > 1 else ()
        # The summarize round-trip is not user-facing output: opt out of
        # token streaming so a live UI never previews compaction internals.
        # The first attempt carries no ``tool_choice`` so Anthropic serves the
        # message tier from the main loop's cache entry too.
        summary_resp = self._llm.complete(
            self._summary_prompt_request(
                to_summarize,
                view.provider_tool_schemas,
                system=system,
                context=context,
            ),
            ctx,
            allow_stream=False,
        )
        summary = _joined_text(summary_resp)
        if _summary_needs_tool_free_retry(summary_resp, summary):
            # The model answered with a tool call (or no text at all): retry
            # exactly once with ``tool_choice: none``. The retry is an ordinary
            # recorded round-trip (Started / Recorded / Finished), and whether
            # it happens is a pure function of the recorded first response, so
            # a resumed run takes the same branch. Whatever the retry returns
            # goes through the guards in ``_compaction_decision`` unchanged.
            summary_resp = self._llm.complete(
                self._summary_prompt_request(
                    to_summarize,
                    view.provider_tool_schemas,
                    system=system,
                    context=context,
                    forbid_tools=True,
                ),
                ctx,
                allow_stream=False,
            )
            summary = _joined_text(summary_resp)
        return summary_resp, summary

    def _recent_files_to_reattach(
        self, history: Sequence[Message], boundary: int
    ) -> tuple[str, ...]:
        """Claude Code's post-compaction file restore: the current content of
        the files most recently read or written, so the model does not have to
        re-read what it was just working on.

        The newest :data:`POST_COMPACT_MAX_FILES_TO_RESTORE` paths a
        successful ``Read`` / ``Edit`` / ``Write`` touched, skipping a file
        whose ``Read`` the kept tail (``history[boundary:]``) already shows and
        the instruction files the instructions resident already carries, are
        re-read through the ``Read`` tool (so a sandboxed workspace is read
        where it lives). Each becomes Claude Code's call/result pair, or its
        too-large note past :data:`POST_COMPACT_MAX_TOKENS_PER_FILE`; pairs are
        kept newest first while the total stays within
        :data:`POST_COMPACT_TOKEN_BUDGET` — and within the protected tail
        budget, so a small window is never refilled by the restore alone.
        The texts ride the decision and are recorded, so a resume replays
        them and never reads the disk.
        """
        read_tool = self._tools.get(_READ_TOOL)
        if read_tool is None or self._content_store is None:
            return ()
        succeeded = {
            block.call_id
            for message in history
            if message.role == "tool"
            for block in message.content
            if isinstance(block, ToolResultBlock) and block.success
        }
        kept_reads = {
            path
            for message in history[boundary:]
            for path, tool_name in _touched_files(message, succeeded)
            if tool_name == _READ_TOOL
        }
        recent: list[str] = []
        for message in reversed(history):
            for path, _tool_name in reversed(_touched_files(message, succeeded)):
                if (
                    path in recent
                    or path in kept_reads
                    or _basename(path) in _INSTRUCTION_FILE_NAMES
                ):
                    continue
                recent.append(path)
        budget = POST_COMPACT_TOKEN_BUDGET
        if self._tail_token_budget > 0:
            budget = min(budget, self._tail_token_budget)
        tool_ctx = ToolContext(artifact_store=self._content_store)
        reattached: list[str] = []
        used = 0
        for path in recent[:POST_COMPACT_MAX_FILES_TO_RESTORE]:
            arguments = {"file_path": path}
            try:
                result = read_tool.invoke(arguments, tool_ctx)
            except Exception:  # noqa: BLE001 — a failed restore is skipped
                continue
            if not result.success or result.images or not isinstance(
                result.output, str
            ):
                continue
            if _estimate_text(result.output) > POST_COMPACT_MAX_TOKENS_PER_FILE:
                texts = [
                    f"Note: {path} was read before the last conversation was "
                    f"summarized, but the contents are too large to include. "
                    f"Use {_READ_TOOL} tool if you need to access it."
                ]
            else:
                texts = [
                    f"Called the {_READ_TOOL} tool with the following input: "
                    + json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                    f"Result of calling the {_READ_TOOL} tool:\n{result.output}",
                ]
            size = sum(_estimate_text(text) for text in texts)
            if used + size > budget:
                continue
            used += size
            reattached.extend(texts)
        return tuple(reattached)

    def _tail_budget_in_estimate_units(
        self, state: _TaskTriggerBaseline
    ) -> int:
        """``tail_token_budget`` converted into the unit ``_summary_boundary``
        actually accumulates in.

        The two knobs from ``derive_compaction_config`` are in DIFFERENT units,
        which is the whole trap. ``context_window`` (and therefore
        ``tail_token_budget = available // 3``) counts REAL provider tokens;
        ``estimate_messages_tokens`` counts chars/4. They coincide only while
        the heuristic is accurate. When the payload tokenises denser than
        4 chars/token, they silently diverge — and comparing a chars/4
        accumulator against a real-token budget then protects a tail far larger
        than intended: at a measured 4x density the WHOLE history fits inside
        the "tail", the boundary cannot advance, and a legitimately triggered
        compaction dies on ``compaction_no_progress``.

        Before the real-usage baseline reached the trigger this was invisible:
        BOTH sides read chars/4 against real-token knobs, i.e. consistently
        wrong, so they still agreed with each other (compaction just fired late
        or never). Correcting only the trigger breaks that accidental symmetry,
        so the tail must be converted with the same observed density.

        ``_observed_density`` is ``1.0`` until a round-trip has been recorded,
        which reproduces the pre-existing arithmetic exactly — an unobserved
        session keeps today's byte-equal behaviour.
        """
        return int(self._tail_token_budget / self._observed_density(state))

    def _observed_density(self, state: _TaskTriggerBaseline) -> float:
        """REAL provider tokens per chars/4 estimated token, as measured on
        this task's last main round-trip (``1.0`` before there is one).

        Both operands are already-recorded numbers pinned at the same call
        site, so a resumed run re-derives the same ratio — we never measure
        live. ``_BASELINE_INVALIDATED`` (post-compaction) also yields ``1.0``:
        with no count describing the collapsed history, the honest fallback is
        to trust the estimate as written.

        Clamped to ``[_DENSITY_MIN, _DENSITY_MAX]`` for the reason those
        constants document: the numerator is a PROVIDER-reported count, and a
        gateway that reports a nonsense one (a flat ``input_tokens=10`` for a
        multi-thousand-token request) drives the ratio toward zero, which
        divides ``tail_token_budget`` by an arbitrary factor and inflates the
        protected tail past the whole history — the boundary then cannot
        advance and every compaction dies on ``compaction_no_progress``. The
        clamp bounds the damage to a factor of four in either direction, which
        the tail budget absorbs.
        """
        if state.last_estimate_at_call <= 0:
            return 1.0
        if state.last_input_tokens_at_call <= 0:
            return 1.0
        ratio = (
            state.last_input_tokens_at_call / state.last_estimate_at_call
        )
        return min(_DENSITY_MAX, max(_DENSITY_MIN, ratio))

    def _summary_boundary(
        self, history: list[Message], state: _TaskTriggerBaseline
    ) -> int:
        """How many leading RAW-history messages to collapse — everything older
        than the protected tail window (sized by ``tail_token_budget``).

        ``history`` is ``view.rolling_history`` (raw ``task.runtime.messages``),
        so the returned index lives in the SAME coordinate space the Composer's
        ``_apply_summary`` slices (``ContextState.summary_boundary``) — see
        finding 2. Walks from the newest message backward accumulating the
        estimate; once it exceeds the tail budget, every older message is
        summarized. Deterministic + provider-neutral (D-3d). Mirrors the
        Composer's ``_prune_tail`` cutoff so the two stay consistent.

        **Tool-pair alignment**: the raw token cutoff can fall between an
        ``assistant`` turn carrying a ``ToolUseBlock`` and the ``role="tool"``
        message carrying its result. Slicing there would (a) leave
        ``history[:boundary]`` ending on an unmatched ``tool_use`` — the
        summarize request itself is then a dangling function call the provider
        rejects with a 400 — and (b) make ``history[boundary:]`` (the kept tail)
        begin with an orphan ``tool_result``, which the next compose→decide
        request also 400s on. We snap the cutoff FORWARD past any leading
        ``tool``-result messages so the whole exchange lands in the collapsed
        prefix and the kept tail starts on a self-contained turn. Forward-only
        keeps the boundary monotonic, so the anti-spiral progress guarantee
        (``boundary > view.summary_boundary``) is preserved.
        """
        budget = self._tail_budget_in_estimate_units(state)
        if budget <= 0:
            return 0
        acc = 0
        cutoff = len(history)
        for i in range(len(history) - 1, -1, -1):
            acc += estimate_messages_tokens([history[i]])
            if acc > budget:
                cutoff = i + 1
                break
            cutoff = i
        # Snap forward off any tool-result message: its matching ``tool_use``
        # sits in the collapsed prefix, so the result must travel with it.
        while cutoff < len(history) and _carries_tool_result(history[cutoff]):
            cutoff += 1
        return cutoff

    # ------------------------------------------------------------------
    # Request building
    # ------------------------------------------------------------------

    def _build_request_and_selection(
        self, view: View
    ) -> tuple[LLMRequest, Optional[MessageSelection]]:
        # Composer is the SoT for prompt material —
        # ReActPolicy pulls system / tools / messages from the View's
        # three segments.
        #
        # The message-count tail-window truncation is
        # DEFAULT-OFF (``max_history_messages=None``): a guard that
        # drops the oldest messages once the count crosses a limit would be
        # decoupled from tokens and fire far earlier than the token
        # compaction gate, bluntly dropping early context before the summariser
        # ran. Pure token compaction (③ ``_compaction_triggered``) is
        # the only gate; the protected tail is sized by ``tail_token_budget``,
        # not a message count. The parameter is KEPT as an escape hatch: pass a
        # positive int to enable count-based tail truncation.
        #
        # This is the one place message selection happens, so it is the
        # single source of the ``MessageSelection`` provenance — but only when
        # the escape hatch is engaged. With the hatch off (``None``) we drop
        # nothing and emit NO ``tail_window`` selection (its absence is the
        # signal that no count-based truncation happened).
        system = view.segments[0].content[0]
        tools = list(view.provider_tool_schemas)
        history = view.iter_messages()
        selection: Optional[MessageSelection] = None
        if self._max_history_messages is not None:
            candidates = len(history)
            if candidates > self._max_history_messages:
                history = history[-self._max_history_messages :]
            selection = MessageSelection(
                strategy="tail_window",
                candidates=candidates,
                selected=len(history),
                dropped=candidates - len(history),
                limit=self._max_history_messages,
            )
        request = LLMRequest(
            model=self._model,
            messages=history,
            tools=tools,
            system=system,
            # Forward the model's output ceiling (catalog ModelSpec.max_output_tokens,
            # injected by the host for compaction math) as the request's hard cap, so a
            # gateway with a stingy default (the aidp Responses gateway caps at 1000 when
            # the client sends none) doesn't cut every answer into output-limit
            # recoveries. ``0`` (host didn't inject)
            # ⇒ ``None`` ⇒ omitted from canonical bytes, keeping the prompt
            # prefix identical whether or not it is set.
            max_tokens=self._max_output_tokens or None,
            # These three are omitted from canonical bytes when None
            # (LLMRequest.__canonical_omit_none__) so the prompt prefix is
            # unchanged whether or not the host set them (prefix-cache friendly).
            output_schema=self._output_schema,
            thinking=self._thinking,
            effort=self._effort,
        )
        return request, selection

    # ------------------------------------------------------------------
    # Response translation
    # ------------------------------------------------------------------

    def _response_to_decision(
        self, response: LLMResponse, view: View
    ) -> Decision:
        history_content = _strip_thinking(response.content)
        # A cut response's complete tool calls run like any others (the
        # adapters already dropped a call whose arguments the cap cut off), and
        # the tool round that follows resumes the work.
        if response.stop_reason == "tool_use" or (
            response.stop_reason == "max_tokens"
            and any(isinstance(b, ToolUseBlock) for b in history_content)
        ):
            assistant_message = Message(
                role="assistant", content=history_content
            )
            # B3: route any enabled control-tool call (ask_user_question →
            # todo_write → spawn_subagent → skill → run_workflow →
            # recall_history, in that fixed order) through the single
            # translation seam. ``None`` means no control tool matched — fall
            # through to the normal tool_calls path. The View rides along so a
            # translate can answer from folded state (RecallHistory renders
            # the collapsed prefix).
            control = translate_control_tool(
                response,
                assistant_message,
                specs=self._control_translate_specs,
                content_store=self._content_store,
                view=view,
            )
            # A call to a routing tool (``McpCall``) becomes a call to the
            # tool it names here, whichever translate built the batch.
            if control is not None:
                return route_tool_calls(control, self._tools)
            calls = [
                ToolCall(
                    call_id=block.call_id,
                    tool_name=block.tool_name,
                    arguments=dict(block.arguments),
                )
                for block in response.content
                if isinstance(block, ToolUseBlock)
            ]
            # Extended-thinking end-to-end (Slice B): carry the thinking the
            # LLM emitted ahead of these tool_use blocks OUT-OF-BAND on the
            # Decision (it is NOT in ``assistant_message`` — that stays
            # thinking-free so ``runtime.messages`` never absorbs the
            # non-deterministic signature). The Engine records it under the
            # turn's first tool_use call_id so the next compose can re-attach it
            # on an Anthropic continuation request.
            thinking = tuple(
                b for b in response.content if isinstance(b, ThinkingBlock)
            )
            return route_tool_calls(
                ToolCallsDecision(
                    calls=calls,
                    assistant_message=assistant_message,
                    assistant_thinking=thinking,
                ),
                self._tools,
            )
        if response.stop_reason == "end_turn":
            # An ``end_turn`` with no renderable content (e.g. a safety-classifier
            # ``refusal`` whose content array came back empty, now mapped to
            # ``end_turn``) would record an empty-content assistant Message.
            # Anthropic rejects ``{"role":"assistant","content":[]}`` with a 400
            # on the next request (deterministic on resume), so fail cleanly here
            # instead of polluting history with an unsendable turn.
            if not history_content:
                return FailDecision(
                    reason="llm_empty_response",
                    retryable=False,
                    assistant_message=None,
                )
            # A response that ends a run of output-limit continuations is
            # only the last piece of the answer: the earlier pieces are the
            # recorded assistant turns between the resume nudges.
            answer = "".join(
                (
                    *_continued_texts(view.rolling_history),
                    "\n".join(
                        block.text
                        for block in response.content
                        if isinstance(block, TextBlock)
                    ),
                )
            )
            # Structured output: when the caller declared a JSON Schema
            # via ``output_schema`` we try to parse the concatenated text
            # as JSON. On success ``FinishDecision.answer`` becomes the
            # parsed object (dict/list/etc.); on failure we fall back
            # to the raw string so the task never fails purely on
            # a malformed model response. ``json.loads`` is deterministic
            # so a resumed run re-derives the same parsed answer.
            if self._output_schema is not None:
                try:
                    parsed: object = json.loads(answer)
                except (ValueError, TypeError):
                    pass
                else:
                    answer = parsed  # type: ignore[assignment]
            return FinishDecision(
                answer=answer,
                assistant_message=Message(
                    role="assistant", content=history_content
                ),
            )
        if response.stop_reason == "max_tokens":
            return self._output_limit_decision(history_content, view)
        # stop_reason == "error".
        return self._error_to_decision(response)

    def _output_limit_decision(
        self, history_content: list[Block], view: View
    ) -> Decision:
        """A response the output cap cut with no tool call in it.

        Up to :data:`MAX_OUTPUT_TOKENS_RECOVERY` times in a row the turn
        continues: the partial content is recorded as the assistant turn and a
        user turn asks the model to resume (when only thinking came back —
        never recorded — nothing is kept and the nudge says so). Past the cap
        the turn fails, keeping whatever text the last response produced; a
        response with nothing renderable records no assistant turn, since a
        ``content: []`` assistant message is a provider 400 on the next call.
        """
        if _output_limit_recoveries(view.rolling_history) >= (
            MAX_OUTPUT_TOKENS_RECOVERY
        ):
            if not history_content:
                return FailDecision(
                    reason="llm_empty_response",
                    retryable=False,
                    assistant_message=None,
                )
            return FailDecision(
                reason="llm_truncated",
                retryable=True,
                assistant_message=Message(
                    role="assistant", content=history_content
                ),
            )
        nudge = (
            OUTPUT_LIMIT_RESUME_NUDGE
            if history_content
            else OUTPUT_LIMIT_THINKING_NUDGE
        )
        return StatePatchDecision(
            messages_before=(
                (Message(role="assistant", content=history_content),)
                if history_content
                else ()
            ),
            messages_after=(
                Message(role="user", content=[TextBlock(text=nudge)]),
            ),
        )

    def _error_to_decision(self, response: LLMResponse) -> Decision:
        """Map an error ``LLMResponse`` to a Decision by neutral category.

        ② error recovery: the runtime stamps a neutral error *category*
        into ``raw['category']``. Transient retries are already consumed
        inside the LLM client (LIVE-only, README D-2d), so Policy never sees
        ``transient`` on the happy path; if one ever arrives the budget is
        already spent, so it is treated as a plain (non-retryable) error.

        This slice owns only the ``fatal`` branch → non-retryable
        ``FailDecision``. The ``overflow`` branch (→ compaction re-continue)
        is deliberately NOT handled here; it is owned by ③ (memory
        management), which will add an ``overflow`` arm to this method
        before the fatal / fall-through arms. An absent category (old
        recordings) or any other value falls through to the same
        ``llm_error`` FailDecision — backward compatible and loop-safe.

        We do not attach an assistant_message so the rolling history is not
        polluted by a failed turn.
        """
        # ``fatal`` and an absent/unrecognised category both map to the same
        # non-retryable ``llm_error`` (the ``overflow`` category never reaches
        # here — the passive trigger in ``decide`` intercepts it before
        # ``_response_to_decision``). Kept as one arm so the identity is
        # explicit rather than a duplicated branch. We attach no
        # assistant_message so a failed turn does not pollute the rolling
        # history. The provider's own error text rides along as ``detail`` so
        # a host reading the failure sees *why* (a 401, a 400 body, a content
        # filter) instead of a bare ``llm_error``.
        return FailDecision(
            reason="llm_error",
            retryable=False,
            assistant_message=None,
            detail=_provider_error_detail(response),
        )


def _touched_files(
    message: Message, succeeded: set[str]
) -> list[tuple[str, str]]:
    """``(file_path, tool name)`` of each successful file-tool call in an
    assistant message, in block order."""
    if message.role != "assistant":
        return []
    touched: list[tuple[str, str]] = []
    for block in message.content:
        if (
            isinstance(block, ToolUseBlock)
            and block.tool_name in _FILE_TOOLS
            and block.call_id in succeeded
        ):
            path = block.arguments.get("file_path")
            if isinstance(path, str) and path:
                touched.append((path, block.tool_name))
    return touched


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _estimate_text(text: str) -> int:
    return estimate_messages_tokens(
        [Message(role="user", content=[TextBlock(text=text)])]
    )


def _output_limit_recoveries(history: Sequence[Message]) -> int:
    """How many output-limit nudges the current run of cut responses has had.

    Walks back from the newest message over nudges and the text-only
    assistant turns between them; a tool round or any other user message ends
    the run, which is when Claude Code resets its count too. A pure function
    of the recorded history, so a resumed turn counts what the live one did.
    """
    count = 0
    for message in reversed(history):
        if message.role == "assistant" and not any(
            isinstance(b, ToolUseBlock) for b in message.content
        ):
            continue
        if (
            message.role == "user"
            and len(message.content) == 1
            and isinstance(message.content[0], TextBlock)
            and message.content[0].text in _OUTPUT_LIMIT_NUDGES
        ):
            count += 1
            continue
        break
    return count


def _continued_texts(history: Sequence[Message]) -> list[str]:
    """The texts of the cut responses an answer continues, oldest first.

    Walks back over the run :func:`_output_limit_recoveries` counts — each
    resume nudge, and the text-only assistant turn it follows when the cut
    response had text — and returns those turns' texts. Empty when the
    history does not end on a nudge. Pure over the recorded history.
    """
    texts: list[str] = []
    i = len(history) - 1
    while i >= 0 and _is_output_limit_nudge(history[i]):
        i -= 1
        previous = history[i] if i >= 0 else None
        if (
            previous is not None
            and previous.role == "assistant"
            and not any(isinstance(b, ToolUseBlock) for b in previous.content)
        ):
            texts.append(
                "\n".join(
                    b.text for b in previous.content if isinstance(b, TextBlock)
                )
            )
            i -= 1
    texts.reverse()
    return texts


def _is_output_limit_nudge(message: Message) -> bool:
    return (
        message.role == "user"
        and len(message.content) == 1
        and isinstance(message.content[0], TextBlock)
        and message.content[0].text in _OUTPUT_LIMIT_NUDGES
    )


def _provider_error_detail(response: LLMResponse) -> Optional[str]:
    """The provider's error text for an error ``LLMResponse``, if it has one.

    The runtime stamps ``raw['error']`` when it translates a provider
    exception; an adapter-produced ``stop_reason="error"`` (an unknown vendor
    stop reason) carries none, and ``None`` means "no detail".
    """
    raw = response.raw if isinstance(response.raw, dict) else {}
    error = raw.get("error")
    if isinstance(error, str) and error:
        return error
    return None


def _strip_thinking(content: list[Block]) -> list[Block]:
    """Drop ``ThinkingBlock`` from a Decision-bound assistant turn.

    Reasoning models emit a ``ThinkingBlock.text`` that is non-
    deterministic across runs even at temperature=0; round-tripping it
    through ``RuntimeState.messages`` propagates that drift into every
    subsequent ``LLMRequest``, perturbing the prompt prefix (defeating the
    provider's prefix cache) and making a resumed run diverge from the
    original.
    OpenAI reasoning models chain reasoning server-side and do not
    require ``reasoning_content`` echoed back, so dropping is safe.

    Anthropic extended-thinking signature round-trip is not handled
    here — the Anthropic adapter and ContextComposer three-segment path
    own it.
    """
    return [b for b in content if not isinstance(b, ThinkingBlock)]


# ---------------------------------------------------------------------------
# safety/permission constraints must survive compaction verbatim
# ---------------------------------------------------------------------------

#: Trigger phrases that mark a line as a safety/permission directive (the
#: "don't touch this file" class). A line is treated as a constraint iff
#: (case-insensitively) it contains any of these. English forms cover the
#: imperative prohibitions Claude Code's Security Monitor keys on; the Chinese
#: forms cover the project's own working language (CLAUDE.md: replies in
#: Chinese). The list is intentionally narrow — we want HIGH precision so we
#: never bloat a summary with benign chatter, and a missed phrase only degrades
#: to the model's best effort (the prompt rule), never breaks the summary.
#: Lower-cased once at module load.
_CONSTRAINT_TRIGGERS: tuple[str, ...] = (
    # English
    "do not touch",
    "don't touch",
    "do not modify",
    "don't modify",
    "do not edit",
    "don't edit",
    "do not access",
    "don't access",
    "do not delete",
    "don't delete",
    "never edit",
    "never modify",
    "never touch",
    "never access",
    "never delete",
    "must not",
    "you may not",
    "not allowed to",
    "forbidden",
    "off-limits",
    "off limits",
    # Chinese
    "禁止",
    "不得",
    "不准",
    "不要修改",
    "不要碰",
    "不要动",
    "不要访问",
    "不要删除",
    "别碰",
    "别动",
    "严禁",
    "切勿",
    "勿",
)


#: List-bullet markers stripped off a detected constraint line before it is
#: recorded. The summarize input begins with the PREVIOUS summary whenever one
#: exists, and a constraint the previous model dropped was re-injected there as
#: ``- <line>`` by :func:`enforce_verbatim_constraints`. Without this
#: normalisation each pass would detect the bulleted form, find it missing from
#: the fresh note, and re-inject it one bullet deeper (``- - <line>``,
#: ``- - - <line>``, …). Stripping the marker makes the round-trip idempotent:
#: the detected text is the constraint itself, which the re-injected block
#: contains verbatim, so the next pass matches and adds nothing. Pure and
#: deterministic; a line that carries no marker is unaffected.
_BULLET_PREFIXES: tuple[str, ...] = ("- ", "* ", "+ ")


def _line_is_constraint(line: str) -> bool:
    """True iff ``line`` carries a safety/permission directive trigger.

    Pure + case-insensitive. The trigger phrases are matched as substrings
    so "Do NOT touch X" and the Chinese "请不得修改 Y" both fire regardless of
    casing or surrounding text.
    """
    low = line.lower()
    return any(trigger in low for trigger in _CONSTRAINT_TRIGGERS)


def extract_safety_constraints(messages: list[Message]) -> list[str]:
    """Return the verbatim safety/permission directive lines in ``messages``.

    A "don't touch this file" / "do not touch X" instruction must keep
    binding the session after its turn is collapsed into a compaction summary.
    This is the detector — it walks every ``TextBlock`` of every message the
    model did not write, splits on newlines, and keeps each line whose text
    contains a constraint trigger
    (:data:`_CONSTRAINT_TRIGGERS`), STRIPPED of surrounding whitespace and of a
    leading list bullet (:data:`_BULLET_PREFIXES`) but otherwise verbatim (so
    the exact wording — including the path it protects — is preserved for
    re-injection).

    Pure + deterministic + provider-neutral: same messages → same list, no
    clock / randomness / network, no provider field. Order is first-appearance;
    duplicates are dropped so a constraint repeated across turns is re-injected
    only once. The summary post-check (:func:`enforce_verbatim_constraints`) and
    the model-facing prompt rule both build on this single detector.

    Source-restricted, like the prompt rule: a ``ToolResultBlock`` is never
    read, neither is an ``assistant`` message, and neither is a HOST-INJECTED
    turn (:func:`is_host_injected` — ``origin`` ``"system"`` / ``"memory"``).
    The model restating a "never edit Y" it found in a web page must not turn
    that text into a constraint that outlives every compaction, and neither
    must a background job's stdout, a sub-agent's notice, an attachment or a
    recalled memory body — all of which stand in for text from elsewhere while
    riding the user channel. A constraint the user stated is detected in the
    user's own message (``origin`` ``None`` / ``"human"``), and the compaction
    summary re-enters with ``origin`` UNSET, so a constraint preserved once
    survives every later compaction.
    """
    seen: set[str] = set()
    out: list[str] = []
    for msg in messages:
        if msg.role == "assistant" or is_host_injected(msg):
            continue
        for block in msg.content:
            if not isinstance(block, TextBlock):
                continue
            for raw_line in block.text.split("\n"):
                line = raw_line.strip()
                for bullet in _BULLET_PREFIXES:
                    if line.startswith(bullet):
                        line = line[len(bullet) :].strip()
                        break
                if not line or not _line_is_constraint(line):
                    continue
                if line in seen:
                    continue
                seen.add(line)
                out.append(line)
    return out


#: Heading prepended to the re-injected constraint block so a reader (and the
#: next compaction pass, which re-detects from raw history not the summary)
#: can tell these lines are preserved-verbatim safety rules, not prose.
_VERBATIM_HEADER = "PRESERVED SAFETY/PERMISSION CONSTRAINTS (verbatim):"


def enforce_verbatim_constraints(
    summary: str, constraints: list[str]
) -> str:
    """Guarantee every constraint appears verbatim in ``summary``.

    Deterministic post-check: the summarize model is *asked* to keep
    safety/permission directives word-for-word, but cannot be trusted to. So
    after the model returns, any constraint from
    :func:`extract_safety_constraints` that is NOT already a verbatim substring
    of ``summary`` is appended back under a clearly-labelled block. We only ADD
    (never rewrite the model's prose), so the model's own summary is preserved
    in full and the lost rule is restored character-for-character.

    Pure over ``(summary, constraints)`` — no clock / randomness / IO — so the
    summary the policy records is a deterministic function of the recorded model
    response + the (deterministic) detected constraints, so a resumed run
    rebuilds the identical summary. A constraint already present verbatim is a no-op
    (idempotent: re-running never double-injects), and an empty constraint list
    returns ``summary`` unchanged (no spurious header).
    """
    missing = [c for c in constraints if c not in summary]
    if not missing:
        return summary
    block = "\n".join(f"- {c}" for c in missing)
    return f"{summary}\n\n{_VERBATIM_HEADER}\n{block}"


# ---------------------------------------------------------------------------
# Summarize response gate
# ---------------------------------------------------------------------------

#: The nine section titles ``summarize.md`` asks the note to carry, in prompt
#: order. :func:`looks_like_summary_note` counts them; a test pins every title
#: to the prompt text so the gate and the prompt cannot drift apart.
_SUMMARY_SECTION_TITLES: tuple[str, ...] = (
    "Primary Request & Intent",
    "Key Technical Concepts",
    "Files & Code",
    "Errors & Fixes",
    "All user messages",
    "Pending Tasks",
    "Decisions & Constraints",
    "Current Work",
    "Next Step",
)

#: How many section titles a response must show before it is recorded as a
#: note. Two rather than one: a narration can mention "the primary request"
#: in passing, while a note for even the shortest conversation carries at
#: least sections 1 and 5.
_SUMMARY_MIN_SECTIONS = 2

#: A localised note's section heading: a markdown heading or a bold run that
#: opens with the section's number — ``## 1. 主要请求与意图``, ``**3) 文件**``.
#: A bare ``1. 主要请求`` does not match: that is also what a narration's
#: numbered plan looks like, and the gate fails closed.
_LOCALISED_SECTION_HEADING = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*|\*\*[ \t]*)([1-9])[ \t]*[.)、．:：]", re.MULTILINE
)

#: Longest excerpt a failure reason quotes. The reason rides an inline event
#: payload with a 4 KB ceiling, and a host renders it as one line.
_FAILURE_EXCERPT_CHARS = 160


def looks_like_summary_note(text: str) -> bool:
    """Whether ``text`` has the shape ``summarize.md`` asks for.

    The check is deliberately structural, not semantic: at least
    :data:`_SUMMARY_MIN_SECTIONS` of the prompt's section titles appear
    (case-insensitive, markdown decoration around them ignored by construction
    since only the title words are matched), or at least that many distinct
    section numbers open a markdown heading or a bold run
    (:data:`_LOCALISED_SECTION_HEADING`) — a note whose headings the model
    translated (``## 1. 主要请求与意图``) is still a note, whatever the
    language. It separates a note from the one failure the prompt cannot
    prevent — a model that ignores the instruction and answers with what it
    would have said next ("The file is long and got cut off. Fetching the
    rest.") — which is otherwise indistinguishable from a short but genuine
    note by length or stop reason. A plain numbered line (``1. 主要请求``)
    counts for neither: that is also the shape of a narration's plan. The
    failure reason quotes the head of a refused reply so a new shape is
    diagnosable. Pure over ``text`` so a resumed run takes the same branch.
    """
    lowered = text.lower()
    found = sum(
        1 for title in _SUMMARY_SECTION_TITLES if title.lower() in lowered
    )
    if found >= _SUMMARY_MIN_SECTIONS:
        return True
    numbered = {m.group(1) for m in _LOCALISED_SECTION_HEADING.finditer(text)}
    return len(numbered) >= _SUMMARY_MIN_SECTIONS


def _excerpt(text: str, limit: int = _FAILURE_EXCERPT_CHARS) -> str:
    """One-line, whitespace-collapsed excerpt for a failure reason."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _joined_text(response: LLMResponse) -> str:
    """The response's text blocks joined by newlines (the summary candidate)."""
    return "\n".join(
        b.text for b in response.content if isinstance(b, TextBlock)
    )


def _as_composed(messages: list[Message], view: View) -> list[Message]:
    """``messages`` with every tool result the composed View shows cleared
    replaced by the View's block, so the summarizer reads what the main loop
    sent. Pure over the two."""
    composed = {
        block.call_id: block
        for message in (view.segments[2].content if len(view.segments) > 2 else ())
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    out: list[Message] = []
    for message in messages:
        blocks = [
            composed[b.call_id]
            if isinstance(b, ToolResultBlock)
            and b.call_id in composed
            and composed[b.call_id].output != b.output
            else b
            for b in message.content
        ]
        if all(new is old for new, old in zip(blocks, message.content)):
            out.append(message)
        else:
            out.append(
                Message(role=message.role, content=blocks, origin=message.origin)
            )
    return out


def _is_overflow(response: LLMResponse) -> bool:
    return (
        response.stop_reason == "error"
        and (response.raw or {}).get("category") == CATEGORY_OVERFLOW
    )


def _drop_oldest_rounds(
    messages: list[Message], *, keep_first: bool
) -> Optional[list[Message]]:
    """``messages`` less the oldest fifth (at least one) of its rounds, behind
    the truncation marker; ``None`` when there is no round left to drop.

    A round opens at every message that is not a tool result, so a dropped
    round takes its tool results with it and none is left without its call.
    ``keep_first`` holds the first message (the previous note) in place. A
    marker from an earlier retry is replaced, not stacked.
    """
    head = messages[:1] if keep_first else []
    body = messages[1:] if keep_first else list(messages)
    if body and _is_truncation_marker(body[0]):
        body = body[1:]
    rounds: list[list[Message]] = []
    for message in body:
        if message.role == "tool" and rounds:
            rounds[-1].append(message)
        else:
            rounds.append([message])
    if len(rounds) < 2:
        return None
    kept = rounds[max(1, len(rounds) // 5) :]
    marker = Message(role="user", content=[TextBlock(text=_SUMMARY_TRUNCATION_MARKER)])
    return [*head, marker, *(m for r in kept for m in r)]


def _is_truncation_marker(message: Message) -> bool:
    return (
        message.role == "user"
        and len(message.content) == 1
        and isinstance(message.content[0], TextBlock)
        and message.content[0].text == _SUMMARY_TRUNCATION_MARKER
    )


def _summary_needs_tool_free_retry(response: LLMResponse, text: str) -> bool:
    """Whether a first summarize attempt sent without ``tool_choice`` must be
    retried once with ``tool_choice: none``.

    True when the model answered with any tool call, or with no usable text.
    An errored round-trip is not retried here: the LLM client has already
    spent its transient retries, and forbidding tools does not change an
    error. A text reply that is not a note is not retried either — the note
    gate handles it. Pure over ``(response, text)``.
    """
    if response.stop_reason == "error":
        return False
    if any(isinstance(b, ToolUseBlock) for b in response.content):
        return True
    return not text.strip()


def _summary_failure(response: LLMResponse, text: str) -> Optional[str]:
    """Why ``response`` must not be recorded as a note, or ``None`` when it may.

    ``text`` is the joined text of the response's text blocks. The returned
    string is the human-facing detail of the failure reason: which of the
    three refusals fired and the facts a reader needs to tell them apart —
    the error category and message for an errored round-trip; the stop
    reason, output token count and block kinds for an empty one (an
    only-thinking response and a zero-content response look identical
    without the block list); and the same counts plus the head of the text
    for a reply that is not a note. Pure over ``(response, text)``.
    """
    if response.stop_reason == "error":
        raw = response.raw if isinstance(response.raw, dict) else {}
        category = raw.get("category") or "unknown"
        error = _excerpt(str(raw.get("error") or ""))
        return (
            f"errored summarize round-trip (category={category}"
            + (f", error={error!r}" if error else "")
            + ")"
        )
    facts = (
        f"stop_reason={response.stop_reason}, "
        f"output_tokens={response.usage.output}"
    )
    if not text.strip():
        kinds = ", ".join(type(b).__name__ for b in response.content) or "none"
        return f"empty summarize response ({facts}, blocks={kinds})"
    if not looks_like_summary_note(text):
        return (
            f"summarize response is not a note ({facts}, "
            f"text_len={len(text)}, head={_excerpt(text, 80)!r})"
        )
    return None
