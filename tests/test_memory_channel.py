"""D5 — memory index lives on the content channel + auto-recall writes inline.

Second tenant on the content channel (reuses the 02/03 generic abstraction, zero runtime changes):

* Index resident — ``memory_content_kind`` is an ordinary
  :class:`ContentKindSpec` (kind=``memory``, policy ``evolving``), activated by
  recording ``ContextContentRecorded``, rendered into ``semi_stable`` by
  ``ThreeSegmentComposer``; the index hash flows through the generic
  ``ContentHashesFn`` seam.
* Auto-recall — host seam ``append_user_message_with_recall``: retrieval
  (impure, reads disk) happens before recording; a hit is recorded with
  ``origin="memory"``. Fold only re-plays the log and never re-runs
  retrieval (no matter how disk changes, the folded message bytes stay fixed).
"""

from __future__ import annotations

from pathlib import Path

from noeta.context.composer import ThreeSegmentComposer
from noeta.context.content_channel import ContentChannelRegistry
from noeta.builtins.memory.impl.index import (
    DEFAULT_RECALL_MAX_HITS,
    MEMORY_DRIFT_POLICY,
    MEMORY_INDEX_NAME,
    MEMORY_INDEX_VERSION,
    MEMORY_KIND,
    RECALL_BODY_MAX_BYTES,
    RECALL_TOTAL_MAX_BYTES,
    RecallHit,
    _tokens,
    build_memory_renderer,
    format_recall_text,
    match_memories,
    memory_content_kind,
    memory_index_hash,
    render_memory_index_text,
)
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.builtins.memory.impl.recall import (
    append_user_message_with_recall,
    recall_memories,
)
from noeta.builtins.memory.impl.store import (
    DEFAULT_GLOBAL_MEMORY_DIR,
    load_memory_store,
)
from noeta.execution.recorder import SeedRecorder
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import FinishDecision
from noeta.protocols.messages import ImageBlock, TextBlock
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.builtins.memory.impl.store import MemoryStore


_ENTRIES = (
    ("deploy-process", "How we deploy", ""),
    ("naming-rules", "Module naming conventions", ""),
)


# ---------------------------------------------------------------------------
# Index rendering + hash — pure functions
# ---------------------------------------------------------------------------


def test_index_text_lists_entries_and_mentions_read_tool() -> None:
    text = render_memory_index_text(_ENTRIES)
    assert "deploy-process" in text
    assert "How we deploy" in text
    assert "naming-rules" in text
    assert "memory_read" in text


def test_index_hash_is_stable_and_tracks_content() -> None:
    assert memory_index_hash(_ENTRIES) == memory_index_hash(_ENTRIES)
    changed = (*_ENTRIES, ("new-memory", "Something new", ""))
    assert memory_index_hash(changed) != memory_index_hash(_ENTRIES)


def test_index_bytes_unchanged_for_frontmatterless_store() -> None:
    """Entries without a type (every pre-frontmatter file) render bare —
    no annotation, no placeholder — and the recorded hash is the sha256
    of exactly the rendered bytes."""
    import hashlib

    entries = (*_ENTRIES, ("bare-name", "", ""))
    text = render_memory_index_text(entries)
    assert text == (
        "Long-term memory index. Each entry is one stored memory; call\n"
        "the 'memory_read' tool with a memory's name for its full text.\n"
        "When the user refers to past decisions, preferences, or earlier\n"
        "work, check this index before answering from scratch. A memory\n"
        "records what was true when it was written — verify anything that\n"
        "may have changed since before relying on it.\n"
        "\n"
        "- deploy-process: How we deploy\n"
        "- naming-rules: Module naming conventions\n"
        "- bare-name"
    )
    assert memory_index_hash(entries) == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def test_index_text_annotates_typed_entries() -> None:
    entries = (
        ("deploy-process", "How we deploy", "procedural"),
        ("me", "", "user"),
        ("naming-rules", "Module naming conventions", ""),
    )
    text = render_memory_index_text(entries)
    assert "- deploy-process (procedural): How we deploy" in text
    assert "- me (user)" in text
    assert "- naming-rules: Module naming conventions" in text  # untyped = v1 form


def _index_resolve(kind: str, name: str) -> bytes:
    """A fake ``resolve`` returning the index bytes the renderer expects (spec
    §6): the ledger's active hash would deref to exactly these bytes."""
    return render_memory_index_text(_ENTRIES).encode("utf-8")


def test_renderer_renders_one_user_message_when_index_active() -> None:
    rendered = build_memory_renderer(_ENTRIES)([MEMORY_INDEX_NAME], _index_resolve)
    assert len(rendered.messages) == 1
    msg = rendered.messages[0]
    assert msg.role == "user"
    assert "deploy-process" in msg.content[0].text


def test_renderer_renders_nothing_when_index_inactive() -> None:
    renderer = build_memory_renderer(_ENTRIES)
    # No resolve call when the index name is not active → empty, no store touch.
    assert renderer([], _index_resolve).messages == []
    assert renderer(["not-the-index"], _index_resolve).messages == []


def test_memory_kind_is_evolving_and_resolves_through_generic_seam() -> None:
    spec = memory_content_kind(_ENTRIES)
    assert spec.kind == MEMORY_KIND
    assert spec.policy == "evolving"
    resolve = ContentChannelRegistry([spec]).content_hashes()
    assert resolve(MEMORY_KIND, MEMORY_INDEX_NAME) == (
        MEMORY_INDEX_VERSION,
        memory_index_hash(_ENTRIES),
    )
    assert resolve(MEMORY_KIND, "unknown") is None
    assert resolve("skill", "anything") is None


# ---------------------------------------------------------------------------
# Recall matching — two tiers (name ≥1 token, then summary ≥2), deterministic
# ---------------------------------------------------------------------------


def test_match_hits_on_name_token_case_insensitive() -> None:
    assert match_memories(_ENTRIES, "How do we DEPLOY this?") == (
        "deploy-process",
    )


def test_match_no_hit_returns_empty() -> None:
    assert match_memories(_ENTRIES, "hello there") == ()


def test_match_multiple_hits_keep_index_order_and_cap() -> None:
    entries = tuple((f"topic-{i}", "s", "") for i in range(8))
    text = "about " + " ".join(f"topic-{i}" for i in range(8))
    hits = match_memories(entries, text, max_hits=3)
    assert hits == ("topic-0", "topic-1", "topic-2")


def test_match_two_letter_name_token_no_longer_hits() -> None:
    """The token floor is 3: a two-letter slug fragment is not evidence.

    Was 2, which let ``ci`` / ``db`` / any hyphenated tail fire tier-1 — a
    whole memory body inline — off a passing mention. The memory stays
    reachable through its real token, and through ``memory_search``.
    """
    entries = (("ci-pipeline", "CI notes", ""),)
    assert match_memories(entries, "the ci failed") == ()
    assert match_memories(entries, "the pipeline failed") == ("ci-pipeline",)


def test_match_ignores_stopword_only_overlap() -> None:
    """A message sharing only stopwords with a memory injects nothing.

    Both tiers read the same filtered tokens, so ``how-we-deploy`` does not
    hit on "how" (stopword) or "we" (two letters) — only on "deploy".
    """
    entries = (("how-we-deploy", "what the team should do for a release", ""),)
    assert match_memories(entries, "how are you? what should we do?") == ()
    assert match_memories(entries, "how do we deploy?") == ("how-we-deploy",)


def test_match_summary_needs_two_distinct_tokens() -> None:
    entries = (("mem-a", "release checklist for services", ""),)
    # Two distinct summary tokens in the text → tier-2 hit.
    assert match_memories(entries, "walk the release checklist") == ("mem-a",)
    # A single shared token is too noisy — no hit.
    assert match_memories(entries, "any release soon?") == ()
    # The same token twice is still ONE distinct token — no hit.
    assert match_memories(entries, "release release release") == ()


def test_match_name_hits_order_before_summary_hits() -> None:
    # ``zz-target`` sorts after the summary hit but its NAME matches, so
    # it must come first: tier 1 wholly precedes tier 2.
    entries = (
        ("aa-notes", "postgres connection pooling notes", ""),
        ("zz-target", "unrelated summary", ""),
    )
    text = "zz-target plus postgres connection tricks"
    assert match_memories(entries, text) == ("zz-target", "aa-notes")


def test_match_type_never_participates() -> None:
    entries = (("mem-a", "nothing shared", "procedural"),)
    assert match_memories(entries, "procedural knowledge please") == ()


def test_match_cap_spans_both_tiers() -> None:
    entries = (
        ("release-notes", "s", ""),
        ("release-plan", "s", ""),
        ("aa-other", "release checklist steps", ""),
    )
    hits = match_memories(entries, "release checklist steps", max_hits=2)
    # Both name hits fill the cap; the tier-2 hit is squeezed out.
    assert hits == ("release-notes", "release-plan")


# ---------------------------------------------------------------------------
# Host-seam scaffolding
# ---------------------------------------------------------------------------


def _runtime() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    return log, InMemoryContentStore(), disp


def _composer(
    cs: InMemoryContentStore, entries: tuple[tuple[str, str, str], ...]
) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="memory test agent",
        tools={},
        content_store=cs,
        content_renderers=ContentChannelRegistry(
            [memory_content_kind(entries)]
        ),
    )


def _store_with_memories(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(root=tmp_path / "memories")
    store.write("deploy-process", "How we deploy\n\nAlways run make deploy.")
    store.write("naming-rules", "Module naming conventions\n\nsnake_case.")
    return store


def _engine(
    log: InMemoryEventLog,
    cs: InMemoryContentStore,
    composer: ThreeSegmentComposer,
) -> Engine:
    return Engine(
        event_log=log,
        content_store=cs,
        composer=composer,
        policy=StubScriptedPolicy([FinishDecision(answer="done")]),
    )


# ---------------------------------------------------------------------------
# Index activation through the generic scoped recorder (spec §4.5)
# ---------------------------------------------------------------------------


def _activate_memory_index(log, cs, task, entries=_ENTRIES):
    """Activate the memory index resident through the generic scoped recorder.

    Mirrors the memory built-in's ``init`` hook (spec §4.5): serialise the
    entries the composer's renderer holds into the ContentStore and record the
    ref through the :class:`SeedRecorder`. ``ref.hash`` equals
    ``memory_index_hash(entries)`` by construction.
    """
    rec = SeedRecorder(log, cs, task, actor="plugin:memory")
    ref = cs.put(
        render_memory_index_text(entries).encode("utf-8"), media_type="text/markdown"
    )
    rec.record_content(
        kind=MEMORY_KIND,
        name=MEMORY_INDEX_NAME,
        version=MEMORY_INDEX_VERSION,
        ref=ref,
        policy=MEMORY_DRIFT_POLICY,
    )
    return rec.task


def test_activation_emits_evolving_event_and_activates() -> None:
    log, cs, _disp = _runtime()
    engine = _engine(log, cs, _composer(cs, _ENTRIES))
    task = engine.create_task(goal="g", policy_name="scripted")

    task = _activate_memory_index(log, cs, task)

    events = [
        e for e in log.read(task.task_id)
        if e.type == "ContextContentRecorded"
    ]
    assert len(events) == 1
    payload = events[0].payload
    assert payload.kind == MEMORY_KIND
    assert payload.name == MEMORY_INDEX_NAME
    assert payload.policy == "evolving"
    assert payload.content_hash == memory_index_hash(_ENTRIES)
    assert events[0].actor == "plugin:memory"
    assert task.state.active_content[MEMORY_KIND] == {
        MEMORY_INDEX_NAME: memory_index_hash(_ENTRIES)
    }


def test_compose_places_index_in_semi_stable_and_stays_pure() -> None:
    log, cs, _disp = _runtime()
    composer = _composer(cs, _ENTRIES)
    engine = _engine(log, cs, composer)
    task = engine.create_task(goal="g", policy_name="scripted")
    task = _activate_memory_index(log, cs, task)

    first = composer.compose(task)
    second = composer.compose(task)

    semi = [s for s in first.segments if s.name == "semi_stable"][0]
    assert len(semi.content) == 1
    assert render_memory_index_text(_ENTRIES) in semi.content[0].content[0].text
    # Red line: composing the same log twice yields byte-equal output.
    assert to_canonical_bytes(first.segments) == to_canonical_bytes(
        second.segments
    )


# ---------------------------------------------------------------------------
# Auto-recall — retrieval before recording, hit gets origin=memory, replay never re-runs
# ---------------------------------------------------------------------------


def test_recall_memories_reads_store_at_call_time(tmp_path: Path) -> None:
    store = _store_with_memories(tmp_path)
    hits = recall_memories(store, "how do we deploy?")
    assert [h.name for h in hits] == ["deploy-process"]
    assert hits[0].full and "make deploy" in hits[0].text
    # The injector may be impure: a memory written midway is immediately recallable.
    store.write("rollback-steps", "Rollback\n\nuse make rollback")
    hits = recall_memories(store, "rollback please")
    assert [h.name for h in hits] == ["rollback-steps"]


def test_recall_stops_seeing_archived_memory(tmp_path: Path) -> None:
    # Archiving moves the file under ``archive/``; the non-recursive
    # entries glob no longer lists it, so recall goes quiet immediately.
    store = _store_with_memories(tmp_path)
    assert recall_memories(store, "how do we deploy?")
    store.archive("deploy-process")
    assert recall_memories(store, "how do we deploy?") == ()
    assert (store.root / "archive" / "deploy-process.md").is_file()


def test_recall_hits_on_summary_tokens_after_name_misses(
    tmp_path: Path,
) -> None:
    # Tier 2 end-to-end: no name token in the text, but two summary tokens
    # hit. A prose guess rides as a POINTER — the summary, verbatim — and
    # the body is left for the model to fetch with memory_read.
    store = MemoryStore(root=tmp_path / "memories")
    store.write(
        "df-42",
        "---\ndescription: postgres connection pooling notes\n---\n"
        "Use pgbouncer.",
    )
    hits = recall_memories(store, "postgres connection keeps dropping")
    assert [h.name for h in hits] == ["df-42"]
    assert hits[0].full is False
    assert hits[0].text == "postgres connection pooling notes"
    assert "pgbouncer" not in format_recall_text(hits)  # body not spent


def test_recall_tier1_keeps_the_body_inline(tmp_path: Path) -> None:
    # The confidence split: a NAME match is the user naming the memory, so
    # it still costs a body and needs no extra round trip.
    store = MemoryStore(root=tmp_path / "memories")
    store.write("pgbouncer-setup", "---\ndescription: pooling\n---\nUse pgbouncer.")
    hits = recall_memories(store, "how is pgbouncer-setup configured?")
    assert [(h.name, h.full) for h in hits] == [("pgbouncer-setup", True)]
    assert "Use pgbouncer." in format_recall_text(hits)


def test_recall_oversized_body_degrades_to_a_pointer(tmp_path: Path) -> None:
    """A tier-1 hit bigger than the per-body cap rides as its index line.

    Recall is uninvited context, so an unbounded body is the model's problem
    and not its choice. The degradation is WHOLE — no truncated body — and it
    keeps the ``memory_read`` affordance, so nothing is lost but the bytes.
    """
    store = MemoryStore(root=tmp_path / "memories")
    big = "x" * (RECALL_BODY_MAX_BYTES + 1)
    store.write(
        "huge-runbook",
        f"---\ndescription: the long runbook\n---\n{big}",
    )
    hits = recall_memories(store, "walk me through the huge-runbook")
    assert [(h.name, h.full) for h in hits] == [("huge-runbook", False)]
    assert hits[0].text == "the long runbook"
    rendered = format_recall_text(hits)
    assert "Possibly relevant memories" in rendered
    assert "memory_read" in rendered
    assert big not in rendered
    # A body at exactly the cap still rides inline (the boundary is inclusive).
    store.write("just-fits", "y" * RECALL_BODY_MAX_BYTES)
    fitted = recall_memories(store, "check just-fits")
    assert [(h.name, h.full) for h in fitted] == [("just-fits", True)]


def test_recall_total_budget_degrades_the_tail(tmp_path: Path) -> None:
    """The turn's total inline budget is spent in hit order: the early
    (highest-confidence) hits keep their bodies, the tail degrades."""
    store = MemoryStore(root=tmp_path / "memories")
    body = "z" * (RECALL_BODY_MAX_BYTES - 100)
    for i in range(5):
        store.write(
            f"budget-note-{i}",
            f"---\ndescription: note {i}\n---\n{body}",
        )
    hits = recall_memories(store, "review every budget-note please")
    assert len(hits) == DEFAULT_RECALL_MAX_HITS
    inline = [h for h in hits if h.full]
    # 4 * (4096 - 100 + frontmatter) fits under 16384; the 5th cannot.
    assert [h.name for h in inline] == [f"budget-note-{i}" for i in range(4)]
    assert hits[4].name == "budget-note-4" and hits[4].full is False
    assert sum(len(h.text.encode("utf-8")) for h in inline) <= (
        RECALL_TOTAL_MAX_BYTES
    )


def test_recall_pointer_survives_an_empty_summary(tmp_path: Path) -> None:
    # A summary-less memory can only be reached by name (tier 1), so a
    # tier-2 pointer with no text is unreachable here; guard the renderer
    # against it directly — a bare name is still enough to read by.
    rendered = format_recall_text((RecallHit(name="df-42", text="", full=False),))
    assert "- df-42" in rendered
    assert "memory_read" in rendered


def test_recall_hit_appends_origin_memory_after_user_message(
    tmp_path: Path,
) -> None:
    store = _store_with_memories(tmp_path)
    log, cs, disp = _runtime()
    engine = _engine(log, cs, _composer(cs, store.entries()))
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w-mem")
    assert lease is not None

    task = append_user_message_with_recall(
        engine,
        task,
        content=[TextBlock(text="how do we deploy?")],
        lease_id=lease.lease_id,
        store=store,
    )

    messages = task.runtime.messages
    assert len(messages) == 2
    assert messages[0].origin is None  # human turn keeps its natural author
    assert messages[0].content[0].text == "how do we deploy?"
    assert messages[1].origin == "memory"
    assert "make deploy" in messages[1].content[0].text
    # fold matches live
    folded = fold(log, cs, task.task_id)
    assert to_canonical_bytes(folded.runtime.messages) == to_canonical_bytes(
        messages
    )


def test_recall_is_verbatim_copy_not_synthesis(tmp_path: Path) -> None:
    """Invariant — memory-retrieval ONLY copies stored text
    verbatim; it never reasons, paraphrases, or synthesises.

    The whole v1 retrieval path is deterministic copy by construction
    (token match on names → ``store.read`` → verbatim concat), with no
    LLM anywhere. This guard pins that contract so a future "smart recall"
    refactor that started summarising would fail loudly: every recalled
    body must appear as an EXACT substring of the file on disk, and the
    rendered recall turn must contain each body verbatim.
    """
    store = MemoryStore(root=tmp_path / "memories")
    # Bodies deliberately carry text that a summariser would be tempted to
    # rewrite (a list, an imperative, an exact path).
    store.write(
        "deploy-process",
        "How we deploy\n\n1. run `make build`\n2. run `make deploy`\n"
        "NEVER touch /etc/prod-secrets while deploying.",
    )
    store.write(
        "naming-rules",
        "Module naming conventions\n\nUse snake_case; avoid CamelCase.",
    )

    hits = recall_memories(store, "how do we deploy this module?")
    # Both memories may hit (name tokens "deploy" and "module"/"naming" —
    # at least deploy-process must), and every body returned is
    # byte-identical to the file on disk (no transformation).
    assert "deploy-process" in {h.name for h in hits}
    for hit in hits:
        on_disk = store.read(hit.name)
        assert on_disk is not None
        if hit.full:
            assert hit.text == on_disk  # verbatim, not paraphrased
        else:
            # A pointer is the index summary, also copied not written.
            assert hit.text in on_disk

    # The rendered recall turn carries each injected text verbatim (the
    # high-signal exact path survives — a synthesis step would drop it).
    rendered = format_recall_text(hits)
    for hit in hits:
        assert hit.text in rendered
    assert "/etc/prod-secrets" in rendered


def test_recall_key_from_text_only_image_rides_along(tmp_path: Path) -> None:
    """D5: the recall key uses only the TextBlock text
    in content — images still ride along in the human turn but don't drive recall.
    With or without an image, the same text recalls the same memories."""
    store = _store_with_memories(tmp_path)
    log, cs, disp = _runtime()
    engine = _engine(log, cs, _composer(cs, store.entries()))
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w-mem")
    assert lease is not None

    ref = cs.put(b"\x89PNG fake", media_type="image/png")
    task = append_user_message_with_recall(
        engine,
        task,
        content=[TextBlock(text="how do we deploy?"), ImageBlock(source=ref)],
        lease_id=lease.lease_id,
        store=store,
    )

    messages = task.runtime.messages
    assert len(messages) == 2
    # Human turn carries the text + image as-is.
    assert messages[0].content[0].text == "how do we deploy?"
    assert isinstance(messages[0].content[1], ImageBlock)
    assert messages[0].content[1].source == ref
    # The hit is driven by text (not the image): the same text recalls the deploy memory.
    assert messages[1].origin == "memory"
    assert "make deploy" in messages[1].content[0].text


def test_recall_miss_appends_only_the_user_message(tmp_path: Path) -> None:
    store = _store_with_memories(tmp_path)
    log, cs, disp = _runtime()
    engine = _engine(log, cs, _composer(cs, store.entries()))
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w-mem")
    assert lease is not None

    task = append_user_message_with_recall(
        engine, task, content=[TextBlock(text="hello")], lease_id=lease.lease_id, store=store
    )

    assert len(task.runtime.messages) == 1
    assert task.runtime.messages[0].origin is None


def test_replay_never_reruns_retrieval_bytes_equal(tmp_path: Path) -> None:
    """Replay red line: retrieval results are recorded; however disk changes later, fold/compose read only the log."""
    store = _store_with_memories(tmp_path)
    entries_at_record = store.entries()
    log, cs, disp = _runtime()
    composer = _composer(cs, entries_at_record)
    engine = _engine(log, cs, composer)
    task = engine.create_task(goal="g", policy_name="scripted")
    task = _activate_memory_index(log, cs, task, entries=entries_at_record)
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w-mem")
    assert lease is not None
    task = append_user_message_with_recall(
        engine,
        task,
        content=[TextBlock(text="how do we deploy?")],
        lease_id=lease.lease_id,
        store=store,
    )
    live_view_bytes = to_canonical_bytes(composer.compose(task).segments)
    live_messages_bytes = to_canonical_bytes(task.runtime.messages)

    # Destroy the memory dir — replay must be unaffected.
    for path in list(store.root.iterdir()):
        path.unlink()
    store.root.rmdir()

    replayed = fold(log, cs, task.task_id)
    assert to_canonical_bytes(replayed.runtime.messages) == (
        live_messages_bytes
    )
    # The composer closure holds a snapshot and never reads disk: same log, equal bytes.
    assert to_canonical_bytes(composer.compose(replayed).segments) == (
        live_view_bytes
    )


# ---------------------------------------------------------------------------
# Host load defaults — zero footprint when unconfigured
# ---------------------------------------------------------------------------


def test_load_memory_store_uses_given_global_root(tmp_path: Path) -> None:
    # Root is a fixed global dir passed in at the agent layer, independent of workspace.
    root = tmp_path / "global-memories"
    store = load_memory_store(root=root)
    assert store.root == root
    assert store.entries() == ()  # missing dir = empty, no error


def test_default_global_memory_dir_is_under_home() -> None:
    # The default global memory dir is pinned to ~/.noeta/memories (independent of the per-session workspace).
    assert DEFAULT_GLOBAL_MEMORY_DIR == Path("~/.noeta/memories").expanduser()


def test_format_recall_text_contains_names_and_bodies() -> None:
    text = format_recall_text(
        (RecallHit("deploy-process", "Always run make deploy.", True),)
    )
    assert "deploy-process" in text
    assert "Always run make deploy." in text


def test_format_recall_text_separates_bodies_from_pointers() -> None:
    text = format_recall_text(
        (
            RecallHit("deploy-process", "Always run make deploy.", True),
            RecallHit("df-42", "postgres pooling notes", False),
        )
    )
    # Bodies first under their own heading, pointers in a trailing list
    # that names the tool to get the rest.
    assert text.index("## deploy-process") < text.index("- df-42:")
    assert "Always run make deploy." in text
    assert "memory_read" in text


# ---------------------------------------------------------------------------
# Tokenisation — space-free scripts (the bug: recall was dead for them)
# ---------------------------------------------------------------------------


def test_cjk_text_produces_bigram_tokens() -> None:
    # Before: the word rule found nothing here and match_memories
    # early-returned on an empty token set, so recall was silently dead
    # for every wholly-Chinese message.
    assert _tokens("记忆机制") == {"记忆", "忆机", "机制"}
    assert _tokens("钱") == {"钱"}  # a lone character still tokenises


def test_ascii_tokenisation_drops_stopwords_and_short_words() -> None:
    # "the" is a stopword, "ci" and "a" are under the 3-character floor.
    assert _tokens("Deploy the CI pipeline, a step") == {
        "deploy", "pipeline", "step",
    }


def test_token_filters_never_touch_cjk_bigrams() -> None:
    """The floor is the WORD rule's alone — every bigram is 2 characters, so
    sharing the filter would delete the space-free path outright."""
    assert _tokens("记忆机制") == {"记忆", "忆机", "机制"}
    # Mixed text: the word half is filtered, the CJK half is not.
    assert _tokens("the 记忆 and deploy") == {"deploy", "记忆"}


def test_mixed_script_text_tokenises_both_halves() -> None:
    assert _tokens("这个 memory 怎么做") == {
        "memory", "这个", "怎么", "么做",
    }


def test_cjk_recall_matches_a_cjk_memory() -> None:
    entries = (("记忆检索设计", "分层注入与二元组匹配", "project"),)
    assert match_memories(entries, "记忆检索到底怎么做的") == ("记忆检索设计",)
    assert match_memories(entries, "完全无关的问题") == ()


def test_cjk_query_does_not_reach_an_english_memory() -> None:
    # Honest limit of token matching: bigrams cannot bridge languages. A
    # Chinese question never finds a wholly English memory — that gap
    # needs semantic retrieval, not a wider tokeniser.
    entries = (("memory-recall", "tiered recall injection", "project"),)
    assert match_memories(entries, "记忆检索怎么做") == ()
