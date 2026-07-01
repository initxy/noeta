"""ContextComposer implementations (L2).

:class:`ThreeSegmentComposer` is the
``stable_prefix / semi_stable / dynamic_suffix`` assembly. It is the single
in-tree Composer implementation. Tests that only need a "any
valid Composer" instance use :func:`noeta.testing.composer.trivial_three_segment`.

Composer is a pure function: no LLM, no clock, no
randomness, no network IO. The only side effect is writing the
ContextPlan body into the injected ``ContentStore`` so the Engine has
a ``ContentRef`` to attach onto the next ``ContextPlanComposed``
envelope (``ContextState.plan_ref``
is fold-derived from that event — Composer never writes ``task.context``
directly, preserving the single-writer invariant).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

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
    "RenderedSkills",
    "RetrievedResource",
    "SkillRenderer",
    "ThreeSegmentComposer",
]


# The composed-bytes history this version string went through (v2 → v3 → v4 →
# v5): Phase 4.5 Issue D let the composer inline skill referenced-file content;
# ③ (D-3e) added prune (clearing tool outputs outside the tail window) plus the
# compaction-summary swap; then changed prune
# to clear a tool output to a cleared-marker; v5 made that marker LEAN — the
# full body's ContentStore ref moved OUT of the model-facing string into
# ``ContextPlan.cleared_outputs`` (the model has no ref-deref tool, so a hash in
# the marker was dead weight it could only misread). The version is kept purely
# as a trace/inspect display label (the trace page shows it); there is no longer
# any consumer that forces a bump when the composed bytes shift — bumping it is
# now optional bookkeeping, not a contract.
_COMPOSER_VERSION = "three_segment.v5"
_PLAN_MEDIA_TYPE = "application/json"
#: ③ (D-3c) — the neutral role of the single summary message swapped in for
#: the covered prefix; ``user`` keeps it provider-neutral and outside the
#: ``system`` stable_prefix.
_SUMMARY_ROLE = "user"
#: Model-visible name of the ``spawn_subagent`` control tool. Source of truth is
#: ``noeta.policies.control_semantics.SPAWN_SUBAGENT_TOOL``; duplicated here as a
#: literal to keep the Composer free of a ``noeta.policies`` import (layering). The
#: concurrency reminder gates on this name appearing in ``provider_tool_schemas``.
_SPAWN_SUBAGENT_TOOL_NAME = "spawn_subagent"


@dataclass(frozen=True, slots=True)
class RetrievedResource:
    """One body-referenced skill resource considered for inlining
    (Phase 4.5 Issue D), returned by the renderer for the Composer to
    persist into provenance.

    ``reason="referenced"`` ⇒ the resource passed every boundary check
    and its ``raw_bytes`` were inlined into the ``semi_stable`` segment;
    the Composer puts ``raw_bytes`` into the ContentStore for the
    provenance ``content_ref``. A ``skipped:*`` reason ⇒ the resource was
    referenced but failed a boundary check (binary / too_large /
    symlink_escape); ``raw_bytes`` is ``None`` and nothing entered the
    prompt. The renderer reads disk but never touches the ContentStore —
    persistence is the Composer's job (single writer).
    """

    skill: str
    relpath: str
    reason: str
    media_type: Optional[str] = None
    raw_bytes: Optional[bytes] = None


@dataclass(frozen=True, slots=True)
class RenderedSkills:
    """Output of a :data:`SkillRenderer` call.

    Issue 21 widens the renderer seam: a renderer must now hand back
    both the rendered ``Message`` list **and** the post-filter,
    post-sort skill name list. Composer writes ``selected_skills`` into
    the ``ContextPlan`` body verbatim — the renderer is the single
    source of truth for "what skill activations actually landed in
    this View", so Composer cannot re-derive the list from raw
    ``task.state.active_skills`` (which may include unknown names or
    arrive in a different order from the rendered output).

    Issue D adds ``retrieved_resources`` — the body-referenced resources
    the renderer inlined (or skipped) for the active skills, in
    deterministic ``(skill, relpath)`` order. Defaulted empty so a
    renderer that does no retrieval is unchanged.
    """

    messages: list[Message]
    selected_skills: list[str]
    retrieved_resources: list[RetrievedResource] = field(default_factory=list)


SkillRenderer = Callable[[list[str]], RenderedSkills]


#: The content channel's resident kind for skills. The
#: composer keys one narrow skill-only behaviour on it: the renderer's
#: post-resolve name list feeds ``ContextPlan.selected_skills`` (a
#: skill-named plan field kept for plan-body byte stability).
_SKILL_KIND = "skill"


class ContentRenderers(Protocol):
    """The composer-facing surface of a content-channel registry.

    One render rule per registered kind; ``kinds()`` order IS the
    ``semi_stable`` layout. The concrete implementation is
    :class:`noeta.context.content_channel.ContentChannelRegistry`
    (declared structurally here so ``composer`` does not import its own
    consumer). A renderer must stay a pure function of the names fold
    derived from the ledger — no compose-time fetches (the red line).
    """

    def kinds(self) -> tuple[str, ...]: ...

    def render(self, kind: str, names: list[str]) -> RenderedSkills: ...


class _SkillOnlyRenderers:
    """Adapter: one legacy ``skill_renderer`` as a single-kind registry.

    Keeps the long-standing ``ThreeSegmentComposer(skill_renderer=…)``
    construction working byte-identically while the compose path itself
    is registry-shaped (the skill renderer is just the ``kind="skill"``
    item). Hosts wanting more kinds pass a real registry instead.
    """

    def __init__(self, renderer: SkillRenderer) -> None:
        self._renderer = renderer

    def kinds(self) -> tuple[str, ...]:
        return (_SKILL_KIND,)

    def render(self, kind: str, names: list[str]) -> RenderedSkills:
        return self._renderer(names)


def _default_skill_renderer(_: list[str]) -> RenderedSkills:
    """Default no-op renderer.

    Returned when ``ThreeSegmentComposer`` is constructed without an
    explicit ``skill_renderer``. Yields an empty semi_stable segment
    and an empty ``selected_skills`` list — Composer still folds an
    empty list into the ``ContextPlan`` body, but no skill body bytes
    enter the View. Wire :func:`noeta.context.skills.build_skill_renderer`
    (issue 21) when real Skill activation is wanted.
    """
    return RenderedSkills(messages=[], selected_skills=[])


def _sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class ThreeSegmentComposer:
    """Three-segment Composer.

    Construct once per Task family (system_prompt + tool set is fixed
    over a Task's lifetime). The composer is a pure function (no clock,
    no randomness), stateless across compose calls — the same Task state
    always composes the same View. The ``stable_prefix`` and ``semi_stable``
    segments must additionally serialize reproducibly across steps to stay
    cache-friendly: churning them busts the provider's prompt / KV cache and
    spikes cost (see CONTEXT.md "Stable Prefix").
    """

    def __init__(
        self,
        *,
        system_prompt: str,
        tools: dict[str, Tool],
        content_store: ContentStore,
        skill_renderer: Optional[SkillRenderer] = None,
        content_renderers: Optional[ContentRenderers] = None,
        control_action_schemas: Optional[list[dict[str, Any]]] = None,
        tail_token_budget: Optional[int] = None,
        available_window: Optional[int] = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._tools = dict(tools)
        self._content_store = content_store
        # The semi_stable segment is rendered
        # through a content-kind registry. ``skill_renderer`` remains as
        # construction sugar for the single-kind case — it becomes the
        # ``kind="skill"`` item of an implicit registry, so both paths
        # produce byte-identical output. Passing both is ambiguous (the
        # skill item would exist twice); register skill in the registry
        # instead.
        if skill_renderer is not None and content_renderers is not None:
            raise ValueError(
                "pass either skill_renderer or content_renderers, not both; "
                "register the skill kind as a registry item instead"
            )
        self._content_renderers: ContentRenderers = (
            content_renderers
            if content_renderers is not None
            else _SkillOnlyRenderers(skill_renderer or _default_skill_renderer)
        )
        # ③ (D-3e): the protected tail-window size in *estimated tokens*
        # (D-3d). When set, tool-result outputs of messages older than this
        # budget are cleared to an explicit cleared-marker (deterministic
        # prune); ``None`` keeps the legacy "no prune" behaviour
        # (every output passes through). The
        # budget is in neutral estimate units — not a hard-coded message
        # count — so the kept window scales with how big the recent turns
        # actually are. Provider-neutral.
        self._tail_token_budget = tail_token_budget
        # ③ (D-3e relief-valve gate): the model's usable window in estimated
        # tokens (``context_window - max_output - buffer`` — the SAME quantity
        # the Policy's summarize trigger compares against). When set, prune is a
        # RELIEF VALVE, not an always-on clamp: tool outputs stay verbatim until
        # the composed history actually approaches this window, so a half-empty
        # window never forces the model to re-run a tool to recover content it
        # already fetched. ``None`` keeps the legacy always-prune-to-tail
        # behaviour. Deterministic (a pure function of the model), so live +
        # resume gate identically.
        self._available_window = available_window
        # Phase 4.5 Issue C — provider-visible **control** action schemas
        # that are NOT executable workspace tools (e.g. ``spawn_subagent``,
        # which the policy translates into a Decision and the ToolRuntime
        # never invokes). They are appended to ``View.provider_tool_schemas`` after
        # the real tools — so the prompt tool surface (and the
        # ``stable_prefix`` hash + ``ContextPlan``) faithfully reflects
        # what the provider sees — while staying absent from the Engine
        # tools dict / ToolRuntime. Order is deterministic.
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
        # narrative prompt text is identical (PRD §"Grill round 1 #1").
        stable_hash = _sha256_hex(
            to_canonical_bytes((stable_content, provider_tool_schemas))
        )

        # Render the semi_stable residents from
        # the generic activation map through the kind registry, in
        # registration order. The skill kind keeps one narrow extra:
        # its post-resolve name list feeds ``ContextPlan.selected_skills``
        # (kept skill-named for plan-body byte stability).
        semi_content: list[Message] = []
        selected_skills: list[str] = []
        renderer_resources: list[RetrievedResource] = []
        for kind in self._content_renderers.kinds():
            rendered = self._content_renderers.render(
                kind, self._active_names(task, kind)
            )
            semi_content.extend(rendered.messages)
            if kind == _SKILL_KIND:
                selected_skills = list(rendered.selected_skills)
            renderer_resources.extend(rendered.retrieved_resources)
        semi_hash = _sha256_hex(to_canonical_bytes(semi_content))

        # Issue D: persist the renderer's referenced-resource provenance.
        # The Composer is the single writer of the ContentStore + plan:
        # a ``referenced`` resource's raw bytes go to the store (the
        # content was already inlined into ``semi_content`` by the
        # renderer, so it is in ``semi_hash``); a ``skipped`` resource
        # carries no ``content_ref`` and never entered the prompt.
        retrieved_resources = self._persist_retrieved(renderer_resources)

        # ③ (D-3c): swap the compacted prefix for the summary message, then
        # re-attach extended-thinking (Slice C) ahead of each assistant
        # turn's tool_use, then ③ (D-3e): prune tool outputs older than the
        # protected tail window.
        dynamic_source = self._reattach_thinking(
            self._apply_summary(task), task
        )
        dynamic_content, selected_refs, dropped_refs, cleared_refs = (
            self._prune_tail(dynamic_source)
        )
        # #1 todo re-injection:
        # ``TaskState.todos`` is otherwise write-only — the model writes a
        # checklist through ``todo_write`` but never sees it again, so multi-step
        # planning goes blind. Append a compose-time ``<system-reminder>`` listing
        # the unfinished todos to the END of the dynamic_suffix (the volatile
        # segment), then hash. This is a View-only product: it is NOT written to
        # ``runtime.messages`` and emits no event, so it never enters the folded
        # truth. It is regenerated from the folded ``TaskState.todos`` on every
        # compose ⇒ resume reproduces it automatically, and because it lands in
        # dynamic_content only it cannot churn the stable_prefix / semi_stable
        # hashes the prompt cache rides on.
        dynamic_content = self._append_todo_reminder(task, dynamic_content)
        # Layer-3 fan-out nudge (mirrors Claude Code's compose-time concurrency
        # reminder): while delegation is offered AND the model has not spawned
        # yet, restate at the tail that parallel delegation = multiple
        # ``spawn_subagent`` calls in one turn. See ``_append_concurrency_reminder``.
        delegation_enabled = any(
            isinstance(schema, dict)
            and schema.get("function", {}).get("name")
            == _SPAWN_SUBAGENT_TOOL_NAME
            for schema in provider_tool_schemas
        )
        dynamic_content = self._append_concurrency_reminder(
            task, dynamic_content, delegation_enabled=delegation_enabled
        )
        # ⑥ compaction thrashing nudge (mirrors Claude Code's "Autocompact is
        # thrashing" hint): when fold has latched ``ContextState.compaction_thrashing``
        # (several compactions within a few turns of each other), suggest a
        # different read strategy. See ``_append_compaction_thrashing_reminder``.
        dynamic_content = self._append_compaction_thrashing_reminder(
            task, dynamic_content
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
            composer_version=_COMPOSER_VERSION,
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
            # ③ (finding 2): expose the RAW rolling history + the prior
            # cumulative summary boundary so a compaction-aware Policy computes
            # its new boundary in the SAME coordinate space the Composer's
            # ``_apply_summary`` slices (``task.runtime.messages`` /
            # ``ContextState.summary_boundary``). Without this the Policy would
            # count over ``iter_messages()`` — the post-summary, post-prune,
            # skill-prefixed, tail-truncated projection — and the two boundaries
            # would point at different messages whenever ``semi_stable`` is
            # non-empty or a prior summary already collapsed a prefix.
            rolling_history=list(task.runtime.messages),
            summary_boundary=task.context.summary_boundary,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _active_names(self, task: Task, kind: str) -> list[str]:
        """The active resident names for ``kind``, read from the generic
        activation map ``TaskState.active_content``.

        The map is the SOLE name source since the issue-07 generation
        switch (the legacy ``active_skills`` sugar bridge died with it).
        Every fold-derived state keeps the map in lockstep with the sugar
        list — the patch sugar mirrors both ways, the old skill event
        merges in, and pre-generic snapshot bodies are seeded at
        rehydrate — so any folded stream composes the identical bytes the
        bridge produced.
        """
        return list(task.state.active_content.get(kind, ()))

    def _persist_retrieved(
        self, resources: list[RetrievedResource]
    ) -> list[dict[str, Any]]:
        """Turn the renderer's :class:`RetrievedResource`s into the
        ``ContextPlan.retrieved_resources`` provenance dicts (Issue D).

        A ``referenced`` resource's raw bytes are stored (content-
        addressed, so the same bytes always yield the same ``content_ref``);
        a ``skipped`` resource gets ``content_ref=None`` and never had its
        bytes inlined. Order is preserved (the renderer already sorts by
        ``(skill, relpath)``).
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
        """③ (D-3c): swap the compacted prefix for the summary message.

        When ``ContextState.summary_ref`` is set (written by fold's
        ``Compacted`` handler), the first ``summary_boundary`` messages of
        the rolling history have been collapsed into one summary. We drop
        that covered prefix and prepend a single provider-neutral summary
        message (role ``user``), preserving ``stable_prefix``. Pure +
        deterministic: same task state → same message list.
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
        """Slice C: re-attach extended-thinking ahead of each assistant
        turn's ``tool_use``.

        ``ContextState.thinking_by_call_id`` (written by fold from
        ``AssistantThinkingRecorded``) maps an assistant turn's first
        ``tool_use`` ``call_id`` to the ThinkingBlocks ``_strip_thinking``
        removed before the turn was persisted. For every assistant message
        whose first ``tool_use`` ``call_id`` has stored thinking, we prepend
        those blocks so the View carries the full ``thinking → tool_use``
        shape an Anthropic continuation request needs (the signature must
        round-trip verbatim).

        Provider-neutral: the composer always re-attaches into
        the neutral View; the OUTBOUND gating lives in each adapter (the
        Anthropic adapter serializes thinking+signature; ``OpenAICompat``
        drops it per ``reasoning_continuation``). Pure (no clock, no
        randomness): same task state → same message list. Empty slice
        (OpenAI / non-reasoning / old recording) is an identity passthrough,
        so the dynamic bytes — and ``dynamic_hash`` — match the stripped
        history unchanged.
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
            # The rebuild keeps ``origin`` — the message-stream
            # source tag must survive every composer transform (byte-safe:
            # ``origin=None`` is omitted from canonical serialization).
            out.append(
                Message(
                    role=msg.role,
                    content=[*thinking, *msg.content],
                    origin=msg.origin,
                )
            )
        return out

    def _append_todo_reminder(
        self, task: Task, dynamic_content: list[Message]
    ) -> list[Message]:
        """#1: append a ``<system-reminder>`` listing the unfinished todos.

        ``TaskState.todos`` is a replace-all checklist of ``{id, content,
        status}`` (status ∈ ``pending`` / ``in_progress`` / ``completed``,
        ``control_semantics.TODO_WRITE_STATUSES``). It is folded state but,
        without this re-injection, never re-enters the prompt — the model is
        blind to its own plan after writing it. We surface only the *unfinished*
        items (anything not ``completed``); an empty list or an all-completed
        list yields no reminder (return the input list unchanged) so a finished
        checklist does not nag.

        The reminder is a single ``Message(role="user", origin="system")``
        carrying one ``TextBlock``. ``origin="system"`` makes each provider
        adapter wrap it in ``<system-reminder>`` tags and merge it into the
        adjacent user wire turn (the existing ``Message.origin`` rendering
        mechanism — no adapter change here). It is appended only to the View's
        ``dynamic_content``; it is never written to ``runtime.messages`` and
        emits no event (D6 red line), so it stays out of the folded truth and is
        re-derived from ``TaskState.todos`` on every compose (resume reproduces
        it). Pure: same todos → same reminder bytes.
        """
        unfinished = [
            t
            for t in task.state.todos
            if isinstance(t, dict) and t.get("status") != "completed"
        ]
        if not unfinished:
            return dynamic_content
        lines = [
            f"- [{t.get('status', 'pending')}] {t.get('content', '')}"
            for t in unfinished
        ]
        # Plain text only — NO ``<system-reminder>`` tag here. The tag is an
        # Anthropic-specific idiom synthesized by the adapter at wire-build time
        # for ``origin="system"`` turns (anthropic adapter wraps; the OpenAI
        # adapters render a native mid-history ``role="system"`` message). Baking
        # the literal tag into this neutral View leaks it to every provider —
        # Anthropic double-wraps it, OpenAI ships the stray tag inside its system
        # message. Keep the View provider-neutral and let each adapter speak its
        # own dialect.
        reminder = (
            "Your current todo list (unfinished items only). Keep it updated as "
            "you make progress — mark items in_progress / completed via "
            "todo_write so it stays accurate:\n"
            + "\n".join(lines)
        )
        return [
            *dynamic_content,
            Message(
                role="user",
                content=[TextBlock(text=reminder)],
                origin="system",
            ),
        ]

    def _append_concurrency_reminder(
        self,
        task: Task,
        dynamic_content: list[Message],
        *,
        delegation_enabled: bool,
    ) -> list[Message]:
        """Layer-3 just-in-time fan-out nudge — mirrors Claude Code's
        ``isInitial && showConcurrencyNote`` compose-time reminder.

        The stable prefix already carries the parallel-spawn rule (``main.md``
        rule 10 + the ``spawn_subagent`` description's MUST), but a model attends
        most to the tail. So while delegation is offered AND the model has not
        yet spawned any sub-agent, append a ``<system-reminder>`` at the very end
        of the dynamic_suffix restating that parallel delegation means multiple
        ``spawn_subagent`` calls in ONE turn — the exact failure mode where a
        model declares parallel intent then spawns one at a time (each single
        spawn suspends the parent, so one-per-turn is strictly sequential).

        Self-limiting: once the first ``spawn_subagent`` lands in the rolling
        history the nudge disappears, so a long delegation run is not nagged
        every turn. Like ``_append_todo_reminder`` it is a View-only product —
        ``role="user"`` / ``origin="system"`` (adapters wrap it in
        ``<system-reminder>``), appended to ``dynamic_content`` only, never
        written to ``runtime.messages`` and emitting no event, so it stays out of
        the folded truth, rides only the volatile (uncached) segment, and is
        re-derived every compose (resume reproduces it). Pure: same inputs → same
        bytes.
        """
        if not delegation_enabled:
            return dynamic_content
        already_spawned = any(
            isinstance(block, ToolUseBlock)
            and block.tool_name == _SPAWN_SUBAGENT_TOOL_NAME
            for message in task.runtime.messages
            for block in (getattr(message, "content", None) or [])
        )
        if already_spawned:
            return dynamic_content
        # Plain text only — the ``<system-reminder>`` tag is the adapter's job
        # (see ``_append_todo_reminder``). A hardcoded tag here double-wraps on
        # Anthropic and leaks into OpenAI's system message.
        reminder = (
            "When you delegate independent work to sub-agents, send them in a "
            "single message with multiple spawn_subagent tool calls so they run "
            "concurrently. Spawning one per turn is sequential, not parallel."
        )
        return [
            *dynamic_content,
            Message(
                role="user",
                content=[TextBlock(text=reminder)],
                origin="system",
            ),
        ]

    def _append_compaction_thrashing_reminder(
        self, task: Task, dynamic_content: list[Message]
    ) -> list[Message]:
        """⑥: append a ``<system-reminder>`` suggesting a different read
        strategy when compaction is thrashing.

        Mirrors Claude Code's "Autocompact is thrashing" nudge. fold latches
        ``ContextState.compaction_thrashing`` when several compactions land
        within a few turns of each other (a single large file / large tool
        output keeps refilling the window, so compaction spins without buying
        headroom — see ``fold._on_compacted``). When the flag is set, append a
        trailing reminder suggesting chunked reads / reading only the relevant
        section / extracting the key points before re-reading. Advisory tone, not
        an error — the model is free to ignore it (R4).

        Like ``_append_todo_reminder`` it is a View-only product — a single
        ``Message(role="user", origin="system")`` (adapters wrap it in
        ``<system-reminder>``), appended to ``dynamic_content`` only, never
        written to ``runtime.messages`` and emitting no event, so it stays out of
        the folded truth, rides only the volatile (uncached) segment, and is
        re-derived from the folded flag every compose (resume reproduces it). The
        flag clears in fold the moment a non-close compaction resets the run, so
        the reminder disappears on its own — the composer only ever reads it.
        Pure: same flag → same bytes.
        """
        if not task.context.compaction_thrashing:
            return dynamic_content
        # Plain text only — the ``<system-reminder>`` tag is the adapter's job
        # (see ``_append_todo_reminder``). A hardcoded tag here double-wraps on
        # Anthropic and leaks into OpenAI's system message.
        reminder = (
            "The context window keeps getting refilled to the limit by what "
            "looks like a single large file or large tool output, so compaction "
            "is spinning without freeing real headroom. Consider a different "
            "reading strategy: read in chunks, read only the relevant section, "
            "or extract the key points once and re-read on demand instead of "
            "pulling the whole large content back into context each time."
        )
        return [
            *dynamic_content,
            Message(
                role="user",
                content=[TextBlock(text=reminder)],
                origin="system",
            ),
        ]

    def _prune_tail(
        self, messages: list[Message]
    ) -> tuple[
        list[Message], list[ContentRef], list[ContentRef], list[ContentRef]
    ]:
        """③ (D-3e): clear tool-result outputs outside the tail window.

        Walks from the newest message backward, accumulating the estimated
        token cost (D-3d). Once the accumulated cost exceeds
        ``tail_token_budget``, every older message that carries a
        :class:`ToolResultBlock` has its ``output`` replaced by an explicit
        cleared-marker: the block keeps its
        ``call_id`` / ``role`` / ``success`` so the conversation stays
        well-formed, and the marker is LEAN (``[tool output cleared]``, no
        hash) so the model reads "there WAS content here, it was elided" —
        never an empty string that reads as "the tool returned nothing", and
        never a ContentStore hash it has no tool to deref. The original
        output's ref is returned (4th tuple element) for
        ``ContextPlan.cleared_outputs`` so the full body stays
        audit-deref-able OFF the prompt. Text / tool_use blocks are never
        touched. Returns the rewritten list plus the kept / cleared message
        refs and the cleared outputs' refs for ``ContextPlan`` provenance.

        Pure (no clock, no randomness): the token estimate is the stable
        heuristic, and each cleared ref is content-addressed (same output
        bytes → same ref) so the same history always produces the same
        markers + refs. ``tail_token_budget is None`` → no prune (legacy
        passthrough), refs empty. ``available_window`` arms the relief-valve
        gate: while the composed history estimate is below it, the whole history
        passes through verbatim (refs empty) — clearing only kicks in once the
        history approaches the usable window, so a half-empty window never
        forces a tool re-read.
        """
        if self._tail_token_budget is None:
            return messages, [], [], []
        # ③ (D-3e relief-valve gate): prune is a relief valve, not an always-on
        # clamp. Below the usable window the whole history stays verbatim — a
        # half-empty window must NOT clear tool outputs, or the model loses
        # content it already fetched and re-runs the read (the thrash this gate
        # fixes). We only start clearing once the composed history approaches
        # ``available_window``, mirroring the Policy's ``estimated >= window``
        # summarize trigger so the two compaction layers share one water mark.
        # Pure: the estimate is the stable chars/4 heuristic and the window is a
        # deterministic function of the model, so live + resume agree.
        if (
            self._available_window is not None
            and estimate_messages_tokens(messages) < self._available_window
        ):
            return messages, [], [], []
        budget = self._tail_token_budget
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
        # Issue C: control action schemas (e.g. spawn_subagent) follow the real
        # executable tools, deterministically, so they are visible to the provider
        # and folded into the stable hash without being executable tools.
        provider_tool_schemas = executable_tool_schemas
        provider_tool_schemas.extend(self._control_action_schemas)
        return provider_tool_schemas


def _first_tool_use_call_id(msg: Message) -> Optional[str]:
    """Return the ``call_id`` of the first ToolUseBlock in ``msg``, or None.

    This is the stable per-turn identity the thinking slice is keyed by:
    an Anthropic assistant turn emits its thinking ahead of (possibly
    several parallel) ``tool_use`` blocks, so the first one's id anchors the
    whole turn's reasoning deterministically (content order is preserved).
    """
    for b in msg.content:
        if isinstance(b, ToolUseBlock):
            return b.call_id
    return None


#: The lean placeholder a pruned tool
#: output is replaced by. Pure ASCII, no hash — the full body's ContentStore
#: ref is recorded in ``ContextPlan.cleared_outputs`` (internal audit), NOT
#: here: the model has no ref-deref tool, so a hash in this string is dead
#: weight it could only misread. The marker still says "there WAS output, it
#: was elided" (not an empty string that reads as "the tool returned nothing"),
#: so the model knows to re-run the tool if it needs the content back.
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
    # The pruned rebuild keeps ``origin`` (same transform
    # discipline as ``_reattach_thinking`` — source tags survive).
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
