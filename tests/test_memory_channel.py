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
from noeta.context.memory import (
    MEMORY_INDEX_NAME,
    MEMORY_INDEX_VERSION,
    MEMORY_KIND,
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
from noeta.execution.memory import (
    DEFAULT_GLOBAL_MEMORY_DIR,
    append_user_message_with_recall,
    load_memory_store,
    recall_memories,
    record_memory_index,
)
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import FinishDecision
from noeta.protocols.messages import ImageBlock, TextBlock
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.tools.memory import MemoryStore


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
    """v2 compat contract: entries without a type (every pre-frontmatter
    file) must render — and therefore hash — byte-identically to v1."""
    import hashlib

    entries = (*_ENTRIES, ("bare-name", "", ""))
    text = render_memory_index_text(entries)
    assert text == (
        "Long-term memory index. Each entry is one stored memory; call\n"
        "the 'memory_read' tool with a memory's name for its full text.\n"
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


def test_renderer_renders_one_user_message_when_index_active() -> None:
    rendered = build_memory_renderer(_ENTRIES)([MEMORY_INDEX_NAME])
    assert len(rendered.messages) == 1
    msg = rendered.messages[0]
    assert msg.role == "user"
    assert "deploy-process" in msg.content[0].text


def test_renderer_renders_nothing_when_inactive_or_empty() -> None:
    renderer = build_memory_renderer(_ENTRIES)
    assert renderer([]).messages == []
    assert renderer(["not-the-index"]).messages == []
    # Empty memory dir = zero footprint: nothing rendered even if the name is active.
    assert build_memory_renderer(())([MEMORY_INDEX_NAME]).messages == []


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


def test_match_short_name_tokens_still_hit() -> None:
    assert match_memories(
        (("ci-pipeline", "CI notes", ""),), "the ci failed"
    ) == ("ci-pipeline",)


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
# record_memory_index — records and activates; second tenant via 02/03 generics
# ---------------------------------------------------------------------------


def test_record_memory_index_emits_evolving_event_and_activates() -> None:
    log, cs, _disp = _runtime()
    engine = _engine(log, cs, _composer(cs, _ENTRIES))
    task = engine.create_task(goal="g", policy_name="scripted")

    task = record_memory_index(log, cs, task, entries=_ENTRIES)

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
    assert task.state.active_content[MEMORY_KIND] == (MEMORY_INDEX_NAME,)


def test_record_memory_index_is_first_only_and_skips_empty() -> None:
    log, cs, _disp = _runtime()
    engine = _engine(log, cs, _composer(cs, _ENTRIES))
    task = engine.create_task(goal="g", policy_name="scripted")

    task = record_memory_index(log, cs, task, entries=())
    assert MEMORY_KIND not in task.state.active_content  # not configured = zero footprint

    task = record_memory_index(log, cs, task, entries=_ENTRIES)
    task = record_memory_index(log, cs, task, entries=_ENTRIES)
    events = [
        e for e in log.read(task.task_id)
        if e.type == "ContextContentRecorded"
    ]
    assert len(events) == 1


def test_compose_places_index_in_semi_stable_and_stays_pure() -> None:
    log, cs, _disp = _runtime()
    composer = _composer(cs, _ENTRIES)
    engine = _engine(log, cs, composer)
    task = engine.create_task(goal="g", policy_name="scripted")
    task = record_memory_index(log, cs, task, entries=_ENTRIES)

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
    assert [name for name, _ in hits] == ["deploy-process"]
    assert "make deploy" in hits[0][1]
    # The injector may be impure: a memory written midway is immediately recallable.
    store.write("rollback-steps", "Rollback\n\nuse make rollback")
    hits = recall_memories(store, "rollback please")
    assert [name for name, _ in hits] == ["rollback-steps"]


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
    # Tier 2 end-to-end: no name token in the text, but two summary
    # tokens hit — the memory body still comes back verbatim.
    store = MemoryStore(root=tmp_path / "memories")
    store.write(
        "df-42",
        "---\ndescription: postgres connection pooling notes\n---\n"
        "Use pgbouncer.",
    )
    hits = recall_memories(store, "postgres connection keeps dropping")
    assert [name for name, _ in hits] == ["df-42"]
    assert "pgbouncer" in hits[0][1]


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
    by_name = dict(hits)
    # Both memories hit (name tokens "deploy" and "module"/"naming" — at
    # least deploy-process must), and each returned body is byte-identical
    # to the file on disk (no transformation).
    assert "deploy-process" in by_name
    for name, body in hits:
        on_disk = store.read(name)
        assert on_disk is not None
        assert body == on_disk  # verbatim, not paraphrased

    # The rendered recall turn carries each body verbatim (the high-signal
    # exact path survives — a synthesis step would be tempted to drop it).
    rendered = format_recall_text(hits)
    for _name, body in hits:
        assert body in rendered
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
    task = record_memory_index(log, cs, task, entries=entries_at_record)
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
        (("deploy-process", "Always run make deploy."),)
    )
    assert "deploy-process" in text
    assert "Always run make deploy." in text
