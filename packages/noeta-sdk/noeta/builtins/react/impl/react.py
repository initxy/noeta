"""ReActPolicy — the ReAct loop policy.

Bridges a typed
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

* Beyond the three core variants
  (``ToolCallsDecision / FinishDecision / FailDecision``), control-tool calls
  translate into their neutral decisions (spawn-subtask, yield-for-human,
  wait-timer) via ``translate_control_tool``.
* ``model`` is a constant per Policy instance.
* ``_step_count`` is an instance attribute — **one Policy instance per
  Task**; a subtask uses its own Policy with its own counter.

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
from typing import Any, Optional, Protocol

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
from noeta.protocols.resources import load_markdown
from noeta.protocols.step_context import StepContext
from noeta.protocols.tool import Tool
from noeta.protocols.view import View


__all__ = [
    "ReActPolicy",
    "SPAWN_SUBAGENT_TOOL",
    "extract_safety_constraints",
    "enforce_verbatim_constraints",
]

#: ``SPAWN_SUBAGENT_TOOL`` is defined in ``noeta.policies.control_semantics``
#: (the kernel's reserved control vocabulary) and re-exported here for this
#: module's own readers. It is NOT this module's public address: control tools
#: are kernel vocabulary. Each control-tool SCHEMA function lives in its own
#: built-in — ``todo_write`` / ``ask_user_question`` / ``spawn_subagent``,
#: ``skill`` (skills), and ``run_workflow`` / ``structured_output`` (this
#: built-in's ``control_tool`` module).

#: The compaction summarize call's system prompt, externalized to
#: ``summarize.md`` beside this module (byte-identical to the former inline
#: literal, so a resumed run still rebuilds the identical summarize request).
#: Its closing HARD RULE is the model-facing half of the verbatim rule: the
#: deterministic post-check (:func:`enforce_verbatim_constraints`) is the
#: actual guarantee; the prompt nudges the model so the common case produces a
#: clean summary without the appended block.
_SUMMARIZE_SYSTEM_PROMPT = load_markdown(__package__, "summarize")

#: ``ReActPolicy._last_input_tokens_at_call`` sentinel: a compaction collapsed
#: the history, so no recorded input count describes it any more. Distinct from
#: ``0`` ("no round-trip yet — fall back to the ctx baseline") because after a
#: compaction the ctx baseline is stale too and must NOT be fallen back to.
#: See :meth:`ReActPolicy._real_baseline`.
_BASELINE_INVALIDATED = -1

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
        # The provider's REAL input count for the last main round-trip THIS
        # instance made, or ``0`` before the first one. Pinned beside
        # ``_last_estimate_at_call`` (same call site, same resume argument) and
        # read by ``_trigger_estimate`` in preference to
        # ``StepContext.last_input_tokens``.
        #
        # Why both: ``ctx.last_input_tokens`` is fold-projected, and fold only
        # runs at a lease boundary — the ``LLMRequestFinished`` emitted mid
        # tool-loop never reaches the in-memory task. So inside ONE
        # ``Engine.run_one_step`` the ctx baseline is frozen at the entry value
        # (``0`` on a first turn), which silently degrades the trigger to the
        # pure chars/4 estimate for the whole turn, however long the tool loop
        # runs. This field carries the same real count across that gap; ctx
        # remains the cross-turn baseline for a FRESH instance (which starts
        # here at ``0``). Most recent wins — never ``max``: after a compaction
        # the real count DROPS, and a max would pin the trigger to a stale
        # pre-compaction high and re-fire forever.
        self._last_input_tokens_at_call = 0
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
        # the token gate is the ONLY compaction entry, so its
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
            self._last_input_tokens_at_call = response.usage.input
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
        baseline = self._real_baseline(ctx)
        if baseline <= 0:
            return estimated
        delta = max(0, estimated - self._last_estimate_at_call)
        return max(estimated, baseline + delta)

    def _real_baseline(self, ctx: StepContext) -> int:
        """The most recent REAL input count available, or ``0`` if none is.

        ``_last_input_tokens_at_call`` (this instance's own last main
        round-trip) is strictly more recent than ``ctx.last_input_tokens``
        (fold-projected at the lease boundary, then frozen for the whole turn),
        so it wins whenever it is set. A fresh instance has none and falls back
        to ctx — the cross-turn baseline. Both derive from an already-recorded
        ``Usage``, so a resumed run re-derives the same number: we never count
        tokens live.

        ``_BASELINE_INVALIDATED`` (a compaction landed) suppresses BOTH: no
        recorded count describes the collapsed history, so the caller falls
        back to the pure estimate rather than trust a stale high.
        """
        if self._last_input_tokens_at_call == _BASELINE_INVALIDATED:
            return 0
        if self._last_input_tokens_at_call > 0:
            return self._last_input_tokens_at_call
        return ctx.last_input_tokens

    def _compaction_triggered(self, estimated: int) -> bool:
        window = self._available_window()
        return window is not None and estimated >= window

    def _summary_prompt_request(
        self, history: list[Message], tools: list[dict[str, Any]]
    ) -> LLMRequest:
        """Build the deterministic summarize round-trip request.

        A fixed structured-section instruction over the
        history-to-be-collapsed. The sections are adopted from Claude Code's
        compaction template but trimmed to a durable subset: Noeta only ever
        summarises the OLD PREFIX (everything before the protected verbatim tail
        window), so the recent state already sits verbatim in that tail.
        Asking the summary to also restate Current Work / Next Step would make
        the model re-narrate text already present word-for-word — wasteful and
        prone to disagree with the tail — so those two sections are deliberately
        DROPPED (left to the tail).

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
        empty-summary guard would kill the task. ``metadata["tool_choice"] =
        "none"`` closes that at the wire for the first-party adapters (each
        maps it to its vendor spelling; ``tool_choice`` sits below the
        tools+system cache tiers, so the shared prefix survives), and the
        prompt's closing no-tools instruction covers any third-party adapter
        that ignores the metadata.

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
        summary_system = Message(
            role="system",
            content=[TextBlock(text=_SUMMARIZE_SYSTEM_PROMPT)],
        )
        return LLMRequest(
            model=self._compaction_model or self._model,
            messages=list(history),
            tools=list(tools),
            system=summary_system,
            # The summarize round-trip must not tool-call (see the docstring):
            # first-party adapters map this to their vendor tool_choice.
            metadata={"tool_choice": "none"},
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
        deref, and no Policy-private carry-over (a resumed run builds a fresh
        Policy instance, so instance state would silently change the summarize
        request across a resume — the one thing determinism forbids). Anything
        that does not look like that message returns ``None`` and the caller
        falls back to summarising the whole raw prefix: lossless, merely
        unbounded, i.e. the safe direction to degrade in.
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
        before. (One residual gap, deliberately not closed here: the delta is
        RAW, so a turn whose tool output the Composer cleared to a marker still
        contributes its full body to the summarize input.)

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
            # also covers the ``boundary <= 0`` case.
            return FailDecision(
                reason="compaction_no_progress", retryable=False
            )
        previous_summary = self._previous_summary_message(view)
        if previous_summary is None:
            # First compaction (or a View whose head is not the Composer's
            # summary message): the whole raw prefix is the input.
            to_summarize = history[:boundary]
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
        summary_req = self._summary_prompt_request(
            to_summarize, view.provider_tool_schemas
        )
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
        self._last_input_tokens_at_call = _BASELINE_INVALIDATED
        return CompactionRequestedDecision(
            reason=reason,
            estimated_tokens=estimated,
            summary=summary,
            boundary_count=boundary,
            composer_version=self._composer_version,
        )

    def _tail_budget_in_estimate_units(self) -> int:
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
        return int(self._tail_token_budget / self._observed_density())

    def _observed_density(self) -> float:
        """REAL provider tokens per chars/4 estimated token, as measured on
        this instance's last main round-trip (``1.0`` before there is one).

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
        if self._last_estimate_at_call <= 0:
            return 1.0
        if self._last_input_tokens_at_call <= 0:
            return 1.0
        ratio = self._last_input_tokens_at_call / self._last_estimate_at_call
        return min(_DENSITY_MAX, max(_DENSITY_MIN, ratio))

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
        budget = self._tail_budget_in_estimate_units()
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
            # the client sends none) doesn't truncate the answer — react would otherwise
            # treat that truncation as an llm_truncated failure. ``0`` (host didn't inject)
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
                specs=self._control_translate_specs,
                content_store=self._content_store,
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
    This is the detector — it walks every ``TextBlock`` of every message, splits
    on newlines, and keeps each line whose text contains a constraint trigger
    (:data:`_CONSTRAINT_TRIGGERS`), STRIPPED of surrounding whitespace and of a
    leading list bullet (:data:`_BULLET_PREFIXES`) but otherwise verbatim (so
    the exact wording — including the path it protects — is preserved for
    re-injection).

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
