"""Memory recall evidence, exclusions, neighbours, names and the write cap.

The acceptance criteria of the two 2026-09 memory specs, in one place:

* **Evidence** — a body rides only when the text NAMES the page (two
  non-common name tokens, or the whole of a shorter name); one shared word is
  a pointer; a token many names share is no evidence at all; the cap keeps the
  strongest hits, not the start of the alphabet.
* **Exclusions** — ``recall_exclude`` names are silent in every tier, and do
  not move what counts as common.
* **Judge** — consulted only when nothing was named and the cap has room.
* **Neighbours** — a named page brings its ``related`` pages as pointers.
* **Names** — a page named in Chinese is a page like any other.
* **Cap** — ``memory_max_bytes`` refuses an oversized body before the write.
"""

from __future__ import annotations

from pathlib import Path

from noeta.builtins.memory.impl.matching import (
    common_name_tokens,
    match_memories_tiered,
    rank_memories,
)
from noeta.builtins.memory.impl.recall import (
    memory_reminder_provider,
    recall_memories,
)
from noeta.builtins.memory.impl.store import (
    MemoryStore,
    MemoryWriteTool,
    build_memory_tools,
)
from noeta.execution.reminders import RecallView, Reminder, ResidentActivation
from noeta.protocols.messages import TextBlock
from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore


def _view(text: str) -> RecallView:
    return RecallView(
        task_id="t-1",
        message=(TextBlock(text=text),),
        task_state=None,
        workspace_path=None,
    )


def _ctx() -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore())


#: 30 names, 20 of them under one project prefix — the shape of a real store.
_ACME_TOPICS = (
    "approval-schema", "billing-export", "cache-eviction", "deadlock-shell",
    "event-replay", "feature-flags", "gateway-timeout", "handshake-retry",
    "ingest-backfill", "journal-compaction", "kafka-lag", "ledger-drift",
    "metrics-cardinality", "namespace-quota", "oncall-rotation",
    "partition-skew", "queue-priority", "rollout-canary", "sharding-plan",
    "tenant-isolation",
)
_OTHER_NAMES = (
    "deploy", "editor-setup", "hiring-loop", "invoice-template",
    "laptop-refresh", "offsite-agenda", "reading-list", "travel-policy",
    "vendor-contacts", "weekly-review",
)
_ACME_ENTRIES = tuple(
    (name, "", "", "")
    for name in sorted(
        tuple(f"acme-{topic}" for topic in _ACME_TOPICS) + _OTHER_NAMES
    )
)


# ---------------------------------------------------------------------------
# Tier-1 evidence
# ---------------------------------------------------------------------------


def test_a_prefix_many_names_share_is_no_evidence() -> None:
    assert common_name_tokens(_ACME_ENTRIES) == frozenset({"acme"})
    assert match_memories_tiered(_ACME_ENTRIES, "what is acme anyway") == ()


def test_one_shared_name_word_is_a_pointer_two_are_a_body() -> None:
    assert match_memories_tiered(_ACME_ENTRIES, "acme and its schema") == (
        ("acme-approval-schema", False),
    )
    assert match_memories_tiered(
        _ACME_ENTRIES, "the approval schema was rejected"
    ) == (("acme-approval-schema", True),)


def test_a_one_word_name_hits_on_its_word_unless_the_word_is_common() -> None:
    assert match_memories_tiered(_ACME_ENTRIES, "time to deploy") == (
        ("deploy", True),
    )
    crowded = tuple((f"deploy-{region}", "", "", "") for region in (
        "tokyo", "paris", "lagos", "quito",
    )) + (("deploy", "", "", ""),)
    assert "deploy" in common_name_tokens(crowded)
    assert match_memories_tiered(crowded, "time to deploy") == ()
    # The distinguishing half of a crowded name is still the whole of its
    # evidence, so it still names the page.
    assert match_memories_tiered(crowded, "deploy to tokyo") == (
        ("deploy-tokyo", True),
    )


def test_the_cap_keeps_the_strongest_hits_not_the_first() -> None:
    entries = (
        ("aaa-bbb-ccc-ddd", "", "", ""),  # 1 matched → pointer
        ("eee-fff-ggg", "", "", ""),  # 2 matched
        ("hhh-iii-jjj", "", "", ""),  # 2 matched
        ("kkk-lll-mmm-nnn", "", "", ""),  # 3 matched
        ("ooo-ppp-qqq", "", "", ""),  # 1 matched → pointer
    )
    text = "aaa eee fff hhh iii kkk lll mmm ooo"
    assert [(r.name, r.by_name, r.score) for r in rank_memories(entries, text)] == [
        ("kkk-lll-mmm-nnn", True, 3),
        ("eee-fff-ggg", True, 2),
        ("hhh-iii-jjj", True, 2),
        ("aaa-bbb-ccc-ddd", False, 1),
        ("ooo-ppp-qqq", False, 1),
    ]
    assert match_memories_tiered(entries, text, max_hits=3) == (
        ("kkk-lll-mmm-nnn", True),
        ("eee-fff-ggg", True),
        ("hhh-iii-jjj", True),
    )


def test_a_keyword_phrase_leads_the_pointer_tier() -> None:
    entries = (
        ("aa-notes", "postgres connection pooling notes", "", ""),
        ("zz-runbook", "unrelated", "", "连接池"),
    )
    text = "postgres connection 连接池 tuning"
    assert match_memories_tiered(entries, text) == (
        ("zz-runbook", False),
        ("aa-notes", False),
    )


def test_exclusion_never_moves_what_is_common() -> None:
    text = "acme approval schema and the gateway"
    everyone = dict(match_memories_tiered(_ACME_ENTRIES, text, max_hits=99))
    # Excluding 17 of the 20 ``acme-*`` pages would leave ``acme`` on three
    # names — not common — if commonness were counted after the exclusion.
    excluded = frozenset(
        f"acme-{topic}" for topic in _ACME_TOPICS[3:]
    )
    remaining = dict(
        match_memories_tiered(_ACME_ENTRIES, text, max_hits=99, exclude=excluded)
    )
    assert remaining == {
        name: tier for name, tier in everyone.items() if name not in excluded
    }
    assert remaining == {"acme-approval-schema": True}


# ---------------------------------------------------------------------------
# Exclusions, neighbours and the judge — through the store
# ---------------------------------------------------------------------------


def _linked_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(root=tmp_path / "memories")
    store.write(
        "rollout-canary",
        "---\ndescription: how a canary rollout runs\n"
        "related: [rollback-steps, [[pager-rules]], no-such-page]\n---\n"
        "Ship to one cell first.",
    )
    store.write(
        "rollback-steps",
        "---\ndescription: undoing a release\nrelated: freeze-calendar\n---\nRevert.",
    )
    store.write("pager-rules", "---\ndescription: who gets paged\n---\nPrimary.")
    store.write("freeze-calendar", "---\ndescription: change freezes\n---\nDecember.")
    return store


def test_a_named_page_brings_its_related_pages_as_pointers(tmp_path: Path) -> None:
    store = _linked_store(tmp_path)
    assert store.related("rollout-canary") == (
        "rollback-steps", "pager-rules", "no-such-page",
    )
    hits = recall_memories(store, "walk me through the rollout canary")
    # One hop: ``freeze-calendar`` hangs off ``rollback-steps`` and stays home;
    # the dangling name is dropped without a word.
    assert [(h.name, h.full) for h in hits] == [
        ("rollout-canary", True),
        ("rollback-steps", False),
        ("pager-rules", False),
    ]
    assert hits[1].text == "undoing a release"


def test_a_pointer_brings_no_neighbours(tmp_path: Path) -> None:
    store = _linked_store(tmp_path)
    hits = recall_memories(store, "is the canary healthy")
    assert [(h.name, h.full) for h in hits] == [("rollout-canary", False)]


def test_neighbours_skip_residents_and_excluded_names(tmp_path: Path) -> None:
    store = _linked_store(tmp_path)
    hits = recall_memories(
        store,
        "walk me through the rollout canary",
        resident=("rollback-steps",),
        exclude=("pager-rules",),
    )
    assert [h.name for h in hits] == ["rollout-canary"]


def test_neighbours_share_the_hit_cap(tmp_path: Path) -> None:
    store = _linked_store(tmp_path)
    names = [f"ledger{i}-sheet{i}" for i in range(4)]
    for name in names:
        store.write(name, "x")
    text = "the rollout canary, then " + " ".join(names)
    hits = recall_memories(store, text)
    assert len(hits) == 5 and all(h.full for h in hits)
    assert {h.name for h in hits} == {"rollout-canary", *names}


def test_an_excluded_name_is_silent_in_every_tier(tmp_path: Path) -> None:
    store = MemoryStore(root=tmp_path / "memories")
    store.write("owner", "---\ndescription: who the owner is\n---\nLeo.")
    store.write("deploy-process", "---\ndescription: how we deploy\n---\nMake.")
    seen: list[tuple[str, ...]] = []

    def judge(entries, text):  # noqa: ANN001 - RecallJudge shape
        seen.append(tuple(name for name, *_rest in entries))
        return ("owner", "deploy-process")

    open_provider = memory_reminder_provider(store)
    (body,) = open_provider(_view("ask the owner"))
    assert isinstance(body, ResidentActivation) and body.name == "owner"

    provider = memory_reminder_provider(store, judge=judge, exclude={"owner"})
    # Named outright, summary overlap, and a judge that picks it anyway.
    (reminder,) = provider(_view("ask the owner who the owner is"))
    assert isinstance(reminder, Reminder)
    assert "owner" not in reminder.text.split("\n", 1)[1]
    assert "- deploy-process: how we deploy" in reminder.text
    assert seen == [("deploy-process",)]


def test_the_judge_runs_only_when_nothing_was_named_and_there_is_room(
    tmp_path: Path,
) -> None:
    store = MemoryStore(root=tmp_path / "memories")
    store.write("deploy-process", "---\ndescription: how we ship\n---\nMake.")
    store.write("naming-rules", "---\ndescription: module names\n---\nSnake.")
    calls: list[tuple[str, ...]] = []

    def judge(entries, text):  # noqa: ANN001 - RecallJudge shape
        calls.append(tuple(name for name, *_rest in entries))
        return ("naming-rules",)

    # A pointer, room left: the judge is asked, about the OTHER pages only,
    # and its pick rides after the lexical pointer.
    hits = recall_memories(store, "when do we deploy", judge=judge)
    assert [(h.name, h.full) for h in hits] == [
        ("deploy-process", False),
        ("naming-rules", False),
    ]
    assert calls == [("naming-rules",)]

    # A page was named: the call is never spent.
    hits = recall_memories(store, "the deploy process please", judge=judge)
    assert [(h.name, h.full) for h in hits] == [("deploy-process", True)]
    assert len(calls) == 1

    # Pointers already fill the cap: its picks would be dropped, so no call.
    for i in range(5):
        store.write(f"canary{i}-cell{i}", "x")
    text = " ".join(f"canary{i}" for i in range(5))
    hits = recall_memories(store, text, judge=judge)
    assert len(hits) == 5 and not any(h.full for h in hits)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# The near-duplicate note speaks the same rule
# ---------------------------------------------------------------------------


def test_similar_note_ignores_a_prefix_the_store_shares(tmp_path: Path) -> None:
    store = MemoryStore(root=tmp_path / "memories")
    for name, *_rest in _ACME_ENTRIES:
        store.write(name, "x")
    tool = MemoryWriteTool(store=store)

    fresh = tool.invoke({"name": "acme-zookeeper-notes", "text": "y"}, _ctx())
    assert fresh.success and "similar" not in fresh.output

    near = tool.invoke({"name": "acme-gateway-retries", "text": "y"}, _ctx())
    assert near.output["similar"] == ["acme-gateway-timeout"]


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def test_a_page_named_in_chinese_is_a_page_like_any_other(tmp_path: Path) -> None:
    store = MemoryStore(root=tmp_path / "memories")
    tool = MemoryWriteTool(store=store)
    written = tool.invoke(
        {"name": "工具技能同步做法", "text": "先同步工具，再同步技能。"}, _ctx()
    )
    assert written.success
    assert (store.root / "工具技能同步做法.md").is_file()
    assert [name for name, *_rest in store.entries()] == ["工具技能同步做法"]
    assert store.read("工具技能同步做法") is not None
    assert [name for name, _lines in store.search("再同步")] == ["工具技能同步做法"]

    (hit,) = recall_memories(store, "技能同步怎么做来着")
    assert (hit.name, hit.full) == ("工具技能同步做法", True)
    # One shared two-character word is a lead, not the page's name.
    (lead,) = recall_memories(store, "这个工具怎么用")
    assert (lead.name, lead.full) == ("工具技能同步做法", False)


def test_names_that_could_leave_the_directory_are_still_refused(
    tmp_path: Path,
) -> None:
    tool = MemoryWriteTool(store=MemoryStore(root=tmp_path / "memories"))
    for bad in ("a/b", "../up", ".hidden", "_lead", "two words", "tail\n", "", "页" * 81):
        result = tool.invoke({"name": bad, "text": "x"}, _ctx())
        assert not result.success and "invalid memory name" in result.summary
    assert not (tmp_path / "memories").exists()
    # 80 CJK characters are 240 bytes — the longest name that still fits a
    # file name once the suffix and the temp decoration are added.
    assert tool.invoke({"name": "页" * 80, "text": "x"}, _ctx()).success


# ---------------------------------------------------------------------------
# The write cap
# ---------------------------------------------------------------------------


def test_memory_write_refuses_a_body_over_the_cap(tmp_path: Path) -> None:
    store = MemoryStore(root=tmp_path / "memories")
    capped = build_memory_tools(store, max_bytes=3000)["memory_write"]

    refused = capped.invoke({"name": "big-page", "text": "x" * 3001}, _ctx())
    assert not refused.success
    assert "3001" in refused.summary and "3000" in refused.summary
    assert store.read("big-page") is None

    # The fence is not the model's to shrink, so it is not what is measured.
    fenced = "---\ndescription: a page at the cap\n---\n" + "x" * 3000
    assert capped.invoke({"name": "big-page", "text": fenced}, _ctx()).success

    uncapped = build_memory_tools(store)["memory_write"]
    assert uncapped.invoke({"name": "huge-page", "text": "x" * 9000}, _ctx()).success


# ---------------------------------------------------------------------------
# Host wiring — HostConfig → SdkHost → recall provider / write tool
# ---------------------------------------------------------------------------


def test_host_threads_recall_exclude_and_the_write_cap(tmp_path: Path) -> None:
    from noeta.testing.fake_llm import FakeLLMProvider
    from tests._sdk_session import make_host, make_registry, runner_main_spec

    mem = tmp_path / "memories"
    MemoryStore(root=mem).write("owner", "---\ndescription: the owner\n---\nLeo.")
    host = make_host(
        make_registry(runner_main_spec("main", memory=True)),
        workspace_dir=tmp_path,
        provider=FakeLLMProvider(responses=[]),
        model="stub-model",
        global_memory_dir=mem,
        recall_exclude=frozenset({"owner"}),
        memory_max_bytes=3000,
    )
    provider = host.intake_reminder_providers("main")[0]
    assert provider(_view("ask the owner")) == ()
    spec = host._lookup_agent("main", task_id="<unbound>")
    config = host._plugin_config(shell_mode="deny", spec=spec)
    assert config["memory"]["max_bytes"] == 3000


def test_the_memory_pack_hands_the_cap_to_its_write_tool(tmp_path: Path) -> None:
    from noeta.builtins.memory.impl import build_memory_session_pack
    from noeta.execution.session_pack import SessionBuildContext
    from noeta.runtime.workspace import WorkspaceRoot

    ctx = SessionBuildContext(
        workspace=WorkspaceRoot.from_path(tmp_path),
        workspace_dir=tmp_path,
        content_store=InMemoryContentStore(),
        exec_env=None,
        model="test-model",
        provider_family=None,
        allowed_tools=frozenset(),
        backends={},
        capability_flags={"memory": True},
        plugin_config={
            "memory": {"memory_dir": tmp_path / "memories", "max_bytes": 10}
        },
    )
    write = build_memory_session_pack(ctx).tools["memory_write"]
    assert not write.invoke({"name": "note", "text": "x" * 11}, _ctx()).success
    assert write.invoke({"name": "note", "text": "x" * 10}, _ctx()).success
