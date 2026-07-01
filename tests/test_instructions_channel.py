"""Project instructions file (the CLAUDE.md counterpart) — third content-channel tenant.

Reuses the generic content-channel mechanism,
adds kind="instructions", policy=evolving, mirroring the memory channel's structure
(noeta/context/instructions.py + noeta/execution/instructions.py).

Coverage:

* Pure-function units — load_instructions file discovery, renderer, hash.
* Channel E2E — session record/activation, semi_stable rendering, View source labels.
* Zero footprint — flag off / no file → byte-for-byte identical to not having the feature.
* verify evolving — changed content is advisory, not a hard failure.
* Product wiring — the product session enables instructions; explicit False turns it off.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from noeta.context.composer import RenderedSkills, ThreeSegmentComposer
from noeta.context.content_channel import ContentChannelRegistry
from noeta.context.environment import ENVIRONMENT_KIND
from noeta.context.instructions import (
    INSTRUCTIONS_DRIFT_POLICY,
    INSTRUCTIONS_KIND,
    INSTRUCTIONS_VERSION,
    InstructionsSnapshot,
    build_instructions_renderer,
    instructions_content_hash,
    instructions_content_kind,
    render_instructions_text,
)
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
from noeta.execution.instructions import (
    DEFAULT_INSTRUCTIONS_FILENAMES,
    load_instructions,
    record_instructions,
)
from noeta.guards.budget import Budget
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import FinishDecision
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    runner_main_spec,
)


_SAMPLE_TEXT = "# Project rules\n\n* Reply in Chinese.\n* Run pytest before committing.\n"
_SAMPLE_SNAPSHOT = InstructionsSnapshot(name="NOETA.md", text=_SAMPLE_TEXT)


# ---------------------------------------------------------------------------
# 1. Pure-function units — load_instructions file discovery
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_load_prefers_noeta_md_over_agents_md(tmp_path: Path) -> None:
    _write(tmp_path / "NOETA.md", "# NOETA\n")
    _write(tmp_path / "AGENTS.md", "# AGENTS\n")
    snap = load_instructions(tmp_path)
    assert snap is not None
    assert snap.name == "NOETA.md"
    assert snap.text.startswith("# NOETA")


def test_load_falls_back_to_agents_md(tmp_path: Path) -> None:
    _write(tmp_path / "AGENTS.md", "# Agents\n")
    snap = load_instructions(tmp_path)
    assert snap is not None
    assert snap.name == "AGENTS.md"


def test_load_none_when_missing(tmp_path: Path) -> None:
    # Empty directory → None
    assert load_instructions(tmp_path) is None


def test_load_none_when_all_empty(tmp_path: Path) -> None:
    _write(tmp_path / "NOETA.md", "   \n\t  ")
    _write(tmp_path / "AGENTS.md", "\n\n")
    assert load_instructions(tmp_path) is None
    # Empty NOETA.md is skipped, falls back to AGENTS.md
    _write(tmp_path / "AGENTS.md", "# real\n")
    snap = load_instructions(tmp_path)
    assert snap is not None
    assert snap.name == "AGENTS.md"


def test_load_override_path_wins(tmp_path: Path) -> None:
    _write(tmp_path / "NOETA.md", "# default\n")
    custom = tmp_path / "sub" / "MY-RULES.md"
    _write(custom, "# CUSTOM RULES\n")
    snap = load_instructions(tmp_path, override_path=custom)
    assert snap is not None
    assert snap.name == "MY-RULES.md"
    assert snap.text.startswith("# CUSTOM")


def test_load_override_missing_is_none(tmp_path: Path) -> None:
    _write(tmp_path / "NOETA.md", "# present\n")
    assert (
        load_instructions(tmp_path, override_path=tmp_path / "nope.md") is None
    )


def test_default_filenames_match_docstring() -> None:
    assert DEFAULT_INSTRUCTIONS_FILENAMES == ("NOETA.md", "AGENTS.md")


# ---------------------------------------------------------------------------
# 2. Pure-function units — render + hash (same pattern as memory)
# ---------------------------------------------------------------------------


def test_render_wraps_text_in_tag_with_source() -> None:
    text = render_instructions_text(_SAMPLE_SNAPSHOT)
    assert text.startswith('<workspace-instructions source="NOETA.md">')
    assert _SAMPLE_TEXT in text
    assert text.rstrip().endswith("</workspace-instructions>")


def test_hash_is_stable_and_tracks_content() -> None:
    # Stability: same snapshot twice → same hash.
    assert instructions_content_hash(_SAMPLE_SNAPSHOT) == (
        instructions_content_hash(_SAMPLE_SNAPSHOT)
    )
    # Content tracking: changed snapshot → different hash.
    other = InstructionsSnapshot(name="NOETA.md", text="different")
    assert instructions_content_hash(other) != (
        instructions_content_hash(_SAMPLE_SNAPSHOT)
    )
    # Hash is sha256 over rendered bytes — canonical source of truth.
    rendered = render_instructions_text(_SAMPLE_SNAPSHOT).encode("utf-8")
    assert instructions_content_hash(_SAMPLE_SNAPSHOT) == (
        hashlib.sha256(rendered).hexdigest()
    )


def test_renderer_renders_user_message_when_name_active() -> None:
    renderer = build_instructions_renderer(_SAMPLE_SNAPSHOT)
    rendered = renderer(["NOETA.md"])
    assert isinstance(rendered, RenderedSkills)
    assert len(rendered.messages) == 1
    assert rendered.messages[0].role == "user"
    assert "Reply in Chinese" in rendered.messages[0].content[0].text


def test_renderer_renders_nothing_when_inactive_or_empty() -> None:
    renderer = build_instructions_renderer(_SAMPLE_SNAPSHOT)
    # Name not in active list → no messages
    assert renderer([]).messages == []
    assert renderer(["AGENTS.md"]).messages == []
    # Empty text → no render even when the name is active
    empty = InstructionsSnapshot(name="NOETA.md", text="  \n\n ")
    assert build_instructions_renderer(empty)(["NOETA.md"]).messages == []


def test_kind_is_evolving_and_resolves_through_generic_seam() -> None:
    spec = instructions_content_kind(_SAMPLE_SNAPSHOT)
    assert spec.kind == INSTRUCTIONS_KIND
    assert spec.policy == "evolving"
    assert spec.policy == INSTRUCTIONS_DRIFT_POLICY
    resolve = ContentChannelRegistry([spec]).content_hashes()
    assert resolve(INSTRUCTIONS_KIND, "NOETA.md") == (
        INSTRUCTIONS_VERSION,
        instructions_content_hash(_SAMPLE_SNAPSHOT),
    )
    # Wrong name / wrong kind → None
    assert resolve(INSTRUCTIONS_KIND, "AGENTS.md") is None
    assert resolve("memory", "NOETA.md") is None


# ---------------------------------------------------------------------------
# 3. Channel E2E — record/activate, semi_stable render, source labels
# ---------------------------------------------------------------------------


def _runtime() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    return log, InMemoryContentStore(), disp


def _composer(
    cs: InMemoryContentStore, snapshot: Optional[InstructionsSnapshot]
) -> ThreeSegmentComposer:
    specs = []
    if snapshot is not None:
        specs.append(instructions_content_kind(snapshot))
    return ThreeSegmentComposer(
        system_prompt="instructions test agent",
        tools={},
        content_store=cs,
        content_renderers=ContentChannelRegistry(specs),
    )


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


def test_record_instructions_emits_evolving_event_and_activates() -> None:
    log, cs, _disp = _runtime()
    engine = _engine(log, cs, _composer(cs, _SAMPLE_SNAPSHOT))
    task = engine.create_task(goal="g", policy_name="scripted")

    task = record_instructions(log, cs, task, snapshot=_SAMPLE_SNAPSHOT)

    events = [
        e for e in log.read(task.task_id)
        if e.type == "ContextContentRecorded"
    ]
    assert len(events) == 1
    payload = events[0].payload
    assert payload.kind == INSTRUCTIONS_KIND
    assert payload.name == "NOETA.md"
    assert payload.policy == "evolving"
    assert payload.content_hash == instructions_content_hash(_SAMPLE_SNAPSHOT)
    assert payload.version == INSTRUCTIONS_VERSION
    assert task.state.active_content[INSTRUCTIONS_KIND] == ("NOETA.md",)


def test_record_instructions_first_only_and_noop_on_none() -> None:
    log, cs, _disp = _runtime()
    engine = _engine(log, cs, _composer(cs, _SAMPLE_SNAPSHOT))
    task = engine.create_task(goal="g", policy_name="scripted")

    # None → no events
    task = record_instructions(log, cs, task, snapshot=None)
    pre_events = [
        e for e in log.read(task.task_id)
        if e.type == "ContextContentRecorded"
    ]
    assert pre_events == []
    assert INSTRUCTIONS_KIND not in task.state.active_content

    # Same snapshot twice → recorded only once
    task = record_instructions(log, cs, task, snapshot=_SAMPLE_SNAPSHOT)
    task = record_instructions(log, cs, task, snapshot=_SAMPLE_SNAPSHOT)
    events = [
        e for e in log.read(task.task_id)
        if e.type == "ContextContentRecorded"
    ]
    assert len(events) == 1


def test_compose_places_instructions_in_semi_stable_pure() -> None:
    log, cs, _disp = _runtime()
    composer = _composer(cs, _SAMPLE_SNAPSHOT)
    engine = _engine(log, cs, composer)
    task = engine.create_task(goal="g", policy_name="scripted")
    task = record_instructions(log, cs, task, snapshot=_SAMPLE_SNAPSHOT)

    first = composer.compose(task)
    second = composer.compose(task)

    semi = [s for s in first.segments if s.name == "semi_stable"][0]
    assert len(semi.content) == 1
    body = semi.content[0].content[0].text
    assert "Reply in Chinese" in body
    assert body.startswith("<workspace-instructions")
    # Same ledger → byte-equivalent
    assert to_canonical_bytes(first.segments) == to_canonical_bytes(
        second.segments
    )


# ---------------------------------------------------------------------------
# 4. Zero footprint — flag off / no file → byte-for-byte unchanged
# ---------------------------------------------------------------------------


def test_instructions_disabled_default_no_change_in_builder(tmp_path: Path) -> None:
    """SDK defaults to False; build_session_inputs behaves as if the feature didn't exist."""
    _write(tmp_path / "NOETA.md", "# rules\n")
    baseline = build_session_inputs(
        workspace_dir=tmp_path,
        system_prompt="p",
        allowed_tools=frozenset({"read_file"}),
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
    )
    assert baseline.instructions_snapshot is None
    # environment is always-on; instructions off ⇒ skill + environment only.
    assert baseline.composer._content_renderers.kinds() == (
        "skill",
        ENVIRONMENT_KIND,
    )


def test_enabled_but_no_file_zero_footprint(tmp_path: Path) -> None:
    """Flag on but no file: the kind isn't registered at all, byte-equivalent to flag off."""
    inputs = build_session_inputs(
        workspace_dir=tmp_path,
        system_prompt="p",
        allowed_tools=frozenset({"read_file"}),
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        instructions_enabled=True,
    )
    assert inputs.instructions_snapshot is None
    assert inputs.composer._content_renderers.kinds() == (
        "skill",
        ENVIRONMENT_KIND,
    )


def test_enabled_adds_kind_after_skill_and_memory(tmp_path: Path) -> None:
    """Registration-order contract: skill → memory → instructions; keep the first two's byte layout."""
    _write(tmp_path / "NOETA.md", "# rules\n")
    # An empty memory dir is fine too; with memory_enabled=True, memory must take the second slot
    (tmp_path / ".noeta" / "memories").mkdir(parents=True)
    inputs = build_session_inputs(
        workspace_dir=tmp_path,
        system_prompt="p",
        allowed_tools=frozenset({"read_file"}),
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        instructions_enabled=True,
        memory_enabled=True,
    )
    assert inputs.composer._content_renderers.kinds() == (
        "skill",
        "memory",
        "instructions",
        ENVIRONMENT_KIND,
    )
    resolved = inputs.content_hashes(INSTRUCTIONS_KIND, "NOETA.md")
    assert resolved is not None
    assert resolved[1] == instructions_content_hash(inputs.instructions_snapshot)


def test_instructions_file_override_in_builder(tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere" / "CUSTOM.md"
    _write(custom, "# my rules\n")
    inputs = build_session_inputs(
        workspace_dir=tmp_path,
        system_prompt="p",
        allowed_tools=frozenset({"read_file"}),
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        instructions_enabled=True,
        instructions_file=custom,
    )
    assert inputs.instructions_snapshot is not None
    assert inputs.instructions_snapshot.name == "CUSTOM.md"
    assert inputs.composer._content_renderers.kinds() == (
        "skill",
        "instructions",
        ENVIRONMENT_KIND,
    )


# ---------------------------------------------------------------------------
# 6. Product wiring — the product session enables instructions; explicit False off
# ---------------------------------------------------------------------------


def _end_response(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _product_session(
    workspace: Path, *, responses, instructions_enabled: bool = True, **host_knobs
):
    """A one-shot production ``SdkHost`` + ``InteractionDriver`` for the
    instructions-channel product wiring. ``instructions_enabled`` defaults to
    True (the product session enables instructions; the SDK host's own field
    default is False, so the product wiring sets it on)."""
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=list(responses)),
        model="stub-model",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        instructions_enabled=instructions_enabled,
        **host_knobs,
    )
    return host, make_driver(host)


def test_product_default_instructions_enabled_records_event(
    tmp_path: Path,
) -> None:
    """Instructions enabled + NOETA.md present → records ContextContentRecorded."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write(ws / "NOETA.md", "# Project conventions\nReply in Chinese\n")
    host, driver = _product_session(ws, responses=[_end_response()])
    out = driver.start(goal="say hello", agent="main")
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    found = [
        e for e in events
        if e.type == "ContextContentRecorded"
        and getattr(e.payload, "kind", "") == INSTRUCTIONS_KIND
    ]
    assert len(found) == 1
    assert found[0].payload.policy == "evolving"
    assert found[0].payload.name == "NOETA.md"
    folded = fold(host.event_log, host.content_store, out.task_id)
    assert folded.state.active_content.get(INSTRUCTIONS_KIND) == (
        "NOETA.md",
    )
    # The instructions text is in semi_stable
    view = host.resolve_engine_for_agent(
        "main", model="stub-model"
    )._composer.compose(folded)
    semi = [s for s in view.segments if s.name == "semi_stable"][0]
    assert any(
        "Reply in Chinese" in block.text
        for msg in semi.content
        for block in msg.content
        if hasattr(block, "text")
    )


def test_product_instructions_explicit_false_no_event(tmp_path: Path) -> None:
    """Explicit instructions_enabled=False — no events even when a file exists."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write(ws / "NOETA.md", "# Project conventions\n")
    host, driver = _product_session(
        ws, responses=[_end_response()], instructions_enabled=False
    )
    out = driver.start(goal="say hello", agent="main")
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    assert not [
        e for e in events
        if e.type == "ContextContentRecorded"
        and getattr(e.payload, "kind", "") == INSTRUCTIONS_KIND
    ]


def test_product_no_instructions_file_zero_event(tmp_path: Path) -> None:
    """Enabled, but no instructions file → equivalent to explicit False, no events."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _product_session(ws, responses=[_end_response()])
    out = driver.start(goal="say hello", agent="main")
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    assert not [
        e for e in events
        if e.type == "ContextContentRecorded"
        and getattr(e.payload, "kind", "") == INSTRUCTIONS_KIND
    ]


def test_product_instructions_file_override(tmp_path: Path) -> None:
    """instructions_file override → uses the file name from the custom path."""
    ws = tmp_path / "ws"
    ws.mkdir()
    custom = tmp_path / "MY-TEAM.md"
    _write(custom, "# Team rules\n")
    host, driver = _product_session(
        ws, responses=[_end_response()], instructions_file=custom
    )
    out = driver.start(goal="say hello", agent="main")
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    found = [
        e for e in events
        if e.type == "ContextContentRecorded"
        and getattr(e.payload, "kind", "") == INSTRUCTIONS_KIND
    ]
    assert len(found) == 1
    assert found[0].payload.name == "MY-TEAM.md"
