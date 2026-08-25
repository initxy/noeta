"""D5 — memory v1 write/read tools: plain tools, zero special mechanism.

* ``MemoryStore`` — file-per-memory store under one directory: slug-named
  ``<name>.md`` files, traversal-proof name validation, deterministic
  (sorted) index entries with a frontmatter-description-or-first-line
  summary, grep-style ``search()``, reversible ``archive()``.
* ``memory_write`` / ``memory_read`` / ``memory_search`` /
  ``memory_archive`` — plain SDK tools over the store, same dataclass
  shape as the fs tool pack. Results travel the ordinary tool-result
  channel (``ToolCallStarted → ToolResultRecorded →
  ToolCallFinished`` + batched ``MessagesAppended``) — no new event
  types, no runtime changes.
"""

from __future__ import annotations

from pathlib import Path

from noeta.core.engine import Engine
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
    ToolCall,
    ToolCallsDecision,
)
from noeta.protocols.tool import ToolContext
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.tools.limits import INLINE_CONTENT_MAX_BYTES
from noeta.builtins.memory.impl.store import (
    MEMORY_ARCHIVE_TOOL_NAME,
    MEMORY_READ_TOOL_NAME,
    MEMORY_SEARCH_TOOL_NAME,
    MEMORY_WRITE_TOOL_NAME,
    MemoryArchiveTool,
    MemoryReadTool,
    MemorySearchTool,
    MemoryStore,
    MemoryWriteTool,
    build_memory_tools,
)


# ---------------------------------------------------------------------------
# MemoryStore — file-per-memory, slug names, sorted entries
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(root=tmp_path / "memories")


def test_store_write_then_read_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.write("deploy-process", "# Deploy\n\nuse make deploy")
    assert path == store.root / "deploy-process.md"
    assert path.read_text(encoding="utf-8") == "# Deploy\n\nuse make deploy"
    assert store.read("deploy-process") == "# Deploy\n\nuse make deploy"


def test_store_write_overwrites_existing_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("note", "v1")
    store.write("note", "v2")
    assert store.read("note") == "v2"
    assert len(list(store.root.iterdir())) == 1


def test_store_write_never_truncates_the_live_file(
    tmp_path: Path, monkeypatch
) -> None:
    """The replace is atomic: the live memory is whole until the rename lands.

    Truncate-then-write left a window where a concurrent reader (``entries()``
    on every session build, recall on every turn intake, the consolidation
    curator) saw half a memory — and half a memory reads as a whole one. Here
    the rename is forced to fail: the previous version must survive intact and
    the temp file must not linger as store litter.
    """
    import os as _os

    store = _store(tmp_path)
    store.write("note", "the durable version")

    def _boom(src, dst):  # noqa: ANN001 - stub signature mirrors os.replace
        raise OSError("no rename today")

    monkeypatch.setattr(_os, "replace", _boom)
    try:
        store.write("note", "the doomed version")
    except OSError:
        pass
    else:  # pragma: no cover - the stub always raises
        raise AssertionError("the failed replace should have propagated")
    monkeypatch.undo()
    assert store.read("note") == "the durable version"
    assert [p.name for p in store.root.iterdir()] == ["note.md"]


def test_store_read_unknown_name_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("known", "x")
    assert store.read("unknown") is None


def test_store_rejects_traversal_and_malformed_names(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for bad in ("../evil", "a/b", "/abs", "", ".hidden", "sp ace", "a\\b"):
        try:
            store.write(bad, "x")
        except ValueError:
            continue
        raise AssertionError(f"name {bad!r} should have been rejected")
    # nothing escaped onto disk
    assert not (tmp_path / "evil.md").exists()


def test_store_entries_sorted_with_first_line_summary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("zeta", "# Zeta heading\nbody")
    store.write("alpha", "\n\nplain first line\nmore")
    assert store.entries() == (
        ("alpha", "plain first line", "", ""),
        ("zeta", "Zeta heading", "", ""),
    )


def test_store_entries_missing_root_is_empty(tmp_path: Path) -> None:
    store = MemoryStore(root=tmp_path / "nope" / "memories")
    assert store.entries() == ()


def test_store_entries_ignore_foreign_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("real", "memory body")
    store.root.joinpath("not-a-memory.txt").write_text("x", encoding="utf-8")
    assert [name for name, _, _, _ in store.entries()] == ["real"]


# ---------------------------------------------------------------------------
# Frontmatter — optional fence, description/type recognized, malformed = body
# ---------------------------------------------------------------------------


def test_entries_frontmatter_description_and_type_win(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(
        "deploy",
        "---\ndescription: How we deploy safely\ntype: procedural\n---\n"
        "# Deploy\nsteps...",
    )
    assert store.entries() == (
        ("deploy", "How we deploy safely", "procedural", ""),
    )


def test_entries_frontmatter_fallback_skips_fence_block(tmp_path: Path) -> None:
    # No description: the summary falls back to the first non-empty BODY
    # line — never a line inside the fence.
    store = _store(tmp_path)
    store.write("note", "---\ntype: reference\n---\n\n# Real heading\nbody")
    assert store.entries() == (("note", "Real heading", "reference", ""),)


def test_entries_frontmatter_invalid_type_treated_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(
        "note", "---\ndescription: A note\ntype: banana\n---\nbody"
    )
    assert store.entries() == (("note", "A note", "", ""),)


def test_entries_frontmatter_unknown_keys_ignored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(
        "note",
        "---\nauthor: someone\ndescription: Known key wins\n---\nbody",
    )
    assert store.entries() == (("note", "Known key wins", "", ""),)


def test_entries_malformed_fence_is_plain_body(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Unclosed fence: the whole file is body, so the opening fence line
    # itself is the first non-empty line (v1 fallback semantics).
    store.write("unclosed", "---\ndescription: never closed\nbody")
    # Non-``key: value`` line inside the fence: same degradation.
    store.write("badline", "---\nnot a key value line\n---\nbody")
    assert store.entries() == (
        ("badline", "---", "", ""),
        ("unclosed", "---", "", ""),
    )


# ---------------------------------------------------------------------------
# memory_write / memory_read — plain tools, direct invoke
# ---------------------------------------------------------------------------


def _ctx() -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore())


def _pin_today(monkeypatch, value: str = "2026-08-05") -> None:
    """Pin the write tool's date stamp — the tests own the calendar."""
    from noeta.builtins.memory.impl import store as store_mod

    monkeypatch.setattr(store_mod, "_today", lambda: value)


def test_write_tool_persists_memory_file_with_date_stamps(
    tmp_path: Path, monkeypatch
) -> None:
    # The tool always stamps ``created`` / ``updated`` — staleness
    # judgement needs dates no model remembers to ask for. The body
    # itself lands byte-exact after the fence.
    _pin_today(monkeypatch)
    store = _store(tmp_path)
    tool = MemoryWriteTool(store=store)
    assert tool.name == MEMORY_WRITE_TOOL_NAME
    result = tool.invoke(
        {"name": "deploy", "text": "use make deploy"}, _ctx()
    )
    assert result.success
    assert (store.root / "deploy.md").read_text(encoding="utf-8") == (
        "---\ncreated: 2026-08-05\nupdated: 2026-08-05\n---\n"
        "use make deploy"
    )
    # The fence never leaks into the index summary: first BODY line wins.
    assert store.entries() == (("deploy", "use make deploy", "", ""),)


def test_write_tool_created_sticks_updated_moves(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    tool = MemoryWriteTool(store=store)
    _pin_today(monkeypatch, "2026-08-01")
    assert tool.invoke({"name": "note", "text": "v1"}, _ctx()).success
    _pin_today(monkeypatch, "2026-08-05")
    assert tool.invoke({"name": "note", "text": "v2"}, _ctx()).success
    text = store.read("note")
    assert text is not None
    assert "created: 2026-08-01" in text  # birth date survives the rewrite
    assert "updated: 2026-08-05" in text
    assert text.endswith("---\nv2")


def test_write_tool_rejects_missing_or_invalid_arguments(
    tmp_path: Path,
) -> None:
    tool = MemoryWriteTool(store=_store(tmp_path))
    assert not tool.invoke({"text": "no name"}, _ctx()).success
    assert not tool.invoke({"name": "n"}, _ctx()).success
    assert not tool.invoke({"name": "../up", "text": "x"}, _ctx()).success
    assert not tool.invoke({"name": "n", "text": 7}, _ctx()).success


def test_write_tool_params_compose_frontmatter(
    tmp_path: Path, monkeypatch
) -> None:
    _pin_today(monkeypatch)
    store = _store(tmp_path)
    tool = MemoryWriteTool(store=store)
    result = tool.invoke(
        {
            "name": "deploy",
            "text": "# Deploy\nsteps",
            "description": "How we deploy",
            "type": "procedural",
            "keywords": "deploy, release, 部署, 发版",
        },
        _ctx(),
    )
    assert result.success
    assert store.read("deploy") == (
        "---\ndescription: How we deploy\ntype: procedural\n"
        "keywords: deploy, release, 部署, 发版\n"
        "created: 2026-08-05\nupdated: 2026-08-05\n---\n"
        "# Deploy\nsteps"
    )
    assert store.entries() == (
        ("deploy", "How we deploy", "procedural", "deploy, release, 部署, 发版"),
    )


def test_write_tool_merges_params_over_text_fence_per_field(
    tmp_path: Path, monkeypatch
) -> None:
    # Per-field merge: a param overrides the field it names; every other
    # field the text's fence carries survives — including keys the tool
    # does not recognize. The old replace-the-whole-fence rule silently
    # destroyed curator-maintained fields on every parametered rewrite.
    _pin_today(monkeypatch)
    store = _store(tmp_path)
    tool = MemoryWriteTool(store=store)
    result = tool.invoke(
        {
            "name": "note",
            "text": (
                "---\ndescription: stale\ntype: user\n"
                "keywords: 记忆, recall\nowner: xiyang\n---\nbody line"
            ),
            "description": "fresh",
        },
        _ctx(),
    )
    assert result.success
    assert store.read("note") == (
        "---\ndescription: fresh\ntype: user\nkeywords: 记忆, recall\n"
        "created: 2026-08-05\nupdated: 2026-08-05\nowner: xiyang\n---\n"
        "body line"
    )
    assert store.entries() == (("note", "fresh", "user", "记忆, recall"),)


def test_write_tool_no_params_keeps_text_fence_fields(
    tmp_path: Path, monkeypatch
) -> None:
    _pin_today(monkeypatch)
    store = _store(tmp_path)
    text = "---\ndescription: self-made\n---\nbody"
    assert MemoryWriteTool(store=store).invoke(
        {"name": "note", "text": text}, _ctx()
    ).success
    assert store.read("note") == (
        "---\ndescription: self-made\n"
        "created: 2026-08-05\nupdated: 2026-08-05\n---\nbody"
    )
    assert store.entries() == (("note", "self-made", "", ""),)


def test_write_tool_body_only_rewrite_keeps_disk_fence_fields(
    tmp_path: Path, monkeypatch
) -> None:
    # The common rewrite: the tool description tells the model NOT to
    # write a fence, and a curator's keywords live only on disk. A write
    # naming neither params nor a fence must keep every field the memory
    # already carries — the tool's own AND ones it does not recognize.
    store = _store(tmp_path)
    tool = MemoryWriteTool(store=store)
    _pin_today(monkeypatch, "2026-08-01")
    assert tool.invoke(
        {
            "name": "note",
            "text": "---\nstatus: active\ndue: 2026-09-01\n---\nv1",
            "description": "Curated summary",
            "type": "project",
            "keywords": "记忆, recall",
        },
        _ctx(),
    ).success
    _pin_today(monkeypatch, "2026-08-05")
    assert tool.invoke({"name": "note", "text": "v2"}, _ctx()).success
    assert store.read("note") == (
        "---\ndescription: Curated summary\ntype: project\n"
        "keywords: 记忆, recall\ncreated: 2026-08-01\nupdated: 2026-08-05\n"
        "due: 2026-09-01\nstatus: active\n---\nv2"
    )
    assert store.entries() == (
        ("note", "Curated summary", "project", "记忆, recall"),
    )


def test_write_tool_merge_precedence_disk_then_text_then_params(
    tmp_path: Path, monkeypatch
) -> None:
    # Lowest to highest: disk fence < text fence < params, each layer
    # touching only the fields it names.
    _pin_today(monkeypatch)
    store = _store(tmp_path)
    tool = MemoryWriteTool(store=store)
    assert tool.invoke(
        {
            "name": "note",
            "text": "---\nowner: disk\nstatus: active\n---\nv1",
            "description": "from disk",
            "type": "user",
            "keywords": "disk-kw",
        },
        _ctx(),
    ).success
    assert tool.invoke(
        {
            "name": "note",
            "text": (
                "---\ndescription: from text\ntype: project\n"
                "owner: text\n---\nv2"
            ),
            "description": "from params",
        },
        _ctx(),
    ).success
    assert store.read("note") == (
        "---\ndescription: from params\ntype: project\nkeywords: disk-kw\n"
        "created: 2026-08-05\nupdated: 2026-08-05\n"
        "owner: text\nstatus: active\n---\nv2"
    )


def test_write_tool_empty_value_in_text_fence_drops_disk_field(
    tmp_path: Path, monkeypatch
) -> None:
    # The one way to remove a field: name it with an empty value. Other
    # disk fields are untouched.
    _pin_today(monkeypatch)
    store = _store(tmp_path)
    tool = MemoryWriteTool(store=store)
    assert tool.invoke(
        {
            "name": "note",
            "text": "---\nstatus: active\ndue: 2026-09-01\n---\nv1",
            "keywords": "kw",
        },
        _ctx(),
    ).success
    assert tool.invoke(
        {"name": "note", "text": "---\ndue:\n---\nv2"}, _ctx()
    ).success
    assert store.read("note") == (
        "---\nkeywords: kw\ncreated: 2026-08-05\nupdated: 2026-08-05\n"
        "status: active\n---\nv2"
    )


def test_write_tool_stamps_source_task_from_runtime_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    # The ledger receipt: when the ToolRuntime threads a task_id through
    # the metadata bag, the memory records which task's history backs it.
    _pin_today(monkeypatch)
    store = _store(tmp_path)
    ctx = ToolContext(
        artifact_store=InMemoryContentStore(),
        metadata={"task_id": "task-123", "trace_id": "t"},
    )
    assert MemoryWriteTool(store=store).invoke(
        {"name": "note", "text": "body"}, ctx
    ).success
    text = store.read("note")
    assert text is not None
    assert "source_task: task-123" in text
    # A hand-built context without task identity stamps nothing.
    assert MemoryWriteTool(store=store).invoke(
        {"name": "bare", "text": "body"}, _ctx()
    ).success
    assert "source_task" not in (store.read("bare") or "?")


def test_write_tool_new_name_reports_similar_existing(
    tmp_path: Path,
) -> None:
    # The near-duplicate probe fires at write time — while the context
    # that caused the write is still live — and is advisory: the write
    # itself always lands.
    store = _store(tmp_path)
    tool = MemoryWriteTool(store=store)
    assert tool.invoke(
        {"name": "deploy-process", "text": "How we deploy"}, _ctx()
    ).success
    result = tool.invoke(
        {"name": "deploy-pipeline", "text": "Pipeline notes"}, _ctx()
    )
    assert result.success
    assert result.output["similar"] == ["deploy-process"]
    assert "consider updating instead" in result.summary
    assert store.read("deploy-pipeline") is not None  # advisory, not a veto


def test_write_tool_update_never_reports_itself_similar(
    tmp_path: Path,
) -> None:
    # Rewriting an existing memory is the cure, not the disease: an
    # update carries no similarity note, and an unrelated new memory
    # stays quiet too.
    store = _store(tmp_path)
    tool = MemoryWriteTool(store=store)
    assert tool.invoke(
        {"name": "deploy-process", "text": "How we deploy"}, _ctx()
    ).success
    updated = tool.invoke(
        {"name": "deploy-process", "text": "How we deploy, v2"}, _ctx()
    )
    assert updated.success
    assert "similar" not in updated.output
    fresh = tool.invoke(
        {"name": "postgres-pooling", "text": "Use pgbouncer."}, _ctx()
    )
    assert fresh.success
    assert "similar" not in fresh.output


def test_write_tool_rejects_invalid_type_description_keywords(
    tmp_path: Path,
) -> None:
    tool = MemoryWriteTool(store=_store(tmp_path))
    args = {"name": "n", "text": "body"}
    assert not tool.invoke({**args, "type": "banana"}, _ctx()).success
    assert not tool.invoke({**args, "type": 7}, _ctx()).success
    assert not tool.invoke({**args, "description": 7}, _ctx()).success
    assert not tool.invoke(
        {**args, "description": "two\nlines"}, _ctx()
    ).success
    assert not tool.invoke({**args, "keywords": 7}, _ctx()).success
    assert not tool.invoke(
        {**args, "keywords": "two\nlines"}, _ctx()
    ).success


def test_read_tool_returns_full_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("deploy", "line one\nline two")
    tool = MemoryReadTool(store=store)
    assert tool.name == MEMORY_READ_TOOL_NAME
    result = tool.invoke({"name": "deploy"}, _ctx())
    assert result.success
    assert result.output["name"] == "deploy"
    assert result.output["text"] == "line one\nline two"


def test_read_tool_unknown_memory_fails_gracefully(tmp_path: Path) -> None:
    tool = MemoryReadTool(store=_store(tmp_path))
    result = tool.invoke({"name": "ghost"}, _ctx())
    assert not result.success
    assert "ghost" in result.summary


def test_read_tool_bounds_oversized_inline_output(tmp_path: Path) -> None:
    store = _store(tmp_path)
    big = "X" * (INLINE_CONTENT_MAX_BYTES + 10_000)
    store.write("big", big)
    result = MemoryReadTool(store=store).invoke({"name": "big"}, _ctx())
    assert result.success
    assert result.output["truncated"] is True
    assert len(result.output["text"].encode("utf-8")) <= INLINE_CONTENT_MAX_BYTES


# ---------------------------------------------------------------------------
# MemoryStore.search / memory_search — grep semantics, bounded, archive-blind
# ---------------------------------------------------------------------------


def test_store_search_matches_name_and_text_case_insensitive(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.write("deploy-process", "# Deploy\nAlways run SMOKE tests.")
    store.write("naming-rules", "# Naming\nsnake_case everywhere.")
    # Text hit, case-insensitive, excerpt line has trailing space stripped.
    assert store.search("smoke") == (
        ("deploy-process", ("Always run SMOKE tests.",)),
    )
    # Name-only hit: still a hit, with an empty excerpt.
    assert store.search("naming-RULES") == (("naming-rules", ()),)
    assert store.search("nowhere-to-be-found") == ()


def test_store_search_caps_lines_and_chars_but_not_memories(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    long_line = "needle " + "x" * 300
    store.write("aa-many-lines", "\n".join([long_line] * 5))
    for i in range(12):
        store.write(f"hit-{i:02d}", "needle here")
    results = store.search("needle")
    # The store returns EVERY match (the memory-count cap is the tool's,
    # so it can report the trim); excerpts are capped per memory.
    assert len(results) == 13
    by_name = dict(results)
    assert len(by_name["aa-many-lines"]) == 3  # excerpt line cap
    assert all(len(line) <= 200 for line in by_name["aa-many-lines"])


def test_search_tool_caps_memories_and_reports_truncation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for i in range(12):
        store.write(f"hit-{i:02d}", "needle here")
    result = MemorySearchTool(store=store).invoke({"query": "needle"}, _ctx())
    assert result.success
    assert len(result.output["results"]) == 10  # memory cap, name-sorted
    assert result.output["truncated"] is True
    assert "12 hit(s), first 10 shown" in result.summary


def test_store_search_never_sees_archived_memories(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("old-way", "the obsolete needle")
    store.write("new-way", "the current needle")
    store.archive("old-way")
    assert [name for name, _ in store.search("needle")] == ["new-way"]


def test_search_tool_returns_grep_shaped_output(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("deploy", "run make deploy\nthen smoke test")
    tool = MemorySearchTool(store=store)
    assert tool.name == MEMORY_SEARCH_TOOL_NAME
    result = tool.invoke({"query": "smoke"}, _ctx())
    assert result.success
    assert result.output == {
        "query": "smoke",
        "results": [{"name": "deploy", "lines": ["then smoke test"]}],
        "truncated": False,
    }
    assert "1 hit(s)" in result.summary


def test_search_tool_zero_hits_is_success_empty_query_is_error(
    tmp_path: Path,
) -> None:
    tool = MemorySearchTool(store=_store(tmp_path))
    result = tool.invoke({"query": "ghost"}, _ctx())
    assert result.success
    assert result.output == {"query": "ghost", "results": [], "truncated": False}
    assert not tool.invoke({"query": ""}, _ctx()).success
    assert not tool.invoke({"query": 7}, _ctx()).success


# ---------------------------------------------------------------------------
# MemoryStore.archive / memory_archive — retire into archive/, never delete
# ---------------------------------------------------------------------------


def test_store_archive_moves_file_out_of_entries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("obsolete", "old body")
    dest = store.archive("obsolete")
    assert dest == store.root / "archive" / "obsolete.md"
    assert dest.read_text(encoding="utf-8") == "old body"
    assert store.entries() == ()  # gone from the index source
    assert store.read("obsolete") is None


def test_store_archive_collision_gets_numbered_suffix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for expected in ("obsolete.md", "obsolete-2.md", "obsolete-3.md"):
        store.write("obsolete", f"body for {expected}")
        dest = store.archive("obsolete")
        assert dest is not None
        assert dest.name == expected
    assert (store.root / "archive" / "obsolete-3.md").read_text(
        encoding="utf-8"
    ) == "body for obsolete-3.md"


def test_store_archive_missing_or_invalid_name_returns_none(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    assert store.archive("ghost") is None
    assert store.archive("../evil") is None


def test_archive_tool_reports_relative_destination(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("obsolete", "x")
    tool = MemoryArchiveTool(store=store)
    assert tool.name == MEMORY_ARCHIVE_TOOL_NAME
    result = tool.invoke({"name": "obsolete"}, _ctx())
    assert result.success
    assert result.output == {
        "name": "obsolete",
        "archived_to": "archive/obsolete.md",
    }


def test_archive_tool_unknown_or_invalid_name_fails(tmp_path: Path) -> None:
    tool = MemoryArchiveTool(store=_store(tmp_path))
    assert not tool.invoke({"name": "ghost"}, _ctx()).success
    assert not tool.invoke({"name": "../up"}, _ctx()).success


def test_build_memory_tools_names_match_tool_attribute(
    tmp_path: Path,
) -> None:
    tools = build_memory_tools(_store(tmp_path))
    assert set(tools) == {
        MEMORY_WRITE_TOOL_NAME,
        MEMORY_READ_TOOL_NAME,
        MEMORY_SEARCH_TOOL_NAME,
        MEMORY_ARCHIVE_TOOL_NAME,
    }
    for key, tool in tools.items():
        assert tool.name == key


# ---------------------------------------------------------------------------
# Engine end-to-end — write then read through the tool-result channel
# ---------------------------------------------------------------------------


def _build_engine(*, policy: object, tools: dict[str, object]) -> tuple[
    Engine, InMemoryEventLog, InMemoryContentStore, str, object
]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=policy,
        tools=tools,
        tool_runtime=ToolRuntime(
            event_log=event_log, content_store=content_store
        ),
    )
    task = engine.create_task(goal="remember", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-mem")
    assert lease is not None
    return engine, event_log, content_store, lease.lease_id, task


def test_engine_write_then_read_memory_end_to_end(tmp_path: Path) -> None:
    store = _store(tmp_path)
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[
                    ToolCall(
                        tool_name=MEMORY_WRITE_TOOL_NAME,
                        arguments={
                            "name": "deploy",
                            "text": "deploy with make deploy",
                        },
                        call_id="c1",
                    ),
                ],
            ),
            ToolCallsDecision(
                calls=[
                    ToolCall(
                        tool_name=MEMORY_READ_TOOL_NAME,
                        arguments={"name": "deploy"},
                        call_id="c2",
                    ),
                ],
            ),
            FinishDecision(answer="done"),
        ]
    )
    engine, log, cs, lease_id, task = _build_engine(
        policy=policy, tools=build_memory_tools(store)
    )

    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "terminal"
    # write memory = one file on disk: stamped fence + byte-exact body,
    # with the ledger receipt naming the very task that wrote it.
    stored = (store.root / "deploy.md").read_text(encoding="utf-8")
    assert stored.endswith("---\ndeploy with make deploy")
    assert "created: " in stored and "updated: " in stored
    assert f"source_task: {task.task_id}" in stored
    # read full text = ordinary tool-result channel (ToolResultRecorded.output_ref is dereferenceable)
    recorded = [
        e for e in log.read(task.task_id) if e.type == "ToolResultRecorded"
    ]
    assert [e.payload.call_id for e in recorded] == ["c1", "c2"]
    assert all(e.payload.success for e in recorded)
    assert b"deploy with make deploy" in cs.get(recorded[1].payload.output_ref)
