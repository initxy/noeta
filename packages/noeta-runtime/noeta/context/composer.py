"""The ``stable_prefix / semi_stable / dynamic_suffix`` ContextComposer.

Compose is a pure function of folded Task state — no LLM, no clock, no
randomness, no network IO — so the same state always yields the same View and a
replay reproduces it byte for byte. The two head segments must additionally
serialize identically from step to step: churn there busts the provider's
prompt / KV cache and spikes cost, which is why every volatile product lands in
the dynamic suffix. The one side effect is writing the ContextPlan body into the
injected ``ContentStore`` so the Engine has a ``ContentRef`` to attach onto
``ContextPlanComposed``; ``ContextState.plan_ref`` is fold-derived from that
event, so the Composer never writes ``task.context`` directly (single-writer
invariant).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from noeta.context.reminders import ReminderRegistry, ReminderView
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.content_store import ContentStore
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.messages import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.task import Task
from noeta.protocols.token_estimate import estimate_messages_tokens
from noeta.protocols.tool import Tool
from noeta.protocols.values import ContentRef
from noeta.protocols.view import View, ViewSegment


__all__ = [
    "ContentRenderers",
    "RenderedContent",
    "RetrievedResource",
    "ContentRenderer",
    "ContentResolve",
    "ThreeSegmentComposer",
]


# A trace / inspect display label only: nothing compares it, so the composed
# bytes may shift without a bump. Bookkeeping, not a contract.
COMPOSER_VERSION = "three_segment.v5"
_PLAN_MEDIA_TYPE = "application/json"


#: Sanity band for the observed real-tokens-per-estimated-token ratio. The
#: honest spread of the chars/4 heuristic across real payloads runs from ~0.5
#: (prose with long words) through ~2 (dense JSON) up to ~4–7 for pure-CJK
#: text (the estimator counts code points; CJK tokenises at ~1–1.7 tokens per
#: character), so the band must sit ABOVE that spread — a clamp that binds on
#: a genuine CJK measurement over-protects the tail by the clamped factor. It
#: exists only to stop a NON-measurement from
#: propagating. A provider or gateway that reports garbage usage — e.g.
#: ``input_tokens=10`` against a ~2600-token estimate — yields ~0.004, which
#: divides the tail budget by 250 and protects a "tail" larger than the whole
#: history: the summary boundary pins at 0 and every compaction dies on
#: ``compaction_no_progress``. Clamping degrades that to "trust the estimate,
#: roughly", which is the same direction the unmeasured first turn already
#: takes.
_DENSITY_MIN = 0.25
_DENSITY_MAX = 8.0


def _density(real_baseline: int, estimated: int) -> float:
    """REAL provider tokens per chars/4 estimated token, from the last round-trip.

    ``1.0`` when there is nothing to measure against — a first turn, or a
    compaction that zeroed the baseline — which leaves the caller's arithmetic in
    pure estimate units. The Policy computes the same ratio rather than sharing
    this one: the two bands do not import each other, and their inputs differ.

    Clamped to ``[_DENSITY_MIN, _DENSITY_MAX]``: both operands are provider-
    reported numbers, so a gateway that reports nonsense would otherwise scale
    the tail budget by an arbitrary factor. Pure and deterministic — a resumed
    run clamps the same recorded operands identically.
    """
    if real_baseline <= 0 or estimated <= 0:
        return 1.0
    return min(_DENSITY_MAX, max(_DENSITY_MIN, real_baseline / estimated))
#: Role of the single summary message swapped in for a compacted prefix:
#: ``user`` keeps it provider-neutral and outside the ``system`` stable_prefix.
_SUMMARY_ROLE = "user"
#: Source of truth is ``noeta.policies.control_semantics.SPAWN_SUBAGENT_TOOL``;
#: duplicated as a literal to keep the Composer free of a ``noeta.policies``
#: import. The delegation reminder gates on this name appearing in
#: ``provider_tool_schemas``.
_SPAWN_SUBAGENT_TOOL_NAME = "Task"


@dataclass(frozen=True, slots=True)
class RetrievedResource:
    """One body-referenced skill resource considered for inlining.

    ``reason="referenced"`` ⇒ the resource passed every boundary check and its
    ``raw_bytes`` were inlined into the ``semi_stable`` segment; a ``skipped:*``
    reason ⇒ it failed one (binary / too_large / symlink_escape), ``raw_bytes``
    is ``None``, and nothing entered the prompt. The renderer reads disk but
    never touches the ContentStore — persistence is the Composer's job (single
    writer).
    """

    skill: str
    relpath: str
    reason: str
    media_type: Optional[str] = None
    raw_bytes: Optional[bytes] = None


@dataclass(frozen=True, slots=True)
class RenderedContent:
    """Output of a :data:`ContentRenderer` call.

    ``selected_skills`` is the post-filter, post-sort name list the Composer
    copies verbatim into the ``ContextPlan`` body: only the renderer knows which
    activations actually landed in this View, so the Composer cannot re-derive
    the list from raw activation state (which may carry unknown names, or a
    different order from the rendered output). ``retrieved_resources`` is the
    body-referenced content the renderer inlined or skipped, in deterministic
    ``(skill, relpath)`` order.
    """

    messages: list[Message]
    selected_skills: list[str]
    retrieved_resources: list[RetrievedResource] = field(default_factory=list)


#: Deref a resident's currently-active bytes: ``resolve(kind, name)`` returns
#: the bytes recorded for the hash the ledger has active for ``(kind, name)``.
#: A renderer reads its content through this, so the composed bytes stay a pure
#: function of (folded state, content store) — a refresh moves bytes, a mutated
#: backing store on disk does not. Raises when the pair is not active.
ContentResolve = Callable[[str, str], bytes]

#: A content kind's render rule: ``(active_names, resolve) -> RenderedContent``.
#: ``resolve`` derefs recorded bytes (above); a renderer whose content is not
#: content-addressed (e.g. skills render from a preloaded registry) accepts the
#: parameter and ignores it.
ContentRenderer = Callable[[list[str], ContentResolve], RenderedContent]


#: The one kind the composer treats specially: its renderer's post-resolve name
#: list feeds ``ContextPlan.selected_skills``, a skill-named plan field kept for
#: plan-body byte stability.
_SKILL_KIND = "skill"


class ContentRenderers(Protocol):
    """The composer-facing surface of a content-channel registry.

    One render rule per registered kind; ``kinds()`` order IS the
    ``semi_stable`` layout. Declared structurally so ``composer`` does not
    import its own consumer,
    :class:`noeta.context.content_channel.ContentChannelRegistry`. A renderer
    must stay a pure function of the names fold derived from the ledger — no
    compose-time fetches (the red line).
    """

    def kinds(self) -> tuple[str, ...]: ...

    def render(
        self, kind: str, names: list[str], resolve: ContentResolve
    ) -> RenderedContent: ...


class _SkillOnlyRenderers:
    """Adapter presenting one ``skill_renderer`` as a single-kind registry.

    Lets the compose path stay registry-shaped for the
    ``ThreeSegmentComposer(skill_renderer=…)`` construction too, so both wirings
    produce byte-identical output. Hosts wanting more kinds pass a real registry.
    """

    def __init__(self, renderer: ContentRenderer) -> None:
        self._renderer = renderer

    def kinds(self) -> tuple[str, ...]:
        return (_SKILL_KIND,)

    def render(
        self, kind: str, names: list[str], resolve: ContentResolve
    ) -> RenderedContent:
        return self._renderer(names, resolve)


def _default_content_renderer(
    _: list[str], __: ContentResolve
) -> RenderedContent:
    """Renderer used when no ``skill_renderer`` and no registry are supplied.

    Yields an empty ``semi_stable`` segment; the Composer still folds an empty
    ``selected_skills`` list into the ``ContextPlan`` body, but no skill bytes
    enter the View.
    """
    return RenderedContent(messages=[], selected_skills=[])


def _sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class ThreeSegmentComposer:
    """Three-segment Composer.

    Construct once per Task family: ``system_prompt`` and the tool set are fixed
    over a Task's lifetime. Compose is pure (no clock, no randomness) and
    stateless across calls, so the same Task state always composes the same
    View. Beyond that, ``stable_prefix`` and ``semi_stable`` must serialize
    reproducibly across steps: churning them busts the provider's prompt / KV
    cache and spikes cost (see CONTEXT.md "Stable Prefix").
    """

    def __init__(
        self,
        *,
        system_prompt: str,
        tools: dict[str, Tool],
        content_store: ContentStore,
        skill_renderer: Optional[ContentRenderer] = None,
        content_renderers: Optional[ContentRenderers] = None,
        reminders: Optional[ReminderRegistry] = None,
        control_action_schemas: Optional[list[dict[str, Any]]] = None,
        tail_token_budget: Optional[int] = None,
        available_window: Optional[int] = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._tools = dict(tools)
        self._content_store = content_store
        # ``skill_renderer`` is construction sugar for the single-kind case: it
        # becomes the ``kind="skill"`` item of an implicit registry, so both
        # paths produce byte-identical output. Passing both is ambiguous — the
        # skill item would exist twice.
        if skill_renderer is not None and content_renderers is not None:
            raise ValueError(
                "pass either skill_renderer or content_renderers, not both; "
                "register the skill kind as a registry item instead"
            )
        self._content_renderers: ContentRenderers = (
            content_renderers
            if content_renderers is not None
            else _SkillOnlyRenderers(skill_renderer or _default_content_renderer)
        )
        # Compose-time reminder registry: the dynamic-suffix-tail reminders are
        # rendered through this table, exactly as semi_stable residents render
        # through ``content_renderers``. ``None`` is an EMPTY registry — a bare
        # composer renders no reminders; a host injects the built-in renderers
        # through ``base_reminders``. The composer stays a pure function of
        # folded state: the registry only ever appends to the volatile dynamic
        # suffix, never the stable prefix.
        self._reminders: ReminderRegistry = (
            reminders if reminders is not None else ReminderRegistry(())
        )
        # The protected tail-window size in *estimated tokens*. When set,
        # tool-result outputs of messages older than this budget are cleared to
        # an explicit cleared-marker (deterministic prune); ``None`` lets every
        # output pass through. The budget is in neutral estimate units — not a
        # message count — so the kept window scales with how big the recent
        # turns actually are. Provider-neutral.
        self._tail_token_budget = tail_token_budget
        # The model's usable window in estimated tokens
        # (``context_window - max_output - buffer`` — the SAME quantity the
        # Policy's summarize trigger compares against). When set, prune is a
        # RELIEF VALVE, not an always-on clamp: tool outputs stay verbatim until
        # the composed history actually approaches this window, so a half-empty
        # window never forces the model to re-run a tool to recover content it
        # already fetched. ``None`` prunes to the tail unconditionally.
        # Deterministic (a pure function of the model), so live + resume gate
        # identically.
        self._available_window = available_window
        # Provider-visible **control** action schemas that are NOT executable
        # workspace tools (e.g. ``spawn_subagent``, which the policy translates
        # into a Decision and the ToolRuntime never invokes). They are appended
        # to ``View.provider_tool_schemas`` after the real tools — so the prompt
        # tool surface (and the ``stable_prefix`` hash + ``ContextPlan``)
        # faithfully reflects what the provider sees — while staying absent from
        # the Engine tools dict / ToolRuntime. Order is deterministic.
        self._control_action_schemas: list[dict[str, Any]] = list(
            control_action_schemas or []
        )

    def compose(self, task: Task) -> View:
        provider_tool_schemas = self._render_provider_tool_schemas()
        stable_content = [
            Message(
                role="system",
                content=_text_blocks(self._system_prompt),
            )
        ]
        # stable_prefix hash folds provider_tool_schemas in alongside the content
        # so a tool-set change rotates the prefix hash even though the
        # narrative prompt text is identical.
        stable_hash = _sha256_hex(
            to_canonical_bytes((stable_content, provider_tool_schemas))
        )

        # Anchored placement: split the active residents by activation anchor.
        # Pre-loop activations (anchor at/before the first assistant message)
        # render in ``semi_stable``; mid-task activations render inside the
        # dynamic suffix at their anchor, so a skill invoked at turn 40 does not
        # rewrite the head segments (which would re-prime the KV cache).
        semi_names, anchored = self._split_placement(task)

        # Render the semi_stable residents through the kind registry, in
        # registration order. The skill kind keeps one narrow extra: its
        # post-resolve name list feeds ``ContextPlan.selected_skills`` (kept
        # skill-named for plan-body byte stability).
        resolve = self._content_resolve(task)
        semi_content: list[Message] = []
        selected_skills: list[str] = []
        renderer_resources: list[RetrievedResource] = []
        for kind in self._content_renderers.kinds():
            rendered = self._content_renderers.render(
                kind, semi_names.get(kind, []), resolve
            )
            semi_content.extend(rendered.messages)
            if kind == _SKILL_KIND:
                selected_skills = list(rendered.selected_skills)
            renderer_resources.extend(rendered.retrieved_resources)
        semi_hash = _sha256_hex(to_canonical_bytes(semi_content))

        # Swap the compacted prefix for the summary message, insert the anchored
        # residents at their (post-summary) anchor positions, re-attach
        # extended-thinking ahead of each assistant turn's tool_use, then prune
        # tool outputs older than the protected tail window.
        summarized, anchored_skills, anchored_resources = (
            self._insert_anchored(task, self._apply_summary(task), anchored)
        )
        for s in anchored_skills:
            if s not in selected_skills:
                selected_skills.append(s)
        renderer_resources.extend(anchored_resources)

        # The Composer is the single writer of the ContentStore + plan: a
        # ``referenced`` resource's raw bytes go to the store (the content was
        # already inlined into the composed messages by the renderer, so it is
        # in the segment hashes); a ``skipped`` resource carries no
        # ``content_ref`` and never entered the prompt.
        retrieved_resources = self._persist_retrieved(renderer_resources)

        dynamic_source = self._reattach_thinking(summarized, task)
        dynamic_content, selected_refs, dropped_refs, cleared_refs = (
            self._prune_tail(
                dynamic_source,
                real_baseline=task.runtime.last_input_tokens,
                prefix_estimate=estimate_messages_tokens(
                    stable_content + semi_content
                ),
            )
        )
        # Compose-time reminders: the todo re-injection, the delegation fan-out
        # nudge, and the compaction-thrashing read hint are rendered through the
        # reminder registry and appended, in priority order, to the END of the
        # dynamic_suffix (the volatile segment), then hashed. Each is a View-only
        # product: NOT written to ``runtime.messages`` and emitting no event, so
        # it never enters the folded truth; regenerated from the folded
        # projection on every compose ⇒ resume reproduces it automatically; and,
        # landing in dynamic_content only, it cannot churn the stable_prefix /
        # semi_stable hashes the prompt cache rides on. ``delegation_enabled`` is
        # whether the ``spawn_subagent`` control schema is offered this compose
        # (the fan-out nudge gates on it).
        delegation_enabled = any(
            isinstance(schema, dict)
            and schema.get("function", {}).get("name")
            == _SPAWN_SUBAGENT_TOOL_NAME
            for schema in provider_tool_schemas
        )
        dynamic_content = self._append_reminders(
            task, dynamic_content, delegation_enabled=delegation_enabled
        )
        dynamic_hash = _sha256_hex(to_canonical_bytes(dynamic_content))

        segments = (
            ViewSegment(
                name="stable_prefix",
                content=stable_content,
                segment_hash=stable_hash,
            ),
            ViewSegment(
                name="semi_stable",
                content=semi_content,
                segment_hash=semi_hash,
            ),
            ViewSegment(
                name="dynamic_suffix",
                content=dynamic_content,
                segment_hash=dynamic_hash,
            ),
        )

        plan = ContextPlan(
            composer_version=COMPOSER_VERSION,
            segment_hashes={
                "stable_prefix": stable_hash,
                "semi_stable": semi_hash,
                "dynamic_suffix": dynamic_hash,
            },
            selected_skills=selected_skills,
            selected_messages=selected_refs,
            dropped_messages=dropped_refs,
            cleared_outputs=cleared_refs,
            retrieved_resources=retrieved_resources,
        )
        plan_ref = self._content_store.put(
            to_canonical_bytes(plan), media_type=_PLAN_MEDIA_TYPE
        )

        return View(
            plan_ref=plan_ref,
            segments=segments,
            provider_tool_schemas=provider_tool_schemas,
            # Expose the RAW rolling history + the prior cumulative summary
            # boundary so a compaction-aware Policy computes its new boundary in
            # the SAME coordinate space the Composer's ``_apply_summary`` slices
            # (``task.runtime.messages`` / ``ContextState.summary_boundary``).
            # Without this the Policy would count over ``iter_messages()`` — the
            # post-summary, post-prune, skill-prefixed, tail-truncated
            # projection — and the two boundaries would point at different
            # messages whenever ``semi_stable`` is non-empty or a prior summary
            # already collapsed a prefix.
            rolling_history=list(task.runtime.messages),
            summary_boundary=task.context.summary_boundary,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _split_placement(
        self, task: Task
    ) -> tuple[dict[str, list[str]], list[tuple[int, int, int, str, str]]]:
        """Split active residents into semi_stable names vs anchored entries.

        A resident is *anchored* iff its recorded activation anchor lies strictly
        AFTER the first assistant message of the raw rolling history — the model
        had already started talking when the resident activated. Everything else
        (pre-loop activations, anchor-less residents) keeps ``semi_stable``
        placement, so a session with no mid-task activation composes
        byte-identically.

        Returns ``(semi_names_by_kind, anchored)`` where ``anchored`` entries are
        ``(anchor, kind_index, activation_index, kind, name)`` sorted by that
        tuple — anchor first, registration order and activation order as
        deterministic tie-breaks. Pure over folded state.
        """
        raw = task.runtime.messages
        first_assistant: Optional[int] = None
        for i, msg in enumerate(raw):
            if msg.role == "assistant":
                first_assistant = i
                break
        anchors = task.context.content_anchors
        semi: dict[str, list[str]] = {}
        anchored: list[tuple[int, int, int, str, str]] = []
        for k_i, kind in enumerate(self._content_renderers.kinds()):
            kept: list[str] = []
            for a_i, name in enumerate(self._active_names(task, kind)):
                anchor = anchors.get(f"{kind}:{name}", 0)
                if first_assistant is not None and anchor > first_assistant:
                    anchored.append((anchor, k_i, a_i, kind, name))
                else:
                    kept.append(name)
            semi[kind] = kept
        anchored.sort()
        return semi, anchored

    def _insert_anchored(
        self,
        task: Task,
        summarized: list[Message],
        anchored: list[tuple[int, int, int, str, str]],
    ) -> tuple[list[Message], list[str], list[RetrievedResource]]:
        """Render the anchored residents and insert each at its anchor.

        Coordinates: anchors index the RAW rolling history; ``summarized`` is
        ``[summary] + raw[boundary:]`` when a compaction summary applies, else
        the raw list. An anchor inside the covered prefix clamps to index 1 —
        the re-hang bucket right after the summary message (the earliest stable
        position on the new timeline; compaction already invalidated the cache,
        so the re-hang is free). An anchor past the end appends. The insertion
        index then slides forward past any ``role="tool"`` message so rendered
        content can never split an assistant ``tool_use`` from its results
        (providers reject that shape). Deterministic and pure: same folded state
        ⇒ same bytes.

        Each resident renders through the SAME kind registry as the semi_stable
        path, one name per call (the renderers are one-message-per-name).
        Returns the merged list plus the anchored skill names (for
        ``ContextPlan.selected_skills``) and the renderers' retrieved resources
        (for provenance persistence).
        """
        if not anchored:
            return summarized, [], []
        has_summary = task.context.summary_ref is not None
        boundary = task.context.summary_boundary if has_summary else 0
        n = len(summarized)
        inserts_at: dict[int, list[Message]] = {}
        skill_names: list[str] = []
        resources: list[RetrievedResource] = []
        resolve = self._content_resolve(task)
        for anchor, _k_i, _a_i, kind, name in anchored:
            rendered = self._content_renderers.render(kind, [name], resolve)
            if kind == _SKILL_KIND:
                skill_names.extend(rendered.selected_skills)
            resources.extend(rendered.retrieved_resources)
            if not rendered.messages:
                continue
            if has_summary:
                eff = anchor - boundary + 1
                eff = 1 if eff < 1 else (n if eff > n else eff)
            else:
                eff = anchor if anchor < n else n
            while eff < n and summarized[eff].role == "tool":
                eff += 1
            inserts_at.setdefault(eff, []).extend(rendered.messages)
        if not inserts_at:
            return summarized, skill_names, resources
        out: list[Message] = []
        for i, msg in enumerate(summarized):
            out.extend(inserts_at.get(i, ()))
            out.append(msg)
        out.extend(inserts_at.get(n, ()))
        return out, skill_names, resources

    def _active_names(self, task: Task, kind: str) -> list[str]:
        """The active resident names for ``kind``, read from the generic
        activation map ``TaskState.active_content``.

        Names are returned **sorted** so the layout is a pure function of the
        activation set, never of activation/load timing — and identical whether
        the map was folded fresh or rehydrated from a canonical (key-sorted)
        snapshot body.
        """
        return sorted(task.state.active_content.get(kind, {}))

    def _content_resolve(self, task: Task) -> ContentResolve:
        """Bind the ledger's active hashes to a byte-deref over the store.

        ``resolve(kind, name)`` returns the bytes recorded for the resident's
        currently-active hash, so a renderer that reads through it composes a
        pure function of (folded state, content store): a refresh (new hash)
        yields new bytes, a mutated backing store on disk changes nothing. Raises
        ``KeyError`` when ``(kind, name)`` is not active with a real hash (a
        caller bug); a renderer for a kind whose content is not content-addressed
        — the skill registry — simply never calls it.
        """
        active = task.state.active_content
        store = self._content_store

        def _resolve(kind: str, name: str) -> bytes:
            content_hash = active.get(kind, {}).get(name)
            if not content_hash:
                raise KeyError(
                    f"resolve({kind!r}, {name!r}): not an active resident"
                )
            return store.get(ContentRef(hash=content_hash, size=0, media_type=""))

        return _resolve

    def _persist_retrieved(
        self, resources: list[RetrievedResource]
    ) -> list[dict[str, Any]]:
        """Turn the renderer's :class:`RetrievedResource`s into the
        ``ContextPlan.retrieved_resources`` provenance dicts.

        A ``referenced`` resource's raw bytes are stored (content-addressed, so
        the same bytes always yield the same ``content_ref``); a ``skipped``
        resource gets ``content_ref=None`` and never had its bytes inlined.
        Order is preserved (the renderer already sorts by ``(skill, relpath)``).
        """
        out: list[dict[str, Any]] = []
        for r in resources:
            if r.reason == "referenced" and r.raw_bytes is not None:
                ref = self._content_store.put(
                    r.raw_bytes, media_type=r.media_type or "text/plain"
                )
                out.append(
                    {
                        "skill": r.skill,
                        "relpath": r.relpath,
                        "content_ref": ref,
                        "bytes": len(r.raw_bytes),
                        "media_type": r.media_type,
                        "reason": r.reason,
                    }
                )
            else:
                out.append(
                    {
                        "skill": r.skill,
                        "relpath": r.relpath,
                        "content_ref": None,
                        "bytes": len(r.raw_bytes) if r.raw_bytes is not None else None,
                        "media_type": r.media_type,
                        "reason": r.reason,
                    }
                )
        return out

    def _apply_summary(self, task: Task) -> list[Message]:
        """Swap the compacted prefix for the summary message.

        When ``ContextState.summary_ref`` is set (written by fold's ``Compacted``
        handler), the first ``summary_boundary`` messages of the rolling history
        have been collapsed into one summary. We drop that covered prefix and
        prepend a single provider-neutral summary message (role ``user``),
        preserving ``stable_prefix``. Pure + deterministic: same task state →
        same message list.
        """
        messages = list(task.runtime.messages)
        ref = task.context.summary_ref
        if ref is None:
            return messages
        boundary = max(0, min(task.context.summary_boundary, len(messages)))
        summary_text = self._decode_summary(ref)
        summary_msg = Message(
            role=_SUMMARY_ROLE, content=[TextBlock(text=summary_text)]
        )
        return [summary_msg] + messages[boundary:]

    def _decode_summary(self, ref: ContentRef) -> str:
        """Deref the summary body (canonical-encoded ``str``) to plain text.

        The runtime handler stores ``to_canonical_bytes(summary)`` where
        ``summary`` is a ``str`` (canonical form of a string is its JSON
        quoting). We strip the surrounding quotes deterministically.
        """
        from noeta.protocols.canonical import from_canonical_bytes

        body = from_canonical_bytes(self._content_store.get(ref))
        return body if isinstance(body, str) else str(body)

    def _reattach_thinking(
        self, messages: list[Message], task: Task
    ) -> list[Message]:
        """Re-attach extended-thinking ahead of each assistant turn's
        ``tool_use``.

        ``ContextState.thinking_by_call_id`` (written by fold from
        ``AssistantThinkingRecorded``) maps an assistant turn's first
        ``tool_use`` ``call_id`` to the ThinkingBlocks ``_strip_thinking``
        removed before the turn was persisted. For every assistant message whose
        first ``tool_use`` ``call_id`` has stored thinking, we prepend those
        blocks so the View carries the full ``thinking → tool_use`` shape an
        Anthropic continuation request needs (the signature must round-trip
        verbatim).

        Provider-neutral: the composer always re-attaches into the neutral View;
        the OUTBOUND gating lives in each adapter (the Anthropic adapter
        serializes thinking+signature; ``OpenAICompat`` drops it per
        ``reasoning_continuation``). Pure (no clock, no randomness): same task
        state → same message list. An empty slice is an identity passthrough, so
        the dynamic bytes — and ``dynamic_hash`` — match the stripped history
        unchanged.
        """
        by_call = task.context.thinking_by_call_id
        if not by_call:
            return messages
        out: list[Message] = []
        for msg in messages:
            key = _first_tool_use_call_id(msg) if msg.role == "assistant" else None
            thinking = by_call.get(key) if key is not None else None
            if not thinking:
                out.append(msg)
                continue
            # The rebuild keeps ``origin`` — the message-stream source tag must
            # survive every composer transform (byte-safe: ``origin=None`` is
            # omitted from canonical serialization).
            out.append(
                Message(
                    role=msg.role,
                    content=[*thinking, *msg.content],
                    origin=msg.origin,
                )
            )
        return out

    def _append_reminders(
        self,
        task: Task,
        dynamic_content: list[Message],
        *,
        delegation_enabled: bool,
    ) -> list[Message]:
        """Render the compose-time reminder registry onto the dynamic-suffix tail.

        Builds the narrow :class:`~noeta.context.reminders.ReminderView`
        projection from folded state (plus the two compose-time facts the
        composer alone knows: whether ``spawn_subagent`` is offered this compose,
        and whether a spawn already landed in history), runs every registered
        reminder in ``(priority, name)`` order, and wraps each non-``None`` text
        in one ``Message(role="user", origin="system")`` — the existing
        ``Message.origin`` rendering that each adapter turns into a
        ``<system-reminder>`` (Anthropic wraps + merges into the adjacent user
        turn; the OpenAI adapters render a native mid-history ``role="system"``
        message). Plain text only here — baking the literal tag into the neutral
        View would leak it to every provider.

        Every reminder is a View-only product: appended to ``dynamic_content``
        only, never written to ``runtime.messages`` and emitting no event, so it
        stays out of the folded truth, rides only the volatile (uncached)
        segment, and is re-derived from the folded projection on every compose
        (resume reproduces it). Pure: same projection → same bytes.
        """
        # ``already_spawned`` is read by exactly one reminder (``delegation-nudge``),
        # which renders nothing unless delegation is offered — so the scan is
        # short-circuited off ``delegation_enabled``. Without the guard every leaf
        # sub-agent (explore / plan / web / __consolidation__ — none of them
        # delegate) would walk the whole rolling history on every compose to
        # compute a value the reminder then discards.
        already_spawned = delegation_enabled and any(
            isinstance(block, ToolUseBlock)
            and block.tool_name == _SPAWN_SUBAGENT_TOOL_NAME
            for message in task.runtime.messages
            for block in (getattr(message, "content", None) or [])
        )
        view = ReminderView(
            todos=tuple(task.state.todos),
            delegation_enabled=delegation_enabled,
            already_spawned=already_spawned,
            compaction_thrashing=task.context.compaction_thrashing,
        )
        texts = self._reminders.render_all(view)
        if not texts:
            return dynamic_content
        return [
            *dynamic_content,
            *(
                Message(
                    role="user",
                    content=[TextBlock(text=text)],
                    origin="system",
                )
                for text in texts
            ),
        ]

    def _prune_tail(
        self,
        messages: list[Message],
        *,
        real_baseline: int = 0,
        prefix_estimate: int = 0,
    ) -> tuple[
        list[Message], list[ContentRef], list[ContentRef], list[ContentRef]
    ]:
        """Clear tool-result outputs outside the tail window.

        Walks from the newest message backward, accumulating the estimated token
        cost. Once the accumulated cost exceeds ``tail_token_budget``, every
        older message that carries a :class:`ToolResultBlock` has its ``output``
        replaced by an explicit cleared-marker: the block keeps its
        ``call_id`` / ``role`` / ``success`` so the conversation stays
        well-formed, and the marker is LEAN (``[tool output cleared]``, no hash)
        so the model reads "there WAS content here, it was elided" — never an
        empty string that reads as "the tool returned nothing", and never a
        ContentStore hash it has no tool to deref. The original output's ref is
        returned (4th tuple element) for ``ContextPlan.cleared_outputs`` so the
        full body stays audit-deref-able OFF the prompt. Text / tool_use blocks
        are never touched. Returns the rewritten list plus the kept / cleared
        message refs and the cleared outputs' refs for ``ContextPlan``
        provenance.

        Pure (no clock, no randomness): the token estimate is the stable
        heuristic, and each cleared ref is content-addressed (same output bytes →
        same ref) so the same history always produces the same markers + refs.
        ``tail_token_budget is None`` → no prune, refs empty. ``available_window``
        arms the relief-valve gate: while the request is below it, the whole
        history passes through verbatim (refs empty) — clearing only kicks in
        once the request approaches the usable window, so a half-empty window
        never forces a tool re-read.

        ``real_baseline`` is ``RuntimeState.last_input_tokens`` (the provider's
        count for the previous round-trip) and ``prefix_estimate`` the chars/4
        size of the stable+semi segments this dynamic tail will be assembled
        behind. Together they let both knobs above — which count REAL tokens — be
        read in the same unit the walk accumulates; see the gate comment. Both
        default to the values a caller with no baseline supplies, which
        reproduces the pure-estimate arithmetic.

        Still pure: both inputs are already-recorded numbers folded off the task,
        so live + resume derive identical cutoffs.
        """
        if self._tail_token_budget is None:
            return messages, [], [], []
        # Prune is a relief valve, not an always-on clamp. Below the usable
        # window the whole history stays verbatim — a half-empty window must NOT
        # clear tool outputs, or the model loses content it already fetched and
        # re-runs the read (the thrash this gate fixes). We only start clearing
        # once the request approaches ``available_window``, mirroring the
        # Policy's summarize trigger so the two compaction layers share one water
        # mark.
        #
        # That water mark is REAL provider tokens (``available_window`` derives
        # from ``context_window``), so the gate may not be read in chars/4: the
        # heuristic assumes 4 chars/token and a measured production payload
        # (CJK + JSON + base64 thinking signatures) runs ~1.2, a ~4x under-read
        # that can hold the estimate below the window for an entire session and
        # leave this valve shut while the real request sits AT the window — the
        # exact under-count the context-compaction trigger avoids, live here
        # because the density conversion is local to the Policy.
        # ``real_baseline`` (``RuntimeState.last_input_tokens``) is the
        # provider's own count for the previous round-trip, i.e. the same
        # content minus the newest tool result. ``max`` mirrors the Policy's
        # ``max(estimated, baseline + delta)``: the mix can only ever RAISE the
        # size, so a genuinely huge raw history is never masked and a missing
        # baseline (first turn, post-compaction) falls back to pure-estimate
        # behaviour. The Policy's ``+ delta`` term has no analogue here — pairing
        # it needs the estimate of the request the baseline was measured on,
        # which is not recorded — so the valve opens at most one round-trip late.
        # Late is the safe direction for a relief valve: the summarize trigger
        # backstops it, and firing early would clear outputs the model still
        # needs.
        estimated = prefix_estimate + estimate_messages_tokens(messages)
        if (
            self._available_window is not None
            and max(estimated, real_baseline) < self._available_window
        ):
            return messages, [], [], []
        # Same unit trap one level down, and the one that BITES once the gate
        # opens: ``tail_token_budget`` counts real tokens while the cutoff walk
        # below accumulates chars/4. At the measured density the whole history
        # fits inside the "tail" and nothing is ever cleared — the valve opens
        # onto a no-op. Convert the budget into the accumulator's unit with the
        # density observed on the last round-trip, the same ``real ÷ chars/4``
        # ratio (and the same 1.0-until-observed fallback) the Policy applies to
        # ``_summary_boundary``. Both operands are already-recorded numbers, so
        # a replay re-derives the ratio — we never count tokens live.
        budget = int(self._tail_token_budget / _density(real_baseline, estimated))
        # Decide the cutoff index: messages at index >= cutoff are protected
        # (within budget); messages before cutoff are pruned.
        acc = 0
        cutoff = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            acc += estimate_messages_tokens([messages[i]])
            if acc > budget:
                cutoff = i + 1
                break
            cutoff = i
        # Always keep the freshest result intact: even when the single newest
        # message alone exceeds the budget (cutoff == len(messages)), the model
        # must still see its own most-recent tool output verbatim. Clamp the
        # cutoff so the newest message is never swept into the cleared tail.
        if messages:
            cutoff = min(cutoff, len(messages) - 1)
        out: list[Message] = []
        selected: list[ContentRef] = []
        dropped: list[ContentRef] = []
        cleared_outputs: list[ContentRef] = []
        for i, msg in enumerate(messages):
            if i >= cutoff:
                out.append(msg)
                selected.append(self._msg_ref(msg))
                continue
            pruned_msg, cleared_refs = _clear_tool_outputs(
                msg, self._cleared_output_ref
            )
            out.append(pruned_msg)
            if cleared_refs:
                dropped.append(self._msg_ref(pruned_msg))
                # Original bodies stay audit-deref-able via the plan, never via
                # a hash leaked into the model-facing marker.
                cleared_outputs.extend(cleared_refs)
            else:
                # An old message with no tool output (e.g. plain text) is
                # kept verbatim; record it as selected provenance.
                selected.append(self._msg_ref(pruned_msg))
        return out, selected, dropped, cleared_outputs

    def _cleared_output_ref(self, output: Any) -> ContentRef:
        """Offload a tool output being cleared to the ContentStore; return its ref.

        Red line: a cleared output must
        still deref back to the full body so the audit trail stays intact. The
        ref is recorded in ``ContextPlan.cleared_outputs`` (internal
        provenance) — NEVER embedded in the model-facing cleared-marker, which
        the model cannot deref. Content-addressed: same output bytes yield the
        same ref (pure — no clock, no randomness).
        """
        return self._content_store.put(
            to_canonical_bytes(output), media_type=_PLAN_MEDIA_TYPE
        )

    def _msg_ref(self, msg: Message) -> ContentRef:
        """Content-addressed ref for one message (provenance only).

        Content-addressed so the same (possibly nullified) message always
        produces the same ref — the plan_ref is a pure function of the
        composed messages.
        """
        return self._content_store.put(
            to_canonical_bytes(msg), media_type=_PLAN_MEDIA_TYPE
        )

    def _render_provider_tool_schemas(self) -> list[dict[str, Any]]:
        def _function_schema(tool: Any) -> dict[str, Any]:
            function: dict[str, Any] = {
                "name": tool.name,
                "parameters": tool.input_schema,
            }
            # Description is the canonical, LLM-facing tool-semantics
            # source. Include it only when non-empty so an undocumented tool's
            # schema stays byte-identical — no stable_prefix-hash churn that
            # would bust the provider prompt cache.
            description = getattr(tool, "description", "") or ""
            if description:
                function["description"] = description
            return {"type": "function", "function": function}

        executable_tool_schemas: list[dict[str, Any]] = [
            _function_schema(tool) for tool in self._tools.values()
        ]
        # Control action schemas (e.g. spawn_subagent) follow the real
        # executable tools, deterministically, so they are visible to the provider
        # and folded into the stable hash without being executable tools.
        provider_tool_schemas = executable_tool_schemas
        provider_tool_schemas.extend(self._control_action_schemas)
        return provider_tool_schemas


def _first_tool_use_call_id(msg: Message) -> Optional[str]:
    """Return the ``call_id`` of the first ToolUseBlock in ``msg``, or None.

    This is the stable per-turn identity the re-attached thinking is keyed by:
    an Anthropic assistant turn emits its thinking ahead of (possibly
    several parallel) ``tool_use`` blocks, so the first one's id anchors the
    whole turn's reasoning deterministically (content order is preserved).
    """
    for b in msg.content:
        if isinstance(b, ToolUseBlock):
            return b.call_id
    return None


#: The lean placeholder a pruned tool output is replaced by. Pure ASCII, no
#: hash — the full body's ContentStore ref is recorded in
#: ``ContextPlan.cleared_outputs`` (internal audit), NOT here: the model has no
#: ref-deref tool, so a hash in this string is dead weight it could only misread.
#: The marker still says "there WAS output, it was elided" (not an empty string
#: that reads as "the tool returned nothing"), so the model knows to re-run the
#: tool if it needs the content back.
_CLEARED_MARKER = "[tool output cleared]"


def _clear_tool_outputs(
    msg: Message, store_full: Callable[[Any], ContentRef]
) -> tuple[Message, list[ContentRef]]:
    """Return a copy of ``msg`` with every ToolResultBlock output replaced by
    the lean cleared-marker, plus the ContentStore refs of the originals.

    Returns ``(message, cleared_refs)`` — ``cleared_refs`` has one entry per
    ToolResultBlock with a non-empty output that was cleared (empty ⇒ nothing
    cleared). ``store_full`` offloads the original output to the ContentStore
    and returns its ref; the caller records these in
    ``ContextPlan.cleared_outputs`` so the full body stays deref-able for audit
    WITHOUT leaking the hash into the model-facing marker. Already cleared /
    empty-output blocks are left untouched (idempotent: ``_CLEARED_MARKER`` is
    never re-wrapped, and an already-empty output — ``""``/``None``/``[]``/
    ``{}``/``0`` and any other falsy value — yields no extra ContentStore
    write, never a misleading "output cleared" marker over a genuinely empty
    tool result).
    Non-tool-result blocks (text / tool_use / thinking) are kept verbatim, so
    prune only swaps the bulky tool output for a marker, never the
    conversational structure.
    """
    cleared_refs: list[ContentRef] = []
    new_blocks: list[Any] = []
    for b in msg.content:
        if (
            isinstance(b, ToolResultBlock)
            and b.output
            and not _is_cleared_marker(b.output)
        ):
            cleared_refs.append(store_full(b.output))
            new_blocks.append(
                ToolResultBlock(
                    call_id=b.call_id,
                    output=_CLEARED_MARKER,
                    success=b.success,
                    error=b.error,
                )
            )
        else:
            new_blocks.append(b)
    if not cleared_refs:
        return msg, []
    # The pruned rebuild keeps ``origin`` (same transform discipline as
    # ``_reattach_thinking`` — source tags survive).
    return (
        Message(role=msg.role, content=new_blocks, origin=msg.origin),
        cleared_refs,
    )


def _is_cleared_marker(output: Any) -> bool:
    """True iff ``output`` is the lean cleared-marker string.

    Guards idempotency: re-composing a View whose tool outputs are already
    markers must not re-wrap them (which would also spawn a redundant
    ContentStore write). Exact match — a real tool output that merely starts
    with the marker text is not mistaken for an already-cleared one.
    """
    return output == _CLEARED_MARKER


def _text_blocks(prompt: str) -> list[Any]:
    # Local import to avoid a top-level cycle through messages → canonical.
    from noeta.protocols.messages import TextBlock

    return [TextBlock(text=prompt)]
