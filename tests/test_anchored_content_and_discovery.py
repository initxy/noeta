"""Anchored content placement + `read`-triggered instructions discovery.

Covers docs/adr/anchored-content-placement.md end to end:

* fold records an activation anchor per resident (pre-loop 0, mid-task the
  rolling-history length, first-write-wins);
* the composer keeps pre-loop residents in ``semi_stable`` and renders
  mid-task residents ANCHORED inside the dynamic suffix — sliding past
  ``role="tool"`` messages, re-hanging after a compaction summary;
* ``discover_instructions`` walks a read file's ancestor directories inside
  the workspace (precedence, dedup, naming, containment);
* the engine seam: with ``instructions_discovery=True`` a successful ``read``
  emits first-only ``ContextContentRecorded(kind=instructions)`` whose anchor
  lands AFTER the batched tool results;
* resume: ``content_preloader`` re-reads active-but-unloaded names.
"""

from __future__ import annotations

from pathlib import Path

from tests._session_inputs import default_factory_kwargs
from noeta.context.composer import RenderedSkills, ThreeSegmentComposer
from noeta.context.content_channel import (
    ContentChannelRegistry,
    ContentKindSpec,
)
from noeta.core.fold import fold
from noeta.builtins.workspace.impl import (
    DEFAULT_INSTRUCTIONS_FILENAMES as _FILENAMES,
    build_instructions_kit,
)
from noeta.execution.instructions import (
    build_instructions_discovery,
    discover_instructions,
)
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import ToolCall
from noeta.protocols.events import (
    ContextContentRecordedPayload,
    TaskCreatedPayload,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.task import Task
from noeta.protocols.tool import ToolResult
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.workspace import WorkspaceRoot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _note_registry() -> ContentChannelRegistry:
    """One fake kind: renders ``note:<name>`` per name, input order."""

    def _render(names: list[str]) -> RenderedSkills:
        return RenderedSkills(
            messages=[
                Message(role="user", content=[TextBlock(text=f"note:{n}")])
                for n in names
            ],
            selected_skills=[],
        )

    return ContentChannelRegistry(
        [ContentKindSpec(kind="note", renderer=_render, policy="evolving")]
    )


def _composer(cs: InMemoryContentStore) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="sys",
        tools={},
        content_store=cs,
        content_renderers=_note_registry(),
    )


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=[TextBlock(text=text)])


def _texts(messages: list[Message]) -> list[str]:
    return [
        b.text
        for m in messages
        for b in m.content
        if isinstance(b, TextBlock)
    ]


def _task_with(
    *,
    messages: list[Message],
    active: dict[str, tuple[str, ...]],
    anchors: dict[str, int],
) -> Task:
    task = Task(task_id="t1")
    task.runtime.messages = list(messages)
    task.state.active_content = dict(active)
    task.context.content_anchors = dict(anchors)
    return task


def _content_event(name: str, kind: str = "note") -> ContextContentRecordedPayload:
    return ContextContentRecordedPayload(
        kind=kind, name=name, version="1", content_hash="h" * 64, policy="evolving"
    )


# ---------------------------------------------------------------------------
# fold — anchor recording
# ---------------------------------------------------------------------------


def test_fold_records_preloop_anchor_zero() -> None:
    log, cs = InMemoryEventLog(), InMemoryContentStore()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1", type="ContextContentRecorded", payload=_content_event("a")
    )
    task = fold(log, cs, "t1")
    assert task.context.content_anchors == {"note:a": 0}


def test_fold_anchor_is_history_length_and_first_write_wins() -> None:
    from noeta.core._decision_handlers import put_messages

    log, cs = InMemoryEventLog(), InMemoryContentStore()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1",
        type="MessagesAppended",
        payload=put_messages(cs, [_msg("user", "goal"), _msg("assistant", "hi")]),
    )
    log.emit(
        task_id="t1", type="ContextContentRecorded", payload=_content_event("a")
    )
    log.emit(
        task_id="t1",
        type="MessagesAppended",
        payload=put_messages(cs, [_msg("user", "more")]),
    )
    # Re-emitted activation must NOT move the anchor (first-write-wins).
    log.emit(
        task_id="t1", type="ContextContentRecorded", payload=_content_event("a")
    )
    task = fold(log, cs, "t1")
    assert task.context.content_anchors == {"note:a": 2}


# ---------------------------------------------------------------------------
# composer — placement
# ---------------------------------------------------------------------------


def test_preloop_resident_stays_in_semi_stable() -> None:
    cs = InMemoryContentStore()
    task = _task_with(
        messages=[_msg("user", "goal"), _msg("assistant", "hi")],
        active={"note": ("a",)},
        anchors={"note:a": 1},  # at the first assistant index — NOT after it
    )
    view = _composer(cs).compose(task)
    assert _texts(view.segments[1].content) == ["note:a"]
    assert "note:a" not in _texts(view.segments[2].content)


def test_anchorless_resident_from_old_snapshot_stays_in_semi_stable() -> None:
    cs = InMemoryContentStore()
    task = _task_with(
        messages=[_msg("user", "goal"), _msg("assistant", "hi")],
        active={"note": ("a",)},
        anchors={},
    )
    view = _composer(cs).compose(task)
    assert _texts(view.segments[1].content) == ["note:a"]


def test_midtask_resident_renders_at_its_anchor() -> None:
    cs = InMemoryContentStore()
    task = _task_with(
        messages=[
            _msg("user", "goal"),
            _msg("assistant", "turn-1"),
            _msg("user", "turn-2"),
            _msg("assistant", "turn-3"),
        ],
        active={"note": ("a",)},
        anchors={"note:a": 2},
    )
    view = _composer(cs).compose(task)
    assert view.segments[1].content == []
    assert _texts(view.segments[2].content) == [
        "goal",
        "turn-1",
        "note:a",
        "turn-2",
        "turn-3",
    ]


def test_insertion_slides_past_tool_results_message() -> None:
    cs = InMemoryContentStore()
    task = _task_with(
        messages=[
            _msg("user", "goal"),
            _msg("assistant", "calls-a-tool"),
            _msg("tool", "tool-results"),
            _msg("assistant", "after"),
        ],
        active={"note": ("a",)},
        # Anchor points AT the tool-results message (the activation folded
        # before the batch was appended) — the insert must slide past it,
        # never splitting the tool_use from its results.
        anchors={"note:a": 2},
    )
    view = _composer(cs).compose(task)
    assert _texts(view.segments[2].content) == [
        "goal",
        "calls-a-tool",
        "tool-results",
        "note:a",
        "after",
    ]


def test_covered_anchor_rehangs_after_compaction_summary() -> None:
    cs = InMemoryContentStore()
    summary_ref = cs.put(to_canonical_bytes("SUMMARY"), media_type="application/json")
    task = _task_with(
        messages=[
            _msg("user", "goal"),
            _msg("assistant", "old-1"),
            _msg("user", "old-2"),
            _msg("assistant", "recent"),
        ],
        active={"note": ("a", "b")},
        # "a" is covered by the summary (anchor 2 < boundary 3) → re-hangs
        # right after the summary message; "b" (anchor 4) keeps its mapped
        # position (4 - 3 + 1 = 2 → after "recent").
        anchors={"note:a": 2, "note:b": 4},
    )
    task.context.summary_ref = summary_ref
    task.context.summary_boundary = 3
    view = _composer(cs).compose(task)
    assert _texts(view.segments[2].content) == [
        "SUMMARY",
        "note:a",
        "recent",
        "note:b",
    ]


def test_compose_is_deterministic_with_anchored_content() -> None:
    cs = InMemoryContentStore()
    task = _task_with(
        messages=[_msg("user", "goal"), _msg("assistant", "hi"), _msg("user", "x")],
        active={"note": ("a",)},
        anchors={"note:a": 2},
    )
    composer = _composer(cs)
    v1 = composer.compose(task)
    v2 = composer.compose(task)
    assert [s.segment_hash for s in v1.segments] == [
        s.segment_hash for s in v2.segments
    ]


# ---------------------------------------------------------------------------
# discover_instructions — the walk
# ---------------------------------------------------------------------------


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src" / "pkg").mkdir(parents=True)
    return ws


def test_discovery_walks_ancestors_shallowest_first(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws / "src" / "AGENTS.md").write_text("src rules")
    (ws / "src" / "pkg" / "AGENTS.md").write_text("pkg rules")
    found = discover_instructions(ws, ws / "src" / "pkg" / "x.py", filenames=_FILENAMES)
    assert [s.name for s in found] == ["src/AGENTS.md", "src/pkg/AGENTS.md"]
    assert found[0].text == "src rules"


def test_discovery_prefers_noeta_md_and_skips_empty(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws / "src" / "NOETA.md").write_text("noeta wins")
    (ws / "src" / "AGENTS.md").write_text("agents loses")
    (ws / "src" / "pkg" / "NOETA.md").write_text("   \n")  # empty → falls over
    (ws / "src" / "pkg" / "AGENTS.md").write_text("agents wins here")
    found = discover_instructions(ws, ws / "src" / "pkg" / "x.py", filenames=_FILENAMES)
    assert [(s.name, s.text) for s in found] == [
        ("src/NOETA.md", "noeta wins"),
        ("src/pkg/AGENTS.md", "agents wins here"),
    ]


def test_discovery_skips_active_directories_and_root(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws / "AGENTS.md").write_text("root file — pre-loop loader's job")
    (ws / "src" / "AGENTS.md").write_text("src rules")
    (ws / "src" / "pkg" / "AGENTS.md").write_text("pkg rules")
    found = discover_instructions(
        ws,
        ws / "src" / "pkg" / "x.py",
        filenames=_FILENAMES,
        active=("src/AGENTS.md",),
    )
    assert [s.name for s in found] == ["src/pkg/AGENTS.md"]


def test_discovery_outside_workspace_yields_nothing(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "AGENTS.md").write_text("must not load")
    assert discover_instructions(ws, other / "x.py", filenames=_FILENAMES) == []


def test_discovery_root_level_file_yields_nothing(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws / "AGENTS.md").write_text("root")
    assert discover_instructions(ws, ws / "x.py", filenames=_FILENAMES) == []


# ---------------------------------------------------------------------------
# build_instructions_discovery — the seam callable
# ---------------------------------------------------------------------------


def test_discovery_callable_fills_mapping_and_builds_payloads(
    tmp_path: Path,
) -> None:
    ws = _ws(tmp_path)
    (ws / "src" / "AGENTS.md").write_text("src rules")
    snapshots: dict = {}
    discover = build_instructions_discovery(
        WorkspaceRoot.from_path(ws), snapshots, kit=build_instructions_kit()
    )
    task = Task(task_id="t1")
    call = ToolCall(tool_name="read", arguments={"path": "src/pkg/x.py"}, call_id="c")
    payloads = discover(task, call, ToolResult(success=True))
    assert [p.name for p in payloads] == ["src/AGENTS.md"]
    assert payloads[0].kind == "instructions"
    assert payloads[0].policy == "evolving"
    assert len(payloads[0].content_hash) == 64
    assert "src/AGENTS.md" in snapshots


def test_discovery_callable_ignores_non_read_failures_and_outside(
    tmp_path: Path,
) -> None:
    ws = _ws(tmp_path)
    (ws / "src" / "AGENTS.md").write_text("src rules")
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("outside")
    snapshots: dict = {}
    discover = build_instructions_discovery(
        WorkspaceRoot.from_path(ws), snapshots, kit=build_instructions_kit()
    )
    task = Task(task_id="t1")
    read_call = ToolCall(
        tool_name="read", arguments={"path": "src/x.py"}, call_id="c"
    )
    grep_call = ToolCall(
        tool_name="grep", arguments={"path": "src/x.py"}, call_id="c"
    )
    outside_call = ToolCall(
        tool_name="read",
        arguments={"path": str(outside / "x.py")},
        call_id="c",
    )
    assert discover(task, grep_call, ToolResult(success=True)) == []
    assert discover(task, read_call, ToolResult(success=False)) == []
    assert discover(task, outside_call, ToolResult(success=True)) == []
    assert snapshots == {}


# ---------------------------------------------------------------------------
# engine integration — read → activation → anchored render; resume preload
# ---------------------------------------------------------------------------


def _read_call(path: str, call_id: str = "r1") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id, tool_name="read", arguments={"path": path}
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _build_discovery_engine(ws: Path, responses: list[LLMResponse]):
    from noeta.core.engine import Engine
    from noeta.core.wiring import wire_default_observers
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.runtime.governance import Budget
    from noeta.runtime.llm import RuntimeLLMClient
    from noeta.runtime.tool import ToolRuntime
    from noeta.storage.memory import (
        InMemoryContentStore,
        InMemoryDispatcher,
        InMemoryEventLog,
    )

    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)

    def _inputs():
        return build_session_inputs(
            **default_factory_kwargs(),
            workspace_dir=ws,
            system_prompt="sys",
            allowed_tools=frozenset({"read"}),
            content_store=cs,
            model="stub-model",
            compaction=COMPACTION_OFF,
            budget=Budget(),
            instructions_discovery=True,
        )

    inputs = _inputs()
    provider = FakeLLMProvider(responses=list(responses))
    client = RuntimeLLMClient(provider=provider, event_log=log, content_store=cs)
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=inputs.composer,
        policy=inputs.policy_factory(client),
        tools=inputs.tools,
        tool_runtime=ToolRuntime(event_log=log, content_store=cs),
        hooks=inputs.hooks,
        content_hashes=inputs.content_hashes,
        content_discovery=inputs.content_discovery,
        content_preloader=inputs.content_preloader,
    )
    return engine, disp, cs, log, _inputs


def _run_to_terminal(engine, disp, task) -> None:
    for _ in range(30):
        lease = disp.lease(worker_id="w")
        if lease is None:
            return
        task = engine.run_one_step(task, lease_id=lease.lease_id)
        if task.status in ("completed", "failed", "suspended"):
            return


def test_engine_read_discovers_and_renders_anchored(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws / "src" / "AGENTS.md").write_text("src conventions")
    (ws / "src" / "pkg" / "AGENTS.md").write_text("pkg conventions")
    (ws / "src" / "pkg" / "x.py").write_text("print('hi')\n")

    engine, disp, cs, log, _inputs = _build_discovery_engine(
        ws, [_read_call("src/pkg/x.py"), _end()]
    )
    task = engine.create_task(goal="read a file", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)
    tid = task.task_id

    events = [e for e in log.read(tid) if e.type == "ContextContentRecorded"]
    assert [e.payload.name for e in events] == [
        "src/AGENTS.md",
        "src/pkg/AGENTS.md",
    ]
    assert all(e.payload.kind == "instructions" for e in events)

    folded = fold(log, cs, tid)
    active = folded.state.active_content.get("instructions", ())
    assert active == ("src/AGENTS.md", "src/pkg/AGENTS.md")
    # Anchors land AFTER the batched tool-results message.
    tool_indices = [
        i for i, m in enumerate(folded.runtime.messages) if m.role == "tool"
    ]
    for name in active:
        anchor = folded.context.content_anchors[f"instructions:{name}"]
        assert anchor > tool_indices[0]

    view = engine._composer.compose(folded)
    assert view.segments[1].content == []  # nothing in semi_stable
    msgs = view.segments[2].content

    def _msg_index(sub: str) -> int:
        return next(
            i
            for i, m in enumerate(msgs)
            if any(
                isinstance(b, TextBlock) and sub in b.text for b in m.content
            )
        )

    src_idx = _msg_index('source="src/AGENTS.md"')
    pkg_idx = _msg_index('source="src/pkg/AGENTS.md"')
    assert _msg_index("src conventions") == src_idx
    assert _msg_index("pkg conventions") == pkg_idx
    assert src_idx < pkg_idx
    # Both render after the tool-results message of the read turn.
    first_tool = min(i for i, m in enumerate(msgs) if m.role == "tool")
    assert src_idx > first_tool


def test_engine_second_read_does_not_reactivate(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws / "src" / "AGENTS.md").write_text("src conventions")
    (ws / "src" / "pkg" / "x.py").write_text("a\n")
    (ws / "src" / "pkg" / "y.py").write_text("b\n")

    engine, disp, cs, log, _inputs = _build_discovery_engine(
        ws,
        [_read_call("src/pkg/x.py", "r1"), _read_call("src/pkg/y.py", "r2"), _end()],
    )
    task = engine.create_task(goal="read two files", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)

    events = [
        e
        for e in log.read(task.task_id)
        if e.type == "ContextContentRecorded"
    ]
    assert [e.payload.name for e in events] == ["src/AGENTS.md"]


def test_resume_preloader_rereads_discovered_files(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws / "src" / "AGENTS.md").write_text("src conventions")
    (ws / "src" / "pkg" / "x.py").write_text("a\n")

    engine, disp, cs, log, _inputs = _build_discovery_engine(
        ws, [_read_call("src/pkg/x.py"), _end()]
    )
    task = engine.create_task(goal="read", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)

    # Fresh-process resume: new inputs (empty snapshot mapping).
    resumed_inputs = _inputs()
    folded = fold(log, cs, task.task_id)
    # Without preload the name is active but unrenderable.
    view = resumed_inputs.composer.compose(folded)
    assert not any(
        "src conventions" in t for t in _texts(view.segments[2].content)
    )
    # Preload re-reads it from the workspace; the compose then renders it.
    resumed_inputs.content_preloader(folded)
    view = resumed_inputs.composer.compose(folded)
    dyn = _texts(view.segments[2].content)
    assert any(
        'source="src/AGENTS.md"' in t and "src conventions" in t for t in dyn
    )


def test_preloader_degrades_when_file_vanishes(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws / "src" / "AGENTS.md").write_text("src conventions")
    (ws / "src" / "pkg" / "x.py").write_text("a\n")

    engine, disp, cs, log, _inputs = _build_discovery_engine(
        ws, [_read_call("src/pkg/x.py"), _end()]
    )
    task = engine.create_task(goal="read", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)

    (ws / "src" / "AGENTS.md").unlink()
    resumed_inputs = _inputs()
    folded = fold(log, cs, task.task_id)
    resumed_inputs.content_preloader(folded)  # must not raise
    view = resumed_inputs.composer.compose(folded)
    assert not any(
        "src conventions" in t for t in _texts(view.segments[2].content)
    )


def test_discovery_off_by_default_no_seams(tmp_path: Path) -> None:
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.runtime.governance import Budget

    ws = _ws(tmp_path)
    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=ws,
        system_prompt="sys",
        allowed_tools=frozenset({"read"}),
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
    )
    assert inputs.content_discovery is None
    assert inputs.content_preloader is None
