"""AI-edit file checkpoint: capture + restore.

Three pieces stand up the file half of rewind:

* the per-turn **gate** (``FileCheckpointRegistry``) — first-edit-per-turn
  test-and-set, cleared at each turn boundary (D6);
* **capture** (``ToolRuntime._capture_file_baselines``) — turns a write-side
  tool's ``file_changes`` into recorded ``FileBaseline``s, deduped by the gate,
  with ``content_ref=None`` for AI-created files (D4/D5/D6);
* **restore** (``InteractionDriver._restore_files``) — the live-only fs
  side-effect of a rewind: writes each dead-tail file's EARLIEST baseline back,
  deleting AI-created files (D5/D6).

Coverage also pins the two boundaries D4 draws: a tool that surfaces no
``file_changes`` (the shell, or any non-fs tool) is never tracked, and a
``ToolRuntime`` with no gate (the replay / pre-0043 construction) captures
nothing — byte-identical to before the feature.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from noeta.execution.driver import InteractionDriver
from noeta.protocols.events import FileBaseline
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.file_checkpoint import FileCheckpointRegistry
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.tools.fs import FsWriteMode, ReplaceTextTool, WorkspaceRoot


# ---------------------------------------------------------------------------
# Gate — first-edit-per-turn test-and-set, reset at the turn boundary (D6)
# ---------------------------------------------------------------------------


def test_gate_marks_only_first_edit_per_turn() -> None:
    gate = FileCheckpointRegistry()
    assert gate.mark_if_first("root", "a.py") is True   # first edit → stash
    assert gate.mark_if_first("root", "a.py") is False  # repeat → no-op
    assert gate.mark_if_first("root", "b.py") is True    # other file → stash


def test_gate_reset_turn_restashes_and_roots_are_independent() -> None:
    gate = FileCheckpointRegistry()
    gate.mark_if_first("root", "a.py")
    # A different session root has its own set — never deduped against "root".
    assert gate.mark_if_first("other", "a.py") is True
    gate.reset_turn("root")
    # New turn re-stashes a fresh baseline for the same path (cleared each turn).
    assert gate.mark_if_first("root", "a.py") is True
    gate.reset_turn("unknown")  # idempotent on an unknown root


# ---------------------------------------------------------------------------
# Capture — file_changes → recorded FileBaselines, deduped by the gate
# ---------------------------------------------------------------------------


def _runtime(registry: object | None) -> tuple[ToolRuntime, InMemoryContentStore]:
    store = InMemoryContentStore()
    runtime = ToolRuntime(
        event_log=InMemoryEventLog(),
        content_store=store,
        file_checkpoint_registry=registry,
    )
    return runtime, store


def _result(file_changes: object) -> ToolResult:
    return ToolResult(success=True, output={}, file_changes=file_changes)


def test_capture_records_pre_edit_baseline_on_first_edit() -> None:
    runtime, store = _runtime(FileCheckpointRegistry())
    baselines = runtime._capture_file_baselines(
        "root", _result([{"path": "a.py", "before": b"OLD"}])
    )
    assert baselines is not None and len(baselines) == 1
    bl = baselines[0]
    assert bl.path == "a.py"
    assert bl.content_ref is not None
    assert store.get(bl.content_ref) == b"OLD"  # the PRE-edit bytes, dedup'd


def test_capture_dedups_repeat_edit_in_same_turn() -> None:
    runtime, _ = _runtime(FileCheckpointRegistry())
    first = runtime._capture_file_baselines(
        "root", _result([{"path": "a.py", "before": b"V1"}])
    )
    assert first is not None
    # Second edit of the SAME path this turn pins nothing new (D6: stash on first touch).
    again = runtime._capture_file_baselines(
        "root", _result([{"path": "a.py", "before": b"V2"}])
    )
    assert again is None


def test_capture_created_file_has_no_content_ref() -> None:
    runtime, _ = _runtime(FileCheckpointRegistry())
    baselines = runtime._capture_file_baselines(
        "root", _result([{"path": "new.py", "before": None}])
    )
    assert baselines == [FileBaseline(path="new.py", content_ref=None)]


def test_capture_is_noop_without_a_gate() -> None:
    # The replay / pre-0043 construction injects no registry → no baselines,
    # byte-identical to a pre-0043 recording (replay-safety).
    runtime, _ = _runtime(None)
    assert runtime._capture_file_baselines(
        "root", _result([{"path": "a.py", "before": b"X"}])
    ) is None


def test_capture_ignores_tools_with_no_file_changes() -> None:
    # The shell (and every non-fs tool) surfaces no ``file_changes`` → D4 leaves
    # shell-driven edits untracked.
    runtime, _ = _runtime(FileCheckpointRegistry())
    assert runtime._capture_file_baselines("root", _result(None)) is None


# ---------------------------------------------------------------------------
# The real ``edit`` tool surfaces the PRE-edit bytes (capture's input)
# ---------------------------------------------------------------------------


def test_edit_tool_surfaces_pre_edit_bytes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("hello world", encoding="utf-8")
    store = InMemoryContentStore()
    ctx = ToolContext(artifact_store=store)
    tool = ReplaceTextTool(
        workspace=WorkspaceRoot.from_path(ws), mode=FsWriteMode.APPLY
    )
    result = tool.invoke({"path": "f.py", "old": "hello", "new": "bye"}, ctx)
    assert result.success
    assert result.file_changes == [{"path": "f.py", "before": b"hello world"}]
    # DRY_RUN never surfaces a baseline (no write, nothing to undo).
    dry = ReplaceTextTool(
        workspace=WorkspaceRoot.from_path(ws), mode=FsWriteMode.DRY_RUN
    )
    assert dry.invoke(
        {"path": "f.py", "old": "bye", "new": "hi"}, ctx
    ).file_changes is None


# ---------------------------------------------------------------------------
# Restore — the live-only fs half of a rewind (D5/D6)
# ---------------------------------------------------------------------------


def _env(seq: int, baselines: list[FileBaseline]) -> SimpleNamespace:
    return SimpleNamespace(
        seq=seq,
        type="ToolResultRecorded",
        payload=SimpleNamespace(file_baselines=baselines),
    )


def test_restore_reverts_modified_deletes_created_leaves_untouched(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "mod.py").write_text("NEW", encoding="utf-8")       # AI-modified
    (ws / "created.py").write_text("MADE", encoding="utf-8")  # AI-created
    (ws / "keep.py").write_text("KEEP", encoding="utf-8")     # untouched

    store = InMemoryContentStore()
    old_ref = store.put(b"OLD", media_type="text/plain")
    wrong_ref = store.put(b"WRONG", media_type="text/plain")

    events = [
        # seq <= keep_through (5): the surviving conversation — NOT restored.
        _env(3, [FileBaseline(path="keep.py", content_ref=wrong_ref)]),
        # The rewound tail: mod.py's EARLIEST baseline (seq 10) is its pre-turn
        # state; a later baseline (seq 12) must NOT win.
        _env(10, [
            FileBaseline(path="mod.py", content_ref=old_ref),
            FileBaseline(path="created.py", content_ref=None),
        ]),
        _env(12, [FileBaseline(path="mod.py", content_ref=wrong_ref)]),
    ]
    host = SimpleNamespace(
        content_store=store, workspace_dir_for=lambda _ws: ws
    )
    task = SimpleNamespace(governance=SimpleNamespace(workspace=str(ws)))

    InteractionDriver._restore_files(host, events, keep_through=5, baseline_task=task)

    assert (ws / "mod.py").read_text(encoding="utf-8") == "OLD"   # earliest wins
    assert not (ws / "created.py").exists()                        # created → deleted
    assert (ws / "keep.py").read_text(encoding="utf-8") == "KEEP"  # seq<=keep, untouched


def test_restore_is_a_noop_when_the_dead_tail_changed_no_files(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("AS-IS", encoding="utf-8")
    host = SimpleNamespace(
        content_store=InMemoryContentStore(), workspace_dir_for=lambda _ws: ws
    )
    task = SimpleNamespace(governance=SimpleNamespace(workspace=str(ws)))
    # Only a kept-seq baseline + a non-tool event → nothing to restore.
    events = [
        _env(2, [FileBaseline(path="f.py", content_ref=None)]),
        SimpleNamespace(seq=9, type="MessagesAppended", payload=SimpleNamespace()),
    ]
    InteractionDriver._restore_files(host, events, keep_through=5, baseline_task=task)
    assert (ws / "f.py").read_text(encoding="utf-8") == "AS-IS"


# ---------------------------------------------------------------------------
# D7 — single-file size cap + binary detection (issue 03)
# ---------------------------------------------------------------------------


def test_capture_skips_oversize_file() -> None:
    # A pre-edit blob over the cap is not checkpointed (rewind cannot cover it,
    # matching Claude Code) — no baseline, no error.
    from noeta.runtime.tool import _BASELINE_MAX_BYTES

    runtime, _ = _runtime(FileCheckpointRegistry())
    big = b"x" * (_BASELINE_MAX_BYTES + 1)
    assert runtime._capture_file_baselines(
        "root", _result([{"path": "big.bin", "before": big}])
    ) is None


def test_capture_skips_binary_file() -> None:
    # A NUL byte marks the pre-edit content binary → skipped, no error.
    runtime, _ = _runtime(FileCheckpointRegistry())
    assert runtime._capture_file_baselines(
        "root", _result([{"path": "b.bin", "before": b"AB\x00CD"}])
    ) is None


def test_capture_oversize_skip_does_not_block_other_files() -> None:
    # One oversize file is dropped; a normal file in the SAME call still pins.
    from noeta.runtime.tool import _BASELINE_MAX_BYTES

    runtime, store = _runtime(FileCheckpointRegistry())
    big = b"x" * (_BASELINE_MAX_BYTES + 1)
    out = runtime._capture_file_baselines(
        "root",
        _result([
            {"path": "big.bin", "before": big},
            {"path": "small.py", "before": b"OK"},
        ]),
    )
    assert out is not None and [b.path for b in out] == ["small.py"]
    assert store.get(out[0].content_ref) == b"OK"


def test_capture_created_file_is_exempt_from_size_and_binary_check() -> None:
    # ``before is None`` (AI-created) has no pre-edit bytes to weigh → always a
    # "did-not-exist" marker regardless of the file's eventual size/kind.
    runtime, _ = _runtime(FileCheckpointRegistry())
    assert runtime._capture_file_baselines(
        "root", _result([{"path": "new.bin", "before": None}])
    ) == [FileBaseline(path="new.bin", content_ref=None)]


# ---------------------------------------------------------------------------
# D8 — subtask cascade: session-root keying (capture) + descendant enumeration
# (restore)
# ---------------------------------------------------------------------------


def _log_with_parents(parents: dict[str, str | None]) -> SimpleNamespace:
    """A minimal read-only event_log: each task's stream is a lone TaskCreated
    carrying its ``parent_task_id`` (what ``_session_root`` walks)."""
    streams = {
        tid: [
            SimpleNamespace(
                seq=1,
                type="TaskCreated",
                payload=SimpleNamespace(parent_task_id=parent),
            )
        ]
        for tid, parent in parents.items()
    }
    return SimpleNamespace(read=lambda tid: streams.get(tid, []))


def test_session_root_walks_parent_chain() -> None:
    log = _log_with_parents({"root": None, "child": "root", "grand": "child"})
    store = InMemoryContentStore()
    runtime = ToolRuntime(
        event_log=log,
        content_store=store,
        file_checkpoint_registry=FileCheckpointRegistry(),
    )
    assert runtime._session_root("grand") == "root"  # walks all the way up
    assert runtime._session_root("child") == "root"
    assert runtime._session_root("root") == "root"   # a root resolves to itself


def test_capture_shares_one_gate_across_a_delegation_tree() -> None:
    # D8 — the parent stashes X's pre-turn baseline; a subtask that edits the
    # SAME X this turn must NOT stash a SECOND (mid-turn, dirty) baseline.
    log = _log_with_parents({"root": None, "child": "root"})
    store = InMemoryContentStore()
    runtime = ToolRuntime(
        event_log=log,
        content_store=store,
        file_checkpoint_registry=FileCheckpointRegistry(),
    )
    parent = runtime._capture_file_baselines(
        "root", _result([{"path": "x.py", "before": b"ORIG"}])
    )
    assert parent is not None and store.get(parent[0].content_ref) == b"ORIG"
    child = runtime._capture_file_baselines(
        "child", _result([{"path": "x.py", "before": b"DIRTY"}])
    )
    assert child is None  # shared gate suppressed the second baseline


def _spawn(seq: int, subtask_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        seq=seq,
        type="SubtaskSpawned",
        payload=SimpleNamespace(subtask_id=subtask_id),
    )


def test_restore_cascades_into_subtask_streams(tmp_path: Path) -> None:
    # D8 — a rewind reverts files the parent AND its descendant subtasks edited
    # in the rewound span (they share one workspace), and deletes files a
    # subtask created.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "parent.py").write_text("NEW-P", encoding="utf-8")
    (ws / "child.py").write_text("NEW-C", encoding="utf-8")
    (ws / "made.py").write_text("MADE", encoding="utf-8")  # subtask-created

    store = InMemoryContentStore()
    p_ref = store.put(b"ORIG-P", media_type="text/plain")
    c_ref = store.put(b"ORIG-C", media_type="text/plain")
    child_stream = [
        _env(2, [FileBaseline(path="child.py", content_ref=c_ref)]),
        _env(4, [FileBaseline(path="made.py", content_ref=None)]),
    ]
    parent_events = [
        _env(10, [FileBaseline(path="parent.py", content_ref=p_ref)]),
        _spawn(11, "child"),
    ]
    host = SimpleNamespace(
        content_store=store,
        workspace_dir_for=lambda _ws: ws,
        event_log=SimpleNamespace(
            read=lambda tid: child_stream if tid == "child" else []
        ),
    )
    task = SimpleNamespace(governance=SimpleNamespace(workspace=str(ws)))

    InteractionDriver._restore_files(
        host, parent_events, keep_through=5, baseline_task=task
    )

    assert (ws / "parent.py").read_text(encoding="utf-8") == "ORIG-P"
    assert (ws / "child.py").read_text(encoding="utf-8") == "ORIG-C"  # cascade
    assert not (ws / "made.py").exists()  # subtask-created → deleted


def test_restore_cascade_keeps_earliest_parent_baseline_for_shared_file(
    tmp_path: Path,
) -> None:
    # Defensive: even if both streams carried a baseline for the same path, the
    # parent's (seq-earlier, walked before the spawn) wins over a child's.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "shared.py").write_text("DIRTY", encoding="utf-8")
    store = InMemoryContentStore()
    orig = store.put(b"PARENT-ORIG", media_type="text/plain")
    dirty = store.put(b"CHILD-DIRTY", media_type="text/plain")
    child_stream = [_env(2, [FileBaseline(path="shared.py", content_ref=dirty)])]
    parent_events = [
        _env(10, [FileBaseline(path="shared.py", content_ref=orig)]),
        _spawn(11, "child"),
    ]
    host = SimpleNamespace(
        content_store=store,
        workspace_dir_for=lambda _ws: ws,
        event_log=SimpleNamespace(
            read=lambda tid: child_stream if tid == "child" else []
        ),
    )
    task = SimpleNamespace(governance=SimpleNamespace(workspace=str(ws)))
    InteractionDriver._restore_files(
        host, parent_events, keep_through=5, baseline_task=task
    )
    assert (ws / "shared.py").read_text(encoding="utf-8") == "PARENT-ORIG"


def test_restore_recurses_into_nested_grandchildren(tmp_path: Path) -> None:
    # The enumeration recurses: a child's own SubtaskSpawned pulls a grandchild's
    # baselines into the same rewound turn.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "g.py").write_text("NEW-G", encoding="utf-8")
    store = InMemoryContentStore()
    g_ref = store.put(b"ORIG-G", media_type="text/plain")
    grand_stream = [_env(2, [FileBaseline(path="g.py", content_ref=g_ref)])]
    child_stream = [_spawn(3, "grand")]
    parent_events = [_spawn(11, "child")]
    streams = {"child": child_stream, "grand": grand_stream}
    host = SimpleNamespace(
        content_store=store,
        workspace_dir_for=lambda _ws: ws,
        event_log=SimpleNamespace(read=lambda tid: streams.get(tid, [])),
    )
    task = SimpleNamespace(governance=SimpleNamespace(workspace=str(ws)))
    InteractionDriver._restore_files(
        host, parent_events, keep_through=5, baseline_task=task
    )
    assert (ws / "g.py").read_text(encoding="utf-8") == "ORIG-G"
