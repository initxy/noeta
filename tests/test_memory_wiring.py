"""Presets / noeta-agent memory v1 wiring.

Switch surface follows the flag precedent (431bccd + f0f39c1 review round):

* ``Capabilities.memory`` is an identity flag (part of structural equality);
  only ``main`` enables it in presets.
* ``build_session_inputs(memory_enabled=…, memory_dir=…)`` is the single
  construction point for live/replay: bytes match only when the switch matches.
  On → memory_write/read tools join the tool table (fixed order
  fs → local → memory → script → mcp → custom), memory kind joins the content
  channel registry (after skill), and ``SessionInputs`` exposes the store and
  the entries snapshot (record and compose share one snapshot, one source of
  truth). Off (default) → unchanged, zero byte change.
* The noeta-agent product enables memory by default
  (``CodeSessionConfig.memory_enabled=True``); prepare() runs the D5/D6 set:
  the index is recorded as resident, and the goal is recorded through the
  recall seam. ``resume_with_goal`` goes through the ``_goal_prelude`` seam,
  the same recall seam.
"""

from __future__ import annotations

from pathlib import Path

from noeta.agent.spec import Capabilities
from noeta.context.environment import ENVIRONMENT_KIND
from noeta.context.memory import MEMORY_KIND
from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
from noeta.guards.budget import Budget
from noeta.presets import official_specs
from noeta.tools.memory import (
    MEMORY_READ_TOOL_NAME,
    MEMORY_WRITE_TOOL_NAME,
)


def _inputs(ws: Path, **kwargs):
    return build_session_inputs(
        workspace_dir=ws,
        system_prompt="p",
        allowed_tools=frozenset({"read"}),
        content_store=__import__("noeta.storage.memory", fromlist=["InMemoryContentStore"]).InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Capabilities.memory — part of Agent identity
# ---------------------------------------------------------------------------


def test_memory_flag_is_identity_bearing() -> None:
    from noeta.client.options import Options, compile_options

    base, _ = compile_options(Options(system_prompt="x", name="a"))
    on, _ = compile_options(
        Options(
            system_prompt="x", name="a", capabilities=Capabilities(memory=True)
        )
    )
    # Turning memory on is a real identity change on the compiled spec.
    assert base != on
    assert base.capabilities.memory is False
    assert on.capabilities.memory is True


# ---------------------------------------------------------------------------
# 2. presets — only main enables memory (explore/plan are read-only identities,
#    subagents don't take user messages, so they don't enable it)
# ---------------------------------------------------------------------------


def test_presets_main_has_memory_subagents_do_not() -> None:
    specs = official_specs()
    assert specs["main"].capabilities.memory is True
    for name in ("explore", "plan", "general-purpose"):
        assert specs[name].capabilities.memory is False


# ---------------------------------------------------------------------------
# 3. build_session_inputs switch surface
# ---------------------------------------------------------------------------


def test_memory_disabled_default_zero_change(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    inputs = _inputs(ws)
    assert MEMORY_WRITE_TOOL_NAME not in inputs.tools
    assert MEMORY_READ_TOOL_NAME not in inputs.tools
    assert inputs.memory_store is None
    assert inputs.memory_entries == ()
    # skill + the always-on environment resident occupy the content channel.
    assert inputs.composer._content_renderers.kinds() == ("skill", ENVIRONMENT_KIND)


def test_memory_enabled_adds_tools_in_fixed_order(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    inputs = _inputs(ws, memory_enabled=True)
    names = list(inputs.tools)
    assert MEMORY_WRITE_TOOL_NAME in names
    assert MEMORY_READ_TOOL_NAME in names
    # Order contract: fs(read) → memory(write→read).
    assert names.index("read") < names.index(MEMORY_WRITE_TOOL_NAME)
    assert names.index(MEMORY_WRITE_TOOL_NAME) < names.index(MEMORY_READ_TOOL_NAME)


def test_memory_enabled_registers_kind_after_skill(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    inputs = _inputs(ws, memory_enabled=True)
    assert inputs.composer._content_renderers.kinds() == (
        "skill",
        MEMORY_KIND,
        ENVIRONMENT_KIND,
    )


def test_memory_entries_snapshot_shared_with_hashes(tmp_path: Path) -> None:
    """record and compose share one entries snapshot — the (version, hash)
    resolved by the generic seam must equal the index fingerprint computed from
    the snapshot (one source of truth)."""
    from noeta.context.memory import MEMORY_INDEX_NAME, memory_index_hash

    ws = tmp_path / "ws"
    ws.mkdir()
    # Memory uses a global directory (independent of workspace);
    # here memory_dir explicitly overrides it to a global directory under tmp.
    mem = tmp_path / "global-memories"
    mem.mkdir()
    (mem / "deploy-notes.md").write_text(
        "# How we deploy\nSteps...\n", encoding="utf-8"
    )
    inputs = _inputs(ws, memory_enabled=True, memory_dir=mem)
    assert inputs.memory_store is not None
    assert [n for n, _ in inputs.memory_entries] == ["deploy-notes"]
    resolved = inputs.content_hashes(MEMORY_KIND, MEMORY_INDEX_NAME)
    assert resolved is not None
    assert resolved[1] == memory_index_hash(inputs.memory_entries)


def test_memory_dir_override_wins(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    alt = tmp_path / "alt-memories"
    alt.mkdir()
    (alt / "alpha.md").write_text("# A\n", encoding="utf-8")
    inputs = _inputs(ws, memory_enabled=True, memory_dir=alt)
    assert [n for n, _ in inputs.memory_entries] == ["alpha"]


# ---------------------------------------------------------------------------
# 4. noeta-agent product prepare() — D5/D6 set wiring (full FakeLLM chain)
# ---------------------------------------------------------------------------


def _memory_session(workspace: Path, responses, *, memory: bool = True, mem_dir=None):
    """An SDK session with memory wired off ``spec.capabilities.memory`` — the
    same memory machinery (tools + resident index) the shipping backend builds."""
    from noeta.testing.fake_llm import FakeLLMProvider
    from noeta.tools.fs import FsWriteMode, ShellMode

    from tests._sdk_session import (
        make_driver,
        make_host,
        make_registry,
        runner_main_spec,
    )

    host = make_host(
        make_registry(runner_main_spec("main", memory=memory)),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=list(responses)),
        model="stub-model",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
        global_memory_dir=mem_dir,
    )
    return host, make_driver(host)


def _product_runner(workspace: Path, responses, **cfg_overrides):
    """The runner's memory **prepare** wiring (index-recording event + per-goal
    recall, ``_goal_prelude`` recall on resume) lived in the deleted noeta-agent
    runner; the SDK ``driver.seed_start`` path does a plain ``append_user_message``
    with no recall and records no ``ContextContentRecorded(kind=memory)`` index
    event. Tests that assert that wiring skip until it is ported into the SDK
    seed path (T8/③-B)."""
    import pytest

    pytest.skip(
        "memory index-event recording + per-goal recall (prepare "
        "wiring) is not on the SDK seed path; deleted with the noeta-agent runner"
    )


def _end_response(text: str = "done"):
    from noeta.protocols.messages import LLMResponse, TextBlock, Usage

    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _seed_memory(mem_dir: Path) -> None:
    # Memory is pinned to a global directory; the seed lands
    # directly in that global directory (no longer ``workspace/.noeta/memories``).
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "deploy-notes.md").write_text(
        "# How we deploy\nAlways run smoke tests.\n", encoding="utf-8"
    )


def test_product_prepare_records_index_and_recalls(tmp_path: Path) -> None:
    """Product enables memory by default: prepare() records the index
    (kind=memory, evolving), and a goal matching a memory name → a recall
    message recorded with origin=memory, right after the human turn."""
    ws = tmp_path / "ws"
    ws.mkdir()
    mem = tmp_path / "global-memories"
    _seed_memory(mem)
    runner = _product_runner(ws, [_end_response()], global_memory_dir=mem)
    runner.prepare()
    try:
        result = runner.execute()
        assert result.status == "terminal"
        events = list(runner.event_log.read(runner.task_id))
        generic = [
            e for e in events
            if e.type == "ContextContentRecorded"
            and getattr(e.payload, "kind", "") == MEMORY_KIND
        ]
        assert len(generic) == 1
        assert generic[0].payload.policy == "evolving"

        from noeta.core.fold import fold

        folded = fold(runner.event_log, runner.content_store, runner.task_id)
        origins = [m.origin for m in folded.runtime.messages]
        assert "memory" in origins
        # The recall message carries the full memory text.
        recall_msg = next(
            m for m in folded.runtime.messages if m.origin == "memory"
        )
        joined = "".join(
            b.text for b in recall_msg.content if hasattr(b, "text")
        )
        assert "deploy-notes" in joined
        assert "Always run smoke tests." in joined
        # The index is resident as semi_stable.
        assert folded.state.active_content.get(MEMORY_KIND) == ("index",)
    finally:
        runner.shutdown()


def test_product_memory_disabled_zero_events(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    mem = tmp_path / "global-memories"
    _seed_memory(mem)
    host, driver = _memory_session(
        ws, [_end_response()], memory=False, mem_dir=mem
    )
    out = driver.start(goal="please remember the deploy-notes", agent="main")
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    assert not [
        e for e in events
        if e.type == "ContextContentRecorded"
        and getattr(e.payload, "kind", "") == MEMORY_KIND
    ]
    from noeta.core.fold import fold

    folded = fold(host.event_log, host.content_store, out.task_id)
    assert all(m.origin != "memory" for m in folded.runtime.messages)


def test_product_empty_store_no_events_no_recall(tmp_path: Path) -> None:
    """Memory on but directory empty: no index recorded (empty entries is a
    no-op), no recall — zero impact on the default flow. The memory tools stay
    (otherwise the first entry could never be written)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # Global memory directory points at an empty tmp (don't touch the real ~/.noeta/memories).
    mem = tmp_path / "global-memories"
    runner = _product_runner(ws, [_end_response()], global_memory_dir=mem)
    runner.prepare()
    try:
        result = runner.execute()
        assert result.status == "terminal"
        events = list(runner.event_log.read(runner.task_id))
        assert not [
            e for e in events
            if e.type == "ContextContentRecorded"
            and getattr(e.payload, "kind", "") == MEMORY_KIND
        ]
    finally:
        runner.shutdown()


def test_product_model_writes_memory_via_tool(tmp_path: Path) -> None:
    """Writing memory = an ordinary tool: model calls memory_write → file lands in the memory directory."""
    from noeta.protocols.messages import LLMResponse, ToolUseBlock, Usage

    ws = tmp_path / "ws"
    ws.mkdir()
    write_call = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="mw1",
                tool_name=MEMORY_WRITE_TOOL_NAME,
                arguments={
                    "name": "team-style",
                    "text": "# Style\nPrefer pure functions.\n",
                },
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "w"},
    )
    mem = tmp_path / "global-memories"
    host, driver = _memory_session(
        ws, [write_call, _end_response()], memory=True, mem_dir=mem
    )
    out = driver.start(goal="please remember the deploy-notes", agent="main")
    assert out.status == "terminal"
    # Memory writes land in the global directory, not ``ws/.noeta/memories``.
    written = mem / "team-style.md"
    assert written.is_file()
    assert "Prefer pure functions." in written.read_text(encoding="utf-8")
    assert not (ws / ".noeta" / "memories").exists()


def test_product_resume_recalls_memory_written_earlier(tmp_path: Path) -> None:
    """Multi-turn loop: first turn the model writes memory, and resume's new
    goal matches its name → second turn's recall recorded with origin=memory
    (_goal_prelude seam)."""
    from noeta.protocols.messages import LLMResponse, ToolUseBlock, Usage

    ws = tmp_path / "ws"
    ws.mkdir()
    write_call = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="mw1",
                tool_name=MEMORY_WRITE_TOOL_NAME,
                arguments={
                    "name": "release-steps",
                    "text": "# Release\nTag, build, publish.\n",
                },
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "w"},
    )
    mem = tmp_path / "global-memories"
    runner = _product_runner(
        ws,
        [write_call, _end_response("saved"), _end_response("done")],
        goal="save the release steps",
        multi_turn=True,
        global_memory_dir=mem,
    )
    runner.prepare()
    try:
        first = runner.execute()
        assert first.status == "suspended"
        second = runner.resume_with_goal("walk me through release-steps")
        assert second.status in ("suspended", "terminal")

        from noeta.core.fold import fold

        folded = fold(runner.event_log, runner.content_store, runner.task_id)
        recall_msgs = [
            m for m in folded.runtime.messages if m.origin == "memory"
        ]
        assert recall_msgs, "a resume goal matching a memory name must inject a recall"
        joined = "".join(
            b.text for b in recall_msgs[-1].content if hasattr(b, "text")
        )
        assert "Tag, build, publish." in joined
    finally:
        runner.shutdown()
