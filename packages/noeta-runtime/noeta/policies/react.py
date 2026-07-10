"""ReActPolicy — Phase 1 first-slice ReAct loop policy.

The first real ``Policy`` shipped with Noeta: bridges a typed
``LLMRequest / LLMResponse`` round-trip into the typed ``Decision``
surface the Engine consumes. The hot path is::

    LLM produced tool_use      → ToolCallsDecision(calls, assistant_message)
    LLM produced end_turn      → FinishDecision(answer, assistant_message)
    LLM produced max_tokens    → FailDecision(reason="llm_truncated",
                                              retryable=True, ...)
    LLM produced error         → FailDecision(reason="llm_error",
                                              retryable=False,
                                              assistant_message=None)
    step counter at ceiling    → FailDecision(reason="react_max_steps_exceeded",
                                              retryable=False)

The Engine writes the assistant message to ``RuntimeState.messages``
(single writer); ReActPolicy never touches RuntimeState
directly. The full LLM content (Thinking + Text + ToolUse blocks,
whatever the provider returned) round-trips through
``assistant_message`` so reasoning models do not lose their
``ThinkingBlock`` continuation between turns.

Scope:

* Only three Decision variants are produced —
  ``ToolCallsDecision / FinishDecision / FailDecision``. Spawn-subtask /
  yield-for-human / wait-timer translation is explicitly Out of Scope and
  lands in Phase 1 second-slice.
* ``model`` is a constant per Policy instance. Future upgrade to a
  ``Callable[[View], str]`` is pure addition.
* History truncation is a trivial tail window; the real three-segment
  ContextComposer arrives in issue 14.
* ``_step_count`` is an instance attribute — **one Policy instance per
  Task**; a subtask uses its own Policy with its own counter.

Layering: this module imports only ``noeta.protocols.*`` and
``noeta.runtime.*``. It never imports ``noeta.providers.*`` (provider
injection happens at the call site, behind a ``RuntimeLLMClient``) and
never imports ``noeta.testing.*`` (per ``production-cannot-import-testing``).
"""

from __future__ import annotations

import json
from typing import Any, Optional, Protocol

from noeta.policies._control_translate import (
    SKILL_TOOL,
    SPAWN_SUBAGENT_TOOL,
    ControlToggles,
    spawn_subagent_tool_schema,
    translate_control_tool,
)
from noeta.policies.control_tools import (
    ASK_USER_QUESTION_TOOL,
    TODO_WRITE_STATUSES,
    TODO_WRITE_TOOL,
    ask_user_question_tool_schema,
    skill_tool_schema,
    todo_write_tool_schema,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import (
    CompactionRequestedDecision,
    Decision,
    FailDecision,
    FinishDecision,
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
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.tool import Tool
from noeta.protocols.view import View


__all__ = [
    "ReActPolicy",
    "SPAWN_SUBAGENT_TOOL",
    "TODO_WRITE_TOOL",
    "TODO_WRITE_STATUSES",
    "ASK_USER_QUESTION_TOOL",
    "SKILL_TOOL",
    "ask_user_question_tool_schema",
    "spawn_subagent_tool_schema",
    "todo_write_tool_schema",
    "skill_tool_schema",
    "extract_safety_constraints",
    "enforce_verbatim_constraints",
]

#: ``SPAWN_SUBAGENT_TOOL`` and ``spawn_subagent_tool_schema`` are defined in
#: ``noeta.policies._control_translate`` (the response→Decision translation
#: seam, B3) and re-exported here so existing
#: ``from noeta.policies.react import SPAWN_SUBAGENT_TOOL`` call sites and the
#: runner's ``spawn_subagent_tool_schema()`` keep working unchanged.


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

    Construct **one instance per Task**: ``_step_count`` is an instance
    attribute, sharing it across tasks would let the cumulative counter
    bleed across loops. Subtasks should construct their own ReActPolicy.
    """

    def __init__(
        self,
        *,
        llm: _LLMClientP,
        tools: dict[str, Tool],
        system_prompt: str,
        model: str,
        max_steps: int = 50,
        max_history_messages: Optional[int] = None,
        delegation_enabled: bool = False,
        todo_write_enabled: bool = False,
        ask_user_question_enabled: bool = False,
        skill_invocation_enabled: bool = False,
        workflow_enabled: bool = False,
        skill_menu_names: frozenset[str] = frozenset(),
        content_store: Optional[ContentStore] = None,
        context_window: Optional[int] = None,
        max_output_tokens: int = 0,
        compaction_buffer: int = 0,
        tail_token_budget: int = 0,
        composer_version: str = "",
        # Wiring-only LLM request overrides. Omitted from canonical bytes
        # on the wire when ``None`` (LLMRequest.__canonical_omit_none__),
        # so a host that leaves them unset keeps the same stable prompt
        # prefix legacy sessions had (prefix-cache friendly).
        output_schema: Optional[dict[str, Any]] = None,
        thinking: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._system_prompt = system_prompt
        self._model = model
        self._max_steps = max_steps
        self._max_history_messages = max_history_messages
        self._step_count = 0
        # the chars/4 estimate of the request we LAST actually
        # sent to the provider. The compaction trigger mixes the real recorded
        # usage of that turn (``StepContext.last_input_tokens``) with the chars/4
        # delta since (``current estimate − this``). Advanced ONLY right after a
        # successful ``_llm.complete`` (never on a proactive/early return), so it
        # always tracks the last request that actually consumed tokens. ``0`` on
        # a fresh instance; one instance per Task (like ``_step_count``), and
        # because decide() is called in the same order on resume, the
        # value reconstructs identically (no persisted state needed).
        self._last_estimate_at_call = 0
        # ③ (D-3): compaction trigger configuration. When ``context_window``
        # is None compaction is OFF (legacy behaviour — no proactive trigger,
        # an overflow error stays a FailDecision). When set, the available
        # window is ``context_window - max_output_tokens - compaction_buffer``
        # and BOTH triggers (proactive estimate / passive overflow) return a
        # ``CompactionRequestedDecision``. The window/output/buffer come from
        # the catalog ModelSpec (D-C1) but are INJECTED here — react.py never
        # imports ``noeta.providers`` (sibling band, provider neutrality). The
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
        # Issue C: when True, a single ``spawn_subagent`` tool_use is
        # translated into a SpawnSubtaskDecision (the schema is supplied
        # to the provider via the Composer's control_action_schemas, not
        # from here). Off by default — existing sessions are unchanged.
        self._delegation_enabled = delegation_enabled
        # CW18b: when True, a ``todo_write`` tool_use becomes a
        # StatePatchDecision (durable TaskState.todos replace-all). Schema is
        # supplied via the Composer's control_action_schemas. Off by default.
        self._todo_write_enabled = todo_write_enabled
        # CW18d: default-off structured HITL question control tool.
        self._ask_user_question_enabled = ask_user_question_enabled
        # D1/D4: when True, a ``skill`` tool_use activates the named
        # skill via a StatePatchDecision (activate_skills patch). The menu
        # names are the frozenset of skill names the workspace registry
        # indexed — same source the schema menu is built from so the two
        # never disagree. Off by default.
        self._skill_invocation_enabled = skill_invocation_enabled
        # when True, a ``run_workflow`` tool_use is translated into
        # a SpawnSubtaskDecision spawning an OrchestrationPolicy child. Schema
        # is supplied via the Composer's control_action_schemas. Off by default.
        self._workflow_enabled = workflow_enabled
        self._skill_menu_names = skill_menu_names
        # Structured output and reasoning controls. Carried verbatim into
        # every LLMRequest so adapters can map them to their vendor wire
        # format (see providers/anthropic.py and providers/openai_compat.py).
        self._output_schema = output_schema
        self._thinking = thinking
        self._effort = effort

    # ------------------------------------------------------------------
    # Policy Protocol
    # ------------------------------------------------------------------

    def decide(self, ctx: StepContext, view: View) -> Decision:
        if self._step_count >= self._max_steps:
            return FailDecision(
                reason="react_max_steps_exceeded", retryable=False
            )
        self._step_count += 1
        request, selection = self._build_request_and_selection(view)
        # ③ (D-3a) proactive trigger. The trigger size is no
        # longer a pure chars/4 estimate of the whole request — that
        # systematically under-counts cache / structured blocks / images, and
        # since D1 removed the message-count gate the token gate is the ONLY entry, so its
        # precision matters. We mix the REAL recorded usage of the previous
        # round-trip (``ctx.last_input_tokens``) with a chars/4 estimate of only
        # what was appended since (see ``_trigger_estimate``). The bare chars/4
        # ``estimated`` is still computed and carried on the Decision /
        # ``Compacted`` event as the provider-neutral size signal.
        estimated = estimate_messages_tokens(request.messages)
        trigger_size = self._trigger_estimate(ctx, estimated)
        if self._compaction_triggered(trigger_size):
            # Compact FIRST — do not spend the main LLM call on a doomed
            # request. (No LLM call this step, so ``_last_estimate_at_call``
            # is intentionally NOT advanced — the mix baseline only moves when
            # a real round-trip records fresh usage.)
            return self._compaction_decision(
                ctx, view, reason="proactive", estimated=estimated
            )
        response = self._llm.complete(request, ctx, selection=selection)
        # a real round-trip just happened, so the next turn's
        # "appended since" delta is measured from THIS request's estimate.
        # Pin it AFTER the call so a proactive/early return never advances it.
        self._last_estimate_at_call = estimated
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
                ctx, view, reason="overflow", estimated=estimated
            )
        return self._response_to_decision(response)

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

    def _trigger_estimate(self, ctx: StepContext, estimated: int) -> int:
        """The size the proactive trigger compares against the window.

        Compaction mix: ``real previous-turn usage + chars/4 of what was
        appended since``. ``ctx.last_input_tokens`` is the provider's REAL
        input count for the previous round-trip (already recorded — read-only,
        never re-counted live → a resumed run derives the same trigger). The
        "appended since" delta is approximated as ``current chars/4 estimate −
        the chars/4 estimate of the request we last actually sent``
        (``_last_estimate_at_call``): a turn that only appended a tool result
        grows the estimate by that result's size, which is exactly the delta we
        want to add on top of the real baseline.

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
        if ctx.last_input_tokens <= 0:
            return estimated
        delta = max(0, estimated - self._last_estimate_at_call)
        return max(estimated, ctx.last_input_tokens + delta)

    def _compaction_triggered(self, estimated: int) -> bool:
        window = self._available_window()
        return window is not None and estimated >= window

    def _summary_prompt_request(
        self, history: list[Message]
    ) -> LLMRequest:
        """Build the deterministic summarize round-trip request (D-3c).

        A fixed structured-section instruction over the
        history-to-be-collapsed. The sections are adopted from Claude Code's
        compaction template but trimmed to a durable subset: Noeta only ever
        summarises the OLD PREFIX (everything before the protected verbatim tail
        window, D3), so the recent state already sits verbatim in that tail.
        Asking the summary to also restate Current Work / Next Step would make
        the model re-narrate text already present word-for-word — wasteful and
        prone to disagree with the tail — so those two sections are deliberately
        DROPPED (left to the tail).

        D6: the "Files & Code" section keeps a PATH LIST only, never the file
        bodies. Re-injecting bodies is a false need here: re-reading disk breaks
        determinism, and a ContentStore snapshot would be a STALE copy for an
        agent that is actively editing — so we record the relevant paths and let
        the model fetch the CURRENT version with ``read`` when it needs them.

        Fully deterministic (same history → same request) so a resumed run
        rebuilds the identical summarize call and the prompt prefix stays
        stable (prefix-cache friendly). Provider-neutral: the wording
        names no vendor and no vendor-specific mechanism — it works for any
        provider. Goes through the same ``_llm`` as a normal turn, so it records
        LLMRequestStarted/Recorded/Finished.
        """
        summary_system = Message(
            role="system",
            content=[
                TextBlock(
                    text=(
                        "Summarize the conversation so far into a durable,"
                        " structured note. The note will REPLACE the older"
                        " messages of this conversation while the most recent"
                        " messages are kept verbatim, so focus on what would"
                        " otherwise be lost — the early intent and accumulated"
                        " context — NOT the immediate latest state (that is"
                        " already preserved).\n"
                        "Organize the note under exactly these sections (omit a"
                        " section only if it is genuinely empty):\n"
                        "1. Primary Request & Intent: the user's original"
                        " goal(s) and overall intent, stated as fully as"
                        " possible — this is the first thing a long session"
                        " loses.\n"
                        "2. Key Technical Concepts: the technologies, patterns,"
                        " and domain ideas that matter to the work.\n"
                        "3. Files & Code: a LIST OF RELEVANT FILE PATHS touched"
                        " or discussed. List the paths only — do NOT copy file"
                        " contents; the current version of any file can be"
                        " re-read with the read tool when needed.\n"
                        "4. Errors & Fixes: problems encountered and how they"
                        " were resolved.\n"
                        "5. All user messages: a faithful list of what the user"
                        " asked for across the conversation.\n"
                        "6. Pending Tasks: work explicitly requested but not yet"
                        " completed.\n"
                        "7. Decisions & Constraints: decisions made and any"
                        " rules or limits agreed on.\n"
                        "Do NOT add any section restating what is happening"
                        " right now or what to do next — the latest state is"
                        " kept verbatim outside this note, so duplicating it"
                        " here is wasteful. Output only the note.\n"
                        # the model-facing half of the verbatim
                        # rule. The deterministic post-check
                        # (``enforce_verbatim_constraints``) is the actual
                        # guarantee; this nudges the model so the common case
                        # produces a clean summary without the appended block.
                        "HARD RULE: any safety or permission constraint "
                        "(e.g. \"do not touch X\", \"never edit Y\", "
                        "\"禁止访问 …\", \"不得修改 …\") MUST be copied into "
                        "the note VERBATIM — word for word, including the "
                        "exact path or name it protects. Do NOT paraphrase, "
                        "soften, or omit any such constraint."
                    )
                )
            ],
        )
        return LLMRequest(
            model=self._model,
            messages=list(history),
            tools=[],
            system=summary_system,
        )

    def _compaction_decision(
        self,
        ctx: StepContext,
        view: View,
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
        to exactly the same messages. The summary therefore covers the whole
        raw prefix ``rolling_history[:boundary]`` (the Composer always replaces
        the entire ``[:boundary]`` with one summary, so re-summarising from the
        raw prefix each time keeps the result deterministic and the summary
        faithful even when a prior summary already collapsed part of it).

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
        boundary = self._summary_boundary(history)
        if boundary <= view.summary_boundary:
            # No NEW prefix beyond what is already collapsed — compaction would
            # re-summarise the same prefix and spin forever. Fail closed; this
            # subsumes the old ``boundary <= 0`` fast-fail (finding 3).
            return FailDecision(
                reason="compaction_no_progress", retryable=False
            )
        to_summarize = history[:boundary]
        summary_req = self._summary_prompt_request(to_summarize)
        # The summarize round-trip is not user-facing output: opt out of
        # token streaming so a live UI never previews compaction internals.
        summary_resp = self._llm.complete(summary_req, ctx, allow_stream=False)
        summary = "\n".join(
            b.text
            for b in summary_resp.content
            if isinstance(b, TextBlock)
        )
        # The summarize round-trip can come back failed — an ``error``
        # stop_reason (the LLM client's transient retries already exhausted, or
        # a fatal/overflow error) — or empty (a model that emitted only a
        # thinking block / whitespace). Recording EITHER as a ``Compacted``
        # would set ``summary_ref`` to an empty note and then let the Composer's
        # ``_apply_summary`` REPLACE the whole collapsed prefix with it: the
        # early intent and accumulated context are destroyed, AND the empty
        # ``user`` text block the provider is then handed is itself rejected
        # with a 400 on the very next request. Fail the step cleanly instead,
        # leaving the durable history untouched — a bad summary is strictly
        # worse than no compaction. Deterministic on resume: the recorded
        # summarize response replays identically, so the same branch is taken.
        if summary_resp.stop_reason == "error" or not summary.strip():
            return FailDecision(
                reason="compaction_summary_failed", retryable=False
            )
        # the model is *asked* (via the summarize prompt) to keep
        # safety/permission directives verbatim, but cannot be trusted to. Run
        # the deterministic post-check over the SAME prefix we collapsed: any
        # detected constraint that the model dropped or paraphrased is appended
        # back word-for-word. Pure over ``(to_summarize, summary)`` so the
        # recorded summary stays a deterministic function of the recorded
        # response → a resumed run rebuilds the identical summary.
        summary = enforce_verbatim_constraints(
            summary, extract_safety_constraints(to_summarize)
        )
        return CompactionRequestedDecision(
            reason=reason,
            estimated_tokens=estimated,
            summary=summary,
            boundary_count=boundary,
            composer_version=self._composer_version,
        )

    def _summary_boundary(self, history: list[Message]) -> int:
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
        if self._tail_token_budget <= 0:
            return 0
        acc = 0
        cutoff = len(history)
        for i in range(len(history) - 1, -1, -1):
            acc += estimate_messages_tokens([history[i]])
            if acc > self._tail_token_budget:
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
        # Issue 14 §F: Composer is the SoT for prompt material —
        # ReActPolicy pulls system / tools / messages from the View's
        # three segments.
        #
        # the legacy message-count tail-window truncation is now
        # DEFAULT-OFF (``max_history_messages=None``). It was a guard that
        # dropped the oldest messages once the count crossed 50 — and because
        # it was decoupled from tokens it fired far earlier than the token
        # compaction gate, bluntly dropping early context before the summariser
        # ever ran. Pure token compaction (③ D-3, ``_compaction_triggered``) is
        # now the only gate; the protected tail is sized by ``tail_token_budget``,
        # not a message count. The parameter is KEPT as an escape hatch: pass a
        # positive int and the old behaviour is restored verbatim.
        #
        # MS1: this is the one place message selection happens, so it is the
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
            # the client sends none) doesn't truncate the answer — react would otherwise
            # treat that truncation as an llm_truncated failure. ``0`` (host didn't inject)
            # ⇒ ``None`` ⇒ omitted from canonical bytes, keeping the prompt
            # prefix identical to legacy sessions that never set it.
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

    def _control_toggles(self) -> ControlToggles:
        """Collect the four default-off control-tool enable flags into the
        small value object the translation seam (B3) consumes."""
        return ControlToggles(
            ask_user_question=self._ask_user_question_enabled,
            todo_write=self._todo_write_enabled,
            delegation=self._delegation_enabled,
            skill_invocation=self._skill_invocation_enabled,
            workflow=self._workflow_enabled,
        )

    def _response_to_decision(
        self, response: LLMResponse
    ) -> Decision:
        history_content = _strip_thinking(response.content)
        if response.stop_reason == "tool_use":
            assistant_message = Message(
                role="assistant", content=history_content
            )
            # B3: route any enabled control-tool call (ask_user_question →
            # todo_write → spawn_subagent → skill → run_workflow, in that
            # fixed order) through the single translation seam. ``None`` means
            # no control tool matched — fall through to the normal tool_calls
            # path.
            control = translate_control_tool(
                response,
                assistant_message,
                toggles=self._control_toggles(),
                content_store=self._content_store,
                skill_menu_names=self._skill_menu_names,
            )
            if control is not None:
                return control
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
            return ToolCallsDecision(
                calls=calls,
                assistant_message=assistant_message,
                assistant_thinking=thinking,
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
            answer = "\n".join(
                block.text
                for block in response.content
                if isinstance(block, TextBlock)
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
            # Mirror the end_turn guard above: a reasoning model can spend its
            # entire output budget on ThinkingBlock(s) before any text/tool_use,
            # leaving history_content == [] here too (thinking is stripped by
            # ``_strip_thinking``). Anthropic rejects an assistant message with
            # content:[] with a 400 on the next request, and this branch is
            # normally retryable — without this guard a retry would resend the
            # very history a poisoned turn just wrote. Fail exactly like the
            # end_turn guard instead of recording an unsendable turn.
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
        # stop_reason == "error".
        return self._error_to_decision(response)

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
        # history.
        return FailDecision(
            reason="llm_error",
            retryable=False,
            assistant_message=None,
        )


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
    here — the Anthropic adapter and ContextComposer three-segment work
    (Phase 1 second slice, issue 14) own that path. First slice records
    this as a documented limitation.
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
    This is the detector — it walks every ``TextBlock`` of every message, splits
    on newlines, and keeps each line whose text contains a constraint trigger
    (:data:`_CONSTRAINT_TRIGGERS`), STRIPPED of surrounding whitespace but
    otherwise verbatim (so the exact wording — including the path it protects —
    is preserved for re-injection).

    Pure + deterministic + provider-neutral: same messages → same list, no
    clock / randomness / network, no provider field. Order is first-appearance;
    duplicates are dropped so a constraint repeated across turns is re-injected
    only once. The summary post-check (:func:`enforce_verbatim_constraints`) and
    the model-facing prompt rule both build on this single detector.
    """
    seen: set[str] = set()
    out: list[str] = []
    for msg in messages:
        for block in msg.content:
            if not isinstance(block, TextBlock):
                continue
            for raw_line in block.text.split("\n"):
                line = raw_line.strip()
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
