"""Memory v2 phase 3 — the SDK consolidation module (T5 + T6).

``noeta.client.consolidation`` is the host-side half of the background
curation pass (``docs/adr/memory-consolidation.md``): the debounce marker
(``.consolidation-state.json`` in the memory root, written at ENQUEUE time),
the session-activity digest builder (roots only, newest first, capped with the
caps STATED in the digest — no silent truncation), and ``run_consolidation``
(decision #11's explicit host-callable, which seeds the ``__consolidation__``
root task through the background seed path). The preset half
(``noeta.presets.CONSOLIDATION_AGENT``) is covered here too: reserved name,
memory-pack-only tool surface, and zero impact on main's spawnable roster.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from noeta.client.consolidation import (
    CONSOLIDATION_AGENT_NAME,
    CONSOLIDATION_MARKER_FILENAME,
    _session_transcript,
    build_consolidation_digest,
    consolidation_due,
    read_consolidation_marker,
    run_consolidation,
    write_consolidation_marker,
)
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.events import EventEnvelope, MessagesAppendedPayload
from noeta.protocols.messages import LLMResponse, Message, TextBlock, Usage
from noeta.sdk import AgentDefinition, Client, HostConfig, Options
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider


NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _internal_agent() -> AgentDefinition:
    """A minimal stand-in registration for the reserved consolidation name."""
    return AgentDefinition(
        description="internal curation stand-in",
        prompt="curate the store",
        tools=(),
    )


def _client(tmp_path: Path, responses, *, agents=None, clock=None) -> Client:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher, clock=clock)
    options = Options(
        system_prompt="you finish immediately",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
        agents=dict(agents or {}),
    )
    return Client(
        options,
        provider=FakeLLMProvider(responses=list(responses)),
        workspace_dir=tmp_path,
        multi_turn=True,
        host_config=HostConfig(
            event_log=event_log,
            content_store=InMemoryContentStore(),
            dispatcher=dispatcher,
        ),
    )


def _consolidation_tasks(client: Client) -> list[str]:
    """Task ids whose genesis agent is the reserved consolidation name."""
    out = []
    for summary in client.task_streams():
        envs = client.events(summary.task_id)
        if (
            envs
            and envs[0].type == "TaskCreated"
            and envs[0].payload.agent_name == CONSOLIDATION_AGENT_NAME
        ):
            out.append(summary.task_id)
    return out


# ---------------------------------------------------------------------------
# 1. Marker + due logic (pure given ``now``)
# ---------------------------------------------------------------------------


def test_marker_roundtrip_and_due_threshold(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    # Missing marker (missing directory, even) ⇒ due.
    assert read_consolidation_marker(root) is None
    assert consolidation_due(root, now=NOW) is True

    write_consolidation_marker(root, now=NOW)
    assert read_consolidation_marker(root) == NOW
    # Inside the debounce window ⇒ not due; at/after the threshold ⇒ due.
    assert consolidation_due(root, now=NOW + timedelta(hours=23)) is False
    assert consolidation_due(root, now=NOW + timedelta(hours=24)) is True
    # The threshold is configurable.
    assert (
        consolidation_due(root, now=NOW + timedelta(hours=2), debounce_hours=1.0)
        is True
    )
    # The stored shape is the documented one-key JSON (ISO-8601 UTC).
    data = json.loads(
        (root / CONSOLIDATION_MARKER_FILENAME).read_text(encoding="utf-8")
    )
    assert data == {"last_run_at": NOW.isoformat()}


def test_marker_corrupt_file_degrades_to_due(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    root.mkdir()
    marker = root / CONSOLIDATION_MARKER_FILENAME
    for corrupt in ("not json", "{}", '{"last_run_at": "not-a-date"}', "[1,2]"):
        marker.write_text(corrupt, encoding="utf-8")
        assert read_consolidation_marker(root) is None
        assert consolidation_due(root, now=NOW) is True


def test_marker_is_invisible_to_the_memory_store(tmp_path: Path) -> None:
    """The dot-file marker must never surface as a memory (index/recall glob)."""
    from noeta.execution.memory import load_memory_store

    root = tmp_path / "memories"
    write_consolidation_marker(root, now=NOW)
    assert load_memory_store(root=root).entries() == ()


# ---------------------------------------------------------------------------
# 2. Digest builder
# ---------------------------------------------------------------------------


def test_digest_role_labeled_text_with_header(tmp_path: Path) -> None:
    client = _client(tmp_path, [_end("hi from assistant")])
    try:
        client.start(goal="hello from user")
        digest = build_consolidation_digest(client)
    finally:
        client.shutdown()
    assert digest is not None
    assert "# Recent session activity digest" in digest
    assert "all recorded sessions (no previous consolidation run)" in digest
    assert "1 shown" in digest
    assert "16000 characters" in digest
    assert "## Session " in digest
    assert "user: hello from user" in digest
    assert "assistant: hi from assistant" in digest


def test_digest_none_when_no_sessions(tmp_path: Path) -> None:
    client = _client(tmp_path, [])
    try:
        assert build_consolidation_digest(client) is None
    finally:
        client.shutdown()


def test_digest_since_filters_on_envelope_timestamps(tmp_path: Path) -> None:
    state = {"t": 1_000.0}

    def clock() -> float:
        state["t"] += 1.0
        return state["t"]

    client = _client(tmp_path, [_end("done")], clock=clock)
    try:
        client.start(goal="old activity")
        # All of the session's envelopes carry occurred_at ≈ 1_000..1_050.
        before = datetime.fromtimestamp(500.0, tz=timezone.utc)
        after = datetime.fromtimestamp(5_000.0, tz=timezone.utc)
        assert build_consolidation_digest(client, since=before) is not None
        assert build_consolidation_digest(client, since=after) is None
        included = build_consolidation_digest(client, since=before)
        assert included is not None
        assert f"sessions with activity after {before.isoformat()}" in included
    finally:
        client.shutdown()


def test_digest_excludes_reserved_agent_sessions(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        [_end("internal curation output"), _end("normal reply")],
        agents={CONSOLIDATION_AGENT_NAME: _internal_agent()},
    )
    try:
        client.start(goal="internal curation run", agent=CONSOLIDATION_AGENT_NAME)
        # Only the reserved session exists ⇒ nothing to digest.
        assert build_consolidation_digest(client) is None
        client.start(goal="real user goal")
        digest = build_consolidation_digest(client)
    finally:
        client.shutdown()
    assert digest is not None
    assert "real user goal" in digest
    assert "internal curation run" not in digest
    assert "1 shown" in digest  # the reserved session never consumes the cap


def test_digest_session_cap_newest_first_and_dropped_stated(tmp_path: Path) -> None:
    state = {"t": 1_000.0}

    def clock() -> float:
        state["t"] += 1.0
        return state["t"]

    client = _client(
        tmp_path, [_end("r-one"), _end("r-two"), _end("r-three")], clock=clock
    )
    try:
        client.start(goal="goal-one oldest")
        client.start(goal="goal-two middle")
        client.start(goal="goal-three newest")
        digest = build_consolidation_digest(client, max_sessions=2)
    finally:
        client.shutdown()
    assert digest is not None
    assert "goal-three newest" in digest
    assert "goal-two middle" in digest
    assert "goal-one oldest" not in digest  # capped — and the cap is stated:
    assert "1 more active session(s) in the window were omitted" in digest
    assert "(session cap 2)" in digest
    # Newest first.
    assert digest.index("goal-three newest") < digest.index("goal-two middle")


def _appended(store, messages, task_id="t1") -> EventEnvelope:
    ref = store.put(to_canonical_bytes(list(messages)), media_type="application/json")
    return EventEnvelope.build(
        task_id=task_id,
        type="MessagesAppended",
        payload=MessagesAppendedPayload(messages_ref=ref, count=len(messages)),
    )


def test_transcript_skips_system_injected_and_non_text_turns() -> None:
    store = InMemoryContentStore()
    env = _appended(
        store,
        [
            Message(role="user", content=[TextBlock(text="real user text")]),
            Message(
                role="user",
                content=[TextBlock(text="recalled memory body")],
                origin="memory",
            ),
            Message(
                role="user",
                content=[TextBlock(text="host system notice")],
                origin="system",
            ),
            Message(role="system", content=[TextBlock(text="system prompt")]),
            Message(role="assistant", content=[TextBlock(text="assistant text")]),
        ],
    )
    text = _session_transcript([env], store, max_chars=10_000)
    assert text == "user: real user text\nassistant: assistant text"


def test_transcript_tail_truncation_keeps_recent_turns() -> None:
    store = InMemoryContentStore()
    envs = [
        _appended(
            store, [Message(role="user", content=[TextBlock(text=f"turn-{i:04d}")])]
        )
        for i in range(100)
    ]
    text = _session_transcript(envs, store, max_chars=200)
    assert text.startswith("[... earlier turns omitted]\n")
    assert "turn-0099" in text  # the tail (most recent) survives
    assert "turn-0000" not in text  # the head is dropped
    # The cut lands on a line boundary — no half turn right after the marker.
    first_kept = text.splitlines()[1]
    assert first_kept.startswith("user: turn-")


# ---------------------------------------------------------------------------
# 3. run_consolidation — the explicit host-callable
# ---------------------------------------------------------------------------


def test_run_consolidation_enqueues_task_and_writes_marker(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    client = _client(
        tmp_path,
        [_end("done")],
        agents={CONSOLIDATION_AGENT_NAME: _internal_agent()},
    )
    try:
        client.start(goal="please remember the deploy steps")
        assert run_consolidation(client, memory_root=root, now=NOW) is True
        # Marker written at enqueue time (the run has NOT been driven — no
        # workers here — yet the debounce is already armed).
        assert read_consolidation_marker(root) == NOW
        tasks = _consolidation_tasks(client)
        assert len(tasks) == 1
        genesis = client.events(tasks[0])[0]
        assert genesis.payload.parent_task_id is None  # a ROOT task
        goal = genesis.payload.goal
        assert goal.startswith("Memory consolidation run:")
        assert "# Recent session activity digest" in goal
        assert "please remember the deploy steps" in goal
    finally:
        client.shutdown()


def test_run_consolidation_debounce_blocks_second_call(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    client = _client(
        tmp_path,
        [_end("done")],
        agents={CONSOLIDATION_AGENT_NAME: _internal_agent()},
    )
    try:
        client.start(goal="some activity")
        assert run_consolidation(client, memory_root=root, now=NOW) is True
        assert (
            run_consolidation(client, memory_root=root, now=NOW + timedelta(hours=1))
            is False
        )
        assert len(_consolidation_tasks(client)) == 1
        assert read_consolidation_marker(root) == NOW  # marker untouched
    finally:
        client.shutdown()


def test_run_consolidation_no_activity_returns_false_without_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "memories"
    client = _client(tmp_path, [])
    try:
        assert run_consolidation(client, memory_root=root, now=NOW) is False
    finally:
        client.shutdown()
    # An empty run must not arm the debounce (nothing was enqueued).
    assert read_consolidation_marker(root) is None
    assert not (root / CONSOLIDATION_MARKER_FILENAME).exists()


def test_run_consolidation_window_rides_the_marker(tmp_path: Path) -> None:
    """After a run, only NEW activity (past the marker) makes the next run due
    in substance: with no fresh envelopes the digest is empty ⇒ False, even
    with the debounce explicitly disabled."""
    state = {"t": 1_000.0}

    def clock() -> float:
        state["t"] += 1.0
        return state["t"]

    root = tmp_path / "memories"
    client = _client(
        tmp_path,
        [_end("first"), _end("second")],
        agents={CONSOLIDATION_AGENT_NAME: _internal_agent()},
        clock=clock,
    )
    try:
        client.start(goal="first burst")
        marker_time = datetime.fromtimestamp(5_000.0, tz=timezone.utc)
        assert (
            run_consolidation(client, memory_root=root, now=marker_time, debounce=False)
            is True
        )
        # No activity after the marker ⇒ nothing to digest ⇒ False.
        assert (
            run_consolidation(
                client,
                memory_root=root,
                now=marker_time + timedelta(days=2),
                debounce=False,
            )
            is False
        )
        # Fresh activity past the marker ⇒ a new run enqueues again.
        state["t"] = 9_000.0
        client.start(goal="second burst")
        assert (
            run_consolidation(
                client,
                memory_root=root,
                now=marker_time + timedelta(days=2),
                debounce=False,
            )
            is True
        )
        assert len(_consolidation_tasks(client)) == 2
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# 4. The preset half (T6a) — reserved name, memory-pack-only surface
# ---------------------------------------------------------------------------


def test_preset_exports_and_roster_isolation() -> None:
    from noeta.presets import (
        CONSOLIDATION_AGENT,
        OFFICIAL_SUBAGENTS,
        main_options,
        with_consolidation_agent,
    )
    from noeta.sdk import compile_options
    from noeta.presets import CONSOLIDATION_AGENT_NAME as preset_name

    assert preset_name == CONSOLIDATION_AGENT_NAME == "__consolidation__"
    assert CONSOLIDATION_AGENT_NAME not in OFFICIAL_SUBAGENTS
    assert CONSOLIDATION_AGENT.tools == ()
    assert CONSOLIDATION_AGENT.capabilities is not None
    assert CONSOLIDATION_AGENT.capabilities.memory is True

    base_main, base_desc = compile_options(main_options())
    main, desc = compile_options(with_consolidation_agent(main_options()))
    # Registration changes NOTHING about main — the reserved name never joins
    # the spawnable auto-union, so the spawn_subagent directory (and the
    # stable prefix) stays byte-identical.
    assert main == base_main
    assert CONSOLIDATION_AGENT_NAME not in main.capabilities.spawnable
    names = sorted(d.name for d in desc)
    assert CONSOLIDATION_AGENT_NAME in names
    assert names == sorted(
        [d.name for d in base_desc] + [CONSOLIDATION_AGENT_NAME]
    )
    spec = next(d for d in desc if d.name == CONSOLIDATION_AGENT_NAME)
    assert spec.tools == ()  # empty whitelist — see the session-inputs test
    assert spec.capabilities.memory is True
    assert spec.capabilities.delegation is False
    # The prompt carries the memory policy fragment (duty 3 is defined by it).
    from noeta.presets import MEMORY_POLICY_PROMPT

    assert spec.instructions.endswith("\n" + MEMORY_POLICY_PROMPT)


def test_consolidation_tool_surface_is_memory_pack_only(tmp_path: Path) -> None:
    """``tools=()`` + ``memory=True`` ⇒ the built session attaches EXACTLY the
    capability-gated memory pack (``_stage_memory`` is flag-gated, not
    whitelist-filtered) — no fs, no shell, no web."""
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.guards.budget import Budget
    from noeta.tools.memory import (
        MEMORY_ARCHIVE_TOOL_NAME,
        MEMORY_READ_TOOL_NAME,
        MEMORY_SEARCH_TOOL_NAME,
        MEMORY_WRITE_TOOL_NAME,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    inputs = build_session_inputs(
        workspace_dir=ws,
        system_prompt="curate",
        allowed_tools=frozenset(),  # the compiled spec's empty whitelist
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        memory_enabled=True,
        memory_dir=tmp_path / "memories",
    )
    assert set(inputs.tools) == {
        MEMORY_WRITE_TOOL_NAME,
        MEMORY_READ_TOOL_NAME,
        MEMORY_SEARCH_TOOL_NAME,
        MEMORY_ARCHIVE_TOOL_NAME,
    }
