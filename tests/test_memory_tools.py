"""D5 — memory v1 write/read tools: plain tools, zero special mechanism.

* ``MemoryStore`` — file-per-memory store under one directory: slug-named
  ``<name>.md`` files, traversal-proof name validation, deterministic
  (sorted) index entries with a first-line summary.
* ``memory_write`` / ``memory_read`` — plain SDK tools over the store,
  same dataclass shape as the fs tool pack. Results travel the ordinary
  tool-result channel (``ToolCallStarted → ToolResultRecorded →
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
from noeta.tools._limits import INLINE_CONTENT_MAX_BYTES
from noeta.tools.memory import (
    MEMORY_READ_TOOL_NAME,
    MEMORY_WRITE_TOOL_NAME,
    MemoryReadTool,
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
        ("alpha", "plain first line"),
        ("zeta", "Zeta heading"),
    )


def test_store_entries_missing_root_is_empty(tmp_path: Path) -> None:
    store = MemoryStore(root=tmp_path / "nope" / "memories")
    assert store.entries() == ()


def test_store_entries_ignore_foreign_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("real", "memory body")
    store.root.joinpath("not-a-memory.txt").write_text("x", encoding="utf-8")
    assert [name for name, _ in store.entries()] == ["real"]


# ---------------------------------------------------------------------------
# memory_write / memory_read — plain tools, direct invoke
# ---------------------------------------------------------------------------


def _ctx() -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore())


def test_write_tool_persists_memory_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tool = MemoryWriteTool(store=store)
    assert tool.name == MEMORY_WRITE_TOOL_NAME
    result = tool.invoke(
        {"name": "deploy", "text": "use make deploy"}, _ctx()
    )
    assert result.success
    assert (store.root / "deploy.md").read_text(encoding="utf-8") == (
        "use make deploy"
    )


def test_write_tool_rejects_missing_or_invalid_arguments(
    tmp_path: Path,
) -> None:
    tool = MemoryWriteTool(store=_store(tmp_path))
    assert not tool.invoke({"text": "no name"}, _ctx()).success
    assert not tool.invoke({"name": "n"}, _ctx()).success
    assert not tool.invoke({"name": "../up", "text": "x"}, _ctx()).success
    assert not tool.invoke({"name": "n", "text": 7}, _ctx()).success


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


def test_build_memory_tools_names_match_tool_attribute(
    tmp_path: Path,
) -> None:
    tools = build_memory_tools(_store(tmp_path))
    assert set(tools) == {MEMORY_WRITE_TOOL_NAME, MEMORY_READ_TOOL_NAME}
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
    # write memory = one file on disk
    assert (store.root / "deploy.md").read_text(encoding="utf-8") == (
        "deploy with make deploy"
    )
    # read full text = ordinary tool-result channel (ToolResultRecorded.output_ref is dereferenceable)
    recorded = [
        e for e in log.read(task.task_id) if e.type == "ToolResultRecorded"
    ]
    assert [e.payload.call_id for e in recorded] == ["c1", "c2"]
    assert all(e.payload.success for e in recorded)
    assert b"deploy with make deploy" in cs.get(recorded[1].payload.output_ref)
