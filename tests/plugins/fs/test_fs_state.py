"""The ``fs`` reducer, and rebuilding the read-first record from recorded
entries (what a fresh process does from the task log)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import msgspec

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.plugins.fs import EditTool, ReadTool
from noeta.plugins.fs.state import (
    FS,
    KIND_CHANGED,
    KIND_READ,
    MAX_FILES,
    FileRead,
    FsState,
    current_fs_state,
    reduce_fs,
)
from tests.plugins.conftest import Harness, text_of


def test_reduce_from_nothing() -> None:
    state = reduce_fs(None, KIND_READ, {"path": "/a", "digest": "d1", "mtime": 1.5})
    assert state.reads == {"/a": FileRead(digest="d1", mtime=1.5)}
    assert state.digest("/a") == "d1"
    assert state.digest("/b") is None


def test_reduce_never_mutates_its_input() -> None:
    first = reduce_fs(None, KIND_READ, {"path": "/a", "digest": "d1"})
    second = reduce_fs(first, KIND_READ, {"path": "/b", "digest": "d2"})
    assert first.reads == {"/a": FileRead(digest="d1")}
    assert set(second.reads) == {"/a", "/b"}


def test_a_later_read_replaces_the_digest() -> None:
    state = reduce_fs(None, KIND_READ, {"path": "/a", "digest": "old"})
    state = reduce_fs(state, KIND_READ, {"path": "/a", "digest": "new", "mtime": 2})
    assert state.reads["/a"] == FileRead(digest="new", mtime=2.0)


def test_only_the_most_recent_files_are_remembered() -> None:
    state = None
    for i in range(MAX_FILES + 5):
        state = reduce_fs(state, KIND_READ, {"path": f"/f{i}", "digest": "d"})
        state = reduce_fs(state, KIND_CHANGED, {"path": f"/f{i}", "digest": "e"})
    # A re-read moves a file to the newest end.
    state = reduce_fs(state, KIND_READ, {"path": "/f6", "digest": "d"})
    state = reduce_fs(state, KIND_READ, {"path": "/new", "digest": "d"})
    assert state is not None
    assert len(state.reads) == MAX_FILES and len(state.noticed) <= MAX_FILES
    assert "/f0" not in state.reads and "/f5" not in state.reads
    assert "/f6" in state.reads and "/new" in state.reads
    assert f"/f{MAX_FILES + 4}" in state.reads


def test_unknown_kind_leaves_the_state_unchanged() -> None:
    state = reduce_fs(None, KIND_READ, {"path": "/a", "digest": "d"})
    assert reduce_fs(state, "something_else", {"path": "/b", "digest": "x"}) is state
    assert reduce_fs(None, "something_else", {}) == FsState()


def test_malformed_data_leaves_the_state_unchanged() -> None:
    state = reduce_fs(None, KIND_READ, {"path": "/a", "digest": "d"})
    for kind, data in [
        (KIND_READ, None),
        (KIND_READ, "not a mapping"),
        (KIND_READ, {"path": "", "digest": "d"}),
        (KIND_READ, {"path": 5, "digest": "d"}),
        (KIND_READ, {"path": "/b"}),
        (KIND_READ, {"path": "/b", "digest": 7}),
    ]:
        assert reduce_fs(state, kind, data) is state


def test_a_bad_mtime_is_zero() -> None:
    for bad in ("x", True, None):
        state = reduce_fs(None, KIND_READ, {"path": "/a", "digest": "d", "mtime": bad})
        assert state.reads["/a"].mtime == 0.0


def test_state_round_trips_as_json() -> None:
    state = reduce_fs(None, KIND_READ, {"path": "/a", "digest": "d", "mtime": 3.0})
    assert msgspec.json.decode(msgspec.json.encode(state), type=FsState) == state


def test_current_fs_state_accepts_struct_mapping_or_nothing() -> None:
    state = reduce_fs(None, KIND_READ, {"path": "/a", "digest": "d"})
    assert current_fs_state({FS: state}) is state
    assert current_fs_state({}) == FsState()
    as_dict: dict[str, Any] = msgspec.to_builtins(state)
    assert current_fs_state({FS: as_dict}) == state
    assert current_fs_state({FS: {"reads": "garbage"}}) == FsState()
    assert current_fs_state({FS: 42}) == FsState()


# -- rebuilding from recorded entries (a "restart") ---------------------------


def _fresh(h: Harness) -> Harness:
    """A new process: an empty context whose state is folded from the old
    one's records, as the runner folds them from the log."""
    state: FsState | None = None
    for ext, kind, data in h.records:
        if ext == FS:
            state = reduce_fs(state, kind, data)
    fresh = Harness(env=LocalExecEnv(h.env.cwd), approved=h.approved)
    if state is not None:
        fresh.state[FS] = state
    return fresh


def _setup(ws: Path) -> None:
    (ws / "a.py").write_text("x = 1\ny = 2\n")


async def test_a_recorded_read_lets_edit_run_after_a_restart(h: Harness, ws: Path) -> None:
    _setup(ws)
    assert not (await h.call(ReadTool(), file_path="a.py")).is_error
    out = await _fresh(h).call(EditTool(), file_path="a.py", old_string="x = 1", new_string="x = 10")
    assert not out.is_error, text_of(out)
    assert (ws / "a.py").read_text() == "x = 10\ny = 2\n"


async def test_an_edit_vouches_for_the_next_one_after_a_restart(h: Harness, ws: Path) -> None:
    _setup(ws)
    await h.call(ReadTool(), file_path="a.py")
    assert not (
        await h.call(EditTool(), file_path="a.py", old_string="x = 1", new_string="x = 10")
    ).is_error
    out = await _fresh(h).call(EditTool(), file_path="a.py", old_string="y = 2", new_string="y = 20")
    assert not out.is_error, text_of(out)


async def test_a_change_since_the_recorded_read_needs_a_read(h: Harness, ws: Path) -> None:
    h.approved = True
    _setup(ws)
    await h.call(ReadTool(), file_path="a.py")
    (ws / "a.py").write_text("x = 1\ny = 2\nz = 3\n")
    out = await _fresh(h).call(EditTool(), file_path="a.py", old_string="x = 1", new_string="x = 10")
    assert out.is_error
    assert "modified since it was read" in text_of(out)


async def test_never_read_is_refused(h: Harness, ws: Path) -> None:
    h.approved = True
    _setup(ws)
    out = await h.call(EditTool(), file_path="a.py", old_string="x = 1", new_string="x = 10")
    assert "this conversation has not read" in text_of(out)


async def test_dropped_records_vouch_for_nothing(h: Harness, ws: Path) -> None:
    # Entries the runner does not fold (a sealed / rewound stretch) leave
    # nothing behind.
    h.approved = True
    _setup(ws)
    await h.call(ReadTool(), file_path="a.py")
    h.records.clear()
    out = await _fresh(h).call(EditTool(), file_path="a.py", old_string="x = 1", new_string="x = 10")
    assert "this conversation has not read" in text_of(out)


async def test_the_read_is_recorded_under_the_canonical_path(h: Harness, ws: Path) -> None:
    _setup(ws)
    await h.call(ReadTool(), file_path="./sub/../a.py")
    ((ext, kind, data),) = h.records
    assert (ext, kind) == (FS, KIND_READ)
    assert data["path"] == str(ws / "a.py")
    assert len(data["digest"]) == 64
    assert data["mtime"] == (ws / "a.py").stat().st_mtime
