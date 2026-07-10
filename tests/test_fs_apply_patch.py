"""M1 — `apply_patch` multi-file transactional edit (unit / direct invoke).

Pins the transaction semantics: validate-all-then-apply (no write on any
validation failure), TOCTOU revalidation + exclusive-create + rollback,
typed rollback-failure surface, conflict / case-collision / boundary
rejection, stale-hash guard, deterministic ordering, output inline
budget, no-orphan-artifact on validation failure, and the outer per-call
byte backstop (oversize args offload — no longer envelope-bound).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.fs import FsWriteMode, WorkspaceRoot
from noeta.tools.fs.patch import (
    MAX_PATCH_CANONICAL_BYTES,
    MAX_PATCH_EDITS,
    ApplyPatchTool,
)
from noeta.tools.fs.edit import _sha256


def _ws(tmp_path: Path, files: dict[str, str] | None = None) -> WorkspaceRoot:
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    for rel, content in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return WorkspaceRoot.from_path(root)


def _ctx() -> tuple[ToolContext, InMemoryContentStore]:
    cs = InMemoryContentStore()
    return ToolContext(artifact_store=cs), cs


def _tool(ws: WorkspaceRoot, *, mode: FsWriteMode = FsWriteMode.APPLY) -> ApplyPatchTool:
    return ApplyPatchTool(workspace=ws, mode=mode)


def _read(ws: WorkspaceRoot, rel: str) -> str:
    return (Path(ws.root) / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# happy path + dry-run
# ---------------------------------------------------------------------------


def test_happy_apply_replace_and_create(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "bar"},
                {"op": "create", "path": "b.py", "content": "new\n"},
            ]
        },
        ctx,
    )
    assert res.success is True and res.output["applied"] is True
    assert _read(ws, "a.py") == "bar\n"
    assert _read(ws, "b.py") == "new\n"
    assert len(res.artifacts) == 3  # 2 per-edit diffs + combined
    assert res.artifacts[-1].media_type == "text/x-diff"  # combined diff


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    res = _tool(ws, mode=FsWriteMode.DRY_RUN).invoke(
        {"edits": [{"op": "replace", "path": "a.py", "old": "foo", "new": "bar"}]},
        ctx,
    )
    assert res.success is True and res.output["applied"] is False
    assert _read(ws, "a.py") == "foo\n"  # unchanged
    assert len(res.artifacts) == 2  # per-edit diff + combined diff still produced
    assert all(a.media_type == "text/x-diff" for a in res.artifacts)


# ---------------------------------------------------------------------------
# validate-all-then-apply + no-orphan-artifact
# ---------------------------------------------------------------------------


def test_validation_failure_writes_no_file_no_artifact(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "bar"},
                {"op": "replace", "path": "missing.py", "old": "x", "new": "y"},
            ]
        },
        ctx,
    )
    assert res.success is False
    assert _read(ws, "a.py") == "foo\n"  # first edit NOT applied (atomic)
    assert len(cs._blobs) == 0  # no orphan diff blobs
    assert not res.artifacts


def test_no_orphan_artifacts_on_validation_failure(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, cs = _ctx()
    before = len(cs._blobs)
    _tool(ws).invoke(
        {"edits": [{"op": "replace", "path": "a.py", "old": "NOPE", "new": "y"}]},
        ctx,
    )
    assert len(cs._blobs) == before  # validation failure put nothing


# ---------------------------------------------------------------------------
# conflict / boundaries / stale hash
# ---------------------------------------------------------------------------


def test_same_file_multiple_replaces(tmp_path: Path) -> None:
    """Several replaces may target one file in a single call — they apply in
    array order and collapse into one per-file write."""
    ws = _ws(tmp_path, {"a.py": "foo bar\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "x"},
                {"op": "replace", "path": "a.py", "old": "bar", "new": "y"},
            ]
        },
        ctx,
    )
    assert res.success is True
    assert _read(ws, "a.py") == "x y\n"
    # One file ⇒ one result row, not one per edit.
    assert [r["path"] for r in res.output["edits"]] == ["a.py"]


def test_same_file_replaces_apply_in_order(tmp_path: Path) -> None:
    """Edit N's `old` is matched against the buffer as left by edits 0..N-1,
    so a later edit can target text an earlier edit just introduced."""
    ws = _ws(tmp_path, {"a.py": "alpha\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "alpha", "new": "beta"},
                {"op": "replace", "path": "a.py", "old": "beta", "new": "gamma"},
            ]
        },
        ctx,
    )
    assert res.success is True
    assert _read(ws, "a.py") == "gamma\n"


def test_same_file_replaces_atomic_on_later_failure(tmp_path: Path) -> None:
    """If a later edit in a same-file group fails validation, nothing is
    written — the whole batch is rejected before any disk write."""
    ws = _ws(tmp_path, {"a.py": "foo bar\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "x"},
                {"op": "replace", "path": "a.py", "old": "nope", "new": "y"},
            ]
        },
        ctx,
    )
    assert res.success is False
    assert _read(ws, "a.py") == "foo bar\n"  # untouched


def test_create_mixed_with_replace_same_path_rejected(tmp_path: Path) -> None:
    """A create writes the whole file, so it cannot share a path with other
    edits in the same call."""
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "x"},
                {"op": "create", "path": "a.py", "content": "z\n"},
            ]
        },
        ctx,
    )
    assert res.success is False
    assert _read(ws, "a.py") == "foo\n"


def test_case_collision_rejected(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "x"},
                {"op": "create", "path": "A.PY", "content": "z\n"},
            ]
        },
        ctx,
    )
    assert res.success is False  # a.py vs A.PY collide under casefold


def test_workspace_escape_rejected(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {"edits": [{"op": "create", "path": "../escape.py", "content": "x"}]}, ctx
    )
    assert res.success is False
    assert not (tmp_path / "escape.py").exists()


def test_create_existing_rejected(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {"edits": [{"op": "create", "path": "a.py", "content": "x"}]}, ctx
    )
    assert res.success is False
    assert _read(ws, "a.py") == "foo\n"


def test_stale_before_sha_rejected(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "bar",
                 "before_sha256": _sha256("WRONG")},
            ]
        },
        ctx,
    )
    assert res.success is False
    assert _read(ws, "a.py") == "foo\n"
    # the matching hash works:
    ok = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "bar",
                 "before_sha256": _sha256("foo\n")},
            ]
        },
        ctx,
    )
    assert ok.success is True and _read(ws, "a.py") == "bar\n"


def test_empty_before_sha256_treated_as_absent(tmp_path: Path) -> None:
    """An LLM habitually fills an optional string field with "" rather than
    omitting it; a blank before_sha256 must be treated as "no guard", not
    compared against the real hash (which would always spuriously mismatch)."""
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "bar",
                 "before_sha256": ""},
            ]
        },
        ctx,
    )
    assert res.success is True and _read(ws, "a.py") == "bar\n"


def test_over_edit_count_rejected(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "create", "path": f"f{i}.py", "content": "x"}
                for i in range(MAX_PATCH_EDITS + 1)
            ]
        },
        ctx,
    )
    assert res.success is False  # > MAX_PATCH_EDITS


# ---------------------------------------------------------------------------
# deterministic ordering
# ---------------------------------------------------------------------------


def test_deterministic_sorted_order(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "create", "path": "z.py", "content": "z\n"},
                {"op": "create", "path": "a.py", "content": "a\n"},
                {"op": "create", "path": "m.py", "content": "m\n"},
            ]
        },
        ctx,
    )
    assert res.success is True
    assert [e["path"] for e in res.output["edits"]] == ["a.py", "m.py", "z.py"]


# ---------------------------------------------------------------------------
# rollback (apply error) + rollback-failure surface + TOCTOU + exclusive create
# ---------------------------------------------------------------------------


def test_apply_error_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _ws(tmp_path, {"a.py": "aaa\n", "z.py": "zzz\n"})
    ctx, _cs = _ctx()
    real_write = Path.write_bytes

    def fail_on_z(self: Path, data: bytes) -> int:
        if self.name == "z.py" and data == b"ZZZ\n":  # the apply write (not rollback)
            raise OSError("disk full")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", fail_on_z)
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "aaa", "new": "AAA"},
                {"op": "replace", "path": "z.py", "old": "zzz", "new": "ZZZ"},
            ]
        },
        ctx,
    )
    assert res.success is False
    assert res.output["rolled_back"] is True
    # a.py was written then rolled back; z.py never changed → both original.
    assert _read(ws, "a.py") == "aaa\n"
    assert _read(ws, "z.py") == "zzz\n"


def test_rollback_failure_is_surfaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _ws(tmp_path, {"a.py": "aaa\n", "z.py": "zzz\n"})
    ctx, _cs = _ctx()
    real_write = Path.write_bytes

    def fail(self: Path, data: bytes) -> int:
        # fail the z.py apply write AND the a.py rollback-restore write.
        if self.name == "z.py" and data == b"ZZZ\n":
            raise OSError("disk full")
        if self.name == "a.py" and data == b"aaa\n":  # rollback restore
            raise OSError("rollback disk full")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", fail)
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "aaa", "new": "AAA"},
                {"op": "replace", "path": "z.py", "old": "zzz", "new": "ZZZ"},
            ]
        },
        ctx,
    )
    assert res.success is False
    assert res.output["rolled_back"] is False
    assert "a.py" in res.output["rollback_failed"]


def test_toctou_replace_revalidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    target = (Path(ws.root) / "a.py").resolve()
    real_read = Path.read_bytes
    seen: dict[str, int] = {}

    def changing_read(self: Path) -> bytes:
        data = real_read(self)
        if self.resolve() == target:
            n = seen.get("a", 0)
            seen["a"] = n + 1
            if n >= 1:  # the apply-phase re-read → pretend an external change
                return data + b"EXTERNAL\n"
        return data

    monkeypatch.setattr(Path, "read_bytes", changing_read)
    res = _tool(ws).invoke(
        {"edits": [{"op": "replace", "path": "a.py", "old": "foo", "new": "bar"}]},
        ctx,
    )
    assert res.success is False
    assert res.output["failed"]["phase"] == "apply"
    assert _read(ws, "a.py") == "foo\n"  # never clobbered


def test_current_replace_partial_write_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1: the FAILING target itself (not yet in `done`) must be
    recovered — a replace whose write truncates/corrupts then raises is
    restored to its pre-edit bytes."""
    ws = _ws(tmp_path, {"z.py": "zzz\n"})
    ctx, _cs = _ctx()
    real_write = Path.write_bytes

    def corrupt_then_raise(self: Path, data: bytes) -> int:
        if self.name == "z.py" and data == b"ZZZ\n":  # the apply write
            real_write(self, b"PARTIAL")  # corrupt the file on disk
            raise OSError("disk full mid-write")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", corrupt_then_raise)
    res = _tool(ws).invoke(
        {"edits": [{"op": "replace", "path": "z.py", "old": "zzz", "new": "ZZZ"}]},
        ctx,
    )
    assert res.success is False
    assert res.output["rolled_back"] is True
    assert _read(ws, "z.py") == "zzz\n"  # current target restored, not "PARTIAL"


def test_current_create_write_error_deletes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1: a create whose O_EXCL open SUCCEEDS but whose write fails must
    delete the now-existing file (it is not in `done`)."""
    ws = _ws(tmp_path, {})

    def boom_write(fd: int, data: Any) -> int:
        raise OSError("disk full")

    ctx, _cs = _ctx()
    monkeypatch.setattr("os.write", boom_write)
    res = _tool(ws).invoke(
        {"edits": [{"op": "create", "path": "b.py", "content": "x\n"}]}, ctx
    )
    assert res.success is False
    assert res.output["rolled_back"] is True
    assert not (Path(ws.root) / "b.py").exists()  # created file removed


def test_os_write_short_return_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1: a partial `os.write` (returns 0) is NOT treated as success —
    the create fails and the file is deleted."""
    ws = _ws(tmp_path, {})

    def short_write(fd: int, data: Any) -> int:
        return 0  # no progress

    ctx, _cs = _ctx()
    monkeypatch.setattr("os.write", short_write)
    res = _tool(ws).invoke(
        {"edits": [{"op": "create", "path": "b.py", "content": "x\n"}]}, ctx
    )
    assert res.success is False
    assert not (Path(ws.root) / "b.py").exists()


def test_create_close_failure_deletes_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1: a create whose write succeeds but whose `os.close` raises (a
    deferred write-back error) must NOT escape invoke — it deletes the
    new file, rolls back already-applied edits, and returns typed."""
    ws = _ws(tmp_path, {"a.py": "aaa\n"})
    ctx, _cs = _ctx()

    def boom_close(fd: int) -> None:
        raise OSError("close: deferred write-back failed")

    monkeypatch.setattr("os.close", boom_close)
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "aaa", "new": "AAA"},
                {"op": "create", "path": "b.py", "content": "new\n"},
            ]
        },
        ctx,
    )
    assert res.success is False  # did NOT escape invoke
    assert res.output["failed"]["reason"].startswith("close failed")
    assert res.output["rolled_back"] is True
    assert not (Path(ws.root) / "b.py").exists()  # current create removed
    assert _read(ws, "a.py") == "aaa\n"  # earlier replace rolled back


def test_before_sha256_overlong_rejected(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "bar",
                 "before_sha256": "x" * 100},
            ]
        },
        ctx,
    )
    assert res.success is False
    assert _read(ws, "a.py") == "foo\n"


def test_normalization_collision_rejected(tmp_path: Path) -> None:
    """P1: NFC-equal paths (precomposed U+00E9 vs e + U+0301 combining
    acute) collide and are rejected — not only case collisions."""
    ws = _ws(tmp_path, {})
    ctx, _cs = _ctx()
    precomposed = "\u00e9.py"      # 'é' as one code point
    decomposed = "e\u0301.py"      # 'e' + combining acute
    assert precomposed != decomposed  # distinct byte sequences
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "create", "path": precomposed, "content": "a\n"},
                {"op": "create", "path": decomposed, "content": "b\n"},
            ]
        },
        ctx,
    )
    assert res.success is False
    assert not list(Path(ws.root).glob("*.py"))  # nothing created


def test_exclusive_create_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _ws(tmp_path, {})
    ctx, _cs = _ctx()

    def boom_open(*a: Any, **k: Any) -> int:
        raise FileExistsError("created concurrently")

    # patch.py does `import os; os.open(...)` → patch the global.
    monkeypatch.setattr("os.open", boom_open)
    res = _tool(ws).invoke(
        {"edits": [{"op": "create", "path": "b.py", "content": "x\n"}]}, ctx
    )
    assert res.success is False
    assert res.output["failed"]["phase"] == "apply"
    assert not (Path(ws.root) / "b.py").exists()


# ---------------------------------------------------------------------------
# byte caps + envelope fit (gate 9)
# ---------------------------------------------------------------------------


def test_over_canonical_byte_budget_rejected(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {})
    ctx, _cs = _ctx()
    # Each create is < the 64 KB per-field cap and the count is < the edit
    # cap, but together they blow the outer per-call canonical-byte backstop
    # → the tool tells the caller to split. (Offload carries normal oversize
    # args; an absurd multi-hundred-KB single call is still refused.)
    per = 60_000
    n = MAX_PATCH_CANONICAL_BYTES // per + 2
    assert n <= MAX_PATCH_EDITS  # the byte backstop fires, not the edit-count cap
    big = "x" * per
    res = _tool(ws).invoke(
        {"edits": [{"op": "create", "path": f"f{i}.py", "content": big} for i in range(n)]},
        ctx,
    )
    assert res.success is False
    assert "too large" in res.summary


def test_oversize_patch_accepted_not_envelope_bound(tmp_path: Path) -> None:
    """Post-offload (`noeta.protocols.tool_args`) the tool is no longer capped
    to the 4 KB event envelope: a create whose content far exceeds the old
    512-byte / 3072-byte caps is accepted — the runtime offloads oversize
    args via `arguments_ref` rather than rejecting them."""
    ws = _ws(tmp_path, {})
    ctx, _cs = _ctx()
    big = "x" * 8000  # >> the old _CONTENT_MAX_BYTES (512) and budget (3072)
    res = _tool(ws).invoke(
        {"edits": [{"op": "create", "path": "big.py", "content": big}]},
        ctx,
    )
    assert res.success is True
    assert _read(ws, "big.py") == big


def test_output_within_inline_budget(tmp_path: Path) -> None:
    from noeta.tools._limits import INLINE_OUTPUT_MAX_BYTES

    ws = _ws(tmp_path, {f"f{i}.py": "foo\n" for i in range(4)})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": f"f{i}.py", "old": "foo", "new": "barbar"}
                for i in range(4)
            ]
        },
        ctx,
    )
    assert res.success is True
    assert len(to_canonical_bytes(res.output)) <= INLINE_OUTPUT_MAX_BYTES


# ---------------------------------------------------------------------------
# file_changes — rewind-checkpoint surface (mirrors edit/write's contract)
# ---------------------------------------------------------------------------


def test_apply_surfaces_file_changes_for_replace_and_create(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    res = _tool(ws).invoke(
        {
            "edits": [
                {"op": "replace", "path": "a.py", "old": "foo", "new": "bar"},
                {"op": "create", "path": "b.py", "content": "new\n"},
            ]
        },
        ctx,
    )
    assert res.success is True
    # deterministic sort by resolved POSIX path — a.py before b.py.
    assert res.file_changes == [
        {"path": "a.py", "before": b"foo\n"},
        {"path": "b.py", "before": None},
    ]


def test_dry_run_surfaces_no_file_changes(tmp_path: Path) -> None:
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    res = _tool(ws, mode=FsWriteMode.DRY_RUN).invoke(
        {"edits": [{"op": "replace", "path": "a.py", "old": "foo", "new": "bar"}]},
        ctx,
    )
    assert res.success is True and res.output["applied"] is False
    assert res.file_changes is None


def test_failed_apply_surfaces_no_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An apply-phase failure never reaches ``_result`` (it returns the typed
    # failure from ``_fail`` instead), so a rolled-back batch must not surface
    # ``file_changes`` for the checkpoint gate to (wrongly) stash.
    ws = _ws(tmp_path, {"a.py": "foo\n"})
    ctx, _cs = _ctx()
    from noeta.tools.fs.exec_env import LocalExecEnv

    def _boom(self: LocalExecEnv, path: Path, data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(LocalExecEnv, "write_bytes", _boom)
    res = _tool(ws).invoke(
        {"edits": [{"op": "replace", "path": "a.py", "old": "foo", "new": "bar"}]},
        ctx,
    )
    assert res.success is False
    assert res.file_changes is None
