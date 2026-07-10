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
* The SDK seed path runs the D5/D6 set for a memory-enabled agent (the port
  of the deleted noeta-agent runner's prepare() / ``_goal_prelude`` wiring):
  ``driver.seed_start`` records the index as resident
  (``ContextContentRecorded`` kind=memory, policy=evolving) and the goal
  enters through the recall seam (``append_user_message_with_recall``);
  ``driver.send_goal`` routes a follow-up goal through ``RecallGoalPrelude``,
  the same recall seam. The host seam (``SdkHost.memory_recall_context``)
  returns ``None`` for a memory-off spec, keeping that stream byte-identical.
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
    MEMORY_ARCHIVE_TOOL_NAME,
    MEMORY_READ_TOOL_NAME,
    MEMORY_SEARCH_TOOL_NAME,
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
    pack = [
        MEMORY_WRITE_TOOL_NAME,
        MEMORY_READ_TOOL_NAME,
        MEMORY_SEARCH_TOOL_NAME,
        MEMORY_ARCHIVE_TOOL_NAME,
    ]
    assert all(name in names for name in pack)
    # Order contract: fs(read) → memory(write→read→search→archive).
    assert names.index("read") < names.index(MEMORY_WRITE_TOOL_NAME)
    assert [names.index(n) for n in pack] == sorted(
        names.index(n) for n in pack
    )


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
    assert [n for n, _, _ in inputs.memory_entries] == ["deploy-notes"]
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
    assert [n for n, _, _ in inputs.memory_entries] == ["alpha"]


# ---------------------------------------------------------------------------
# 4. SDK seed path — D5/D6 set wiring (full FakeLLM chain)
# ---------------------------------------------------------------------------


def _memory_session(
    workspace: Path,
    responses,
    *,
    memory: bool = True,
    mem_dir=None,
    multi_turn: bool = False,
):
    """An SDK session with memory wired off ``spec.capabilities.memory`` — the
    same memory machinery (tools + resident index + seed-path recall) the
    shipping backend builds."""
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
        multi_turn=multi_turn,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
        global_memory_dir=mem_dir,
    )
    return host, make_driver(host)


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
    """A memory-enabled session's seed (the prepare() counterpart) records the
    index (kind=memory, evolving), and a goal matching a memory name → a recall
    message recorded with origin=memory, right after the human turn."""
    ws = tmp_path / "ws"
    ws.mkdir()
    mem = tmp_path / "global-memories"
    _seed_memory(mem)
    host, driver = _memory_session(
        ws, [_end_response()], memory=True, mem_dir=mem
    )
    out = driver.start(goal="please remember the deploy-notes", agent="main")
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    generic = [
        e for e in events
        if e.type == "ContextContentRecorded"
        and getattr(e.payload, "kind", "") == MEMORY_KIND
    ]
    assert len(generic) == 1
    assert generic[0].payload.policy == "evolving"

    from noeta.core.fold import fold

    folded = fold(host.event_log, host.content_store, out.task_id)
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
    # The recall turn lands right after the human goal turn.
    goal_index = next(
        i for i, m in enumerate(folded.runtime.messages)
        if m.origin is None and m.role == "user"
    )
    assert folded.runtime.messages[goal_index + 1].origin == "memory"
    # The index is resident as semi_stable.
    assert folded.state.active_content.get(MEMORY_KIND) == ("index",)


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
    host, driver = _memory_session(
        ws, [_end_response()], memory=True, mem_dir=mem
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
    """Multi-turn loop: first turn the model writes memory, and the follow-up
    goal matches its name → second turn's recall recorded with origin=memory
    (RecallGoalPrelude, the ``_goal_prelude`` seam's SDK port)."""
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
    host, driver = _memory_session(
        ws,
        [write_call, _end_response("saved"), _end_response("done")],
        memory=True,
        mem_dir=mem,
        multi_turn=True,
    )
    first = driver.start(goal="save the release steps", agent="main")
    assert first.status == "suspended"
    second = driver.send_goal(
        first.task_id, goal="walk me through release-steps"
    )
    assert second.status in ("suspended", "terminal")

    from noeta.core.fold import fold

    folded = fold(host.event_log, host.content_store, first.task_id)
    recall_msgs = [
        m for m in folded.runtime.messages if m.origin == "memory"
    ]
    assert recall_msgs, "a resume goal matching a memory name must inject a recall"
    joined = "".join(
        b.text for b in recall_msgs[-1].content if hasattr(b, "text")
    )
    assert "Tag, build, publish." in joined
