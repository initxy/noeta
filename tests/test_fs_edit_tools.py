"""``edit`` / ``write`` — the file-mutation primitives.

These contracts exist to stop a model from destroying work it never saw:
``edit`` replaces a unique match (or every match under ``replace_all``) and
refuses an ambiguous one, ``write`` may create freely but must have read an
existing file before overwriting it, and both default to dry-run so a caller who
forgets ``FsWriteMode.APPLY`` gets a diff artifact instead of a mutated disk.
Writes outside the workspace need a per-task authorization grant and fail closed
when the resolver misbehaves.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.tool import _encode_output
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.limits import INLINE_OUTPUT_MAX_BYTES
from noeta.builtins.fs.impl import (
    WRITE_FILE_MAX_BYTES,
    ReadFileTool,
    ReplaceTextTool,
    WriteFileTool,
    build_fs_tools,
)
from noeta.runtime.workspace import FsWriteMode, WorkspaceRoot


def _ctx_and_workspace(
    tmp_path: Path,
) -> tuple[ToolContext, WorkspaceRoot, InMemoryContentStore]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = InMemoryContentStore()
    ctx = ToolContext(artifact_store=store)
    return ctx, WorkspaceRoot.from_path(workspace), store


def _assert_output_json_safe(result: ToolResult) -> None:
    """ToolResult.output must survive stdlib json.dumps."""
    _encode_output(result.output)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# build_fs_tools — extended pack
# ---------------------------------------------------------------------------


def test_build_fs_tools_includes_edit_tools(tmp_path: Path) -> None:
    _, workspace, _ = _ctx_and_workspace(tmp_path)
    tools = build_fs_tools(workspace, mode=FsWriteMode.DRY_RUN)
    # The pack also carries shell / git tools, so this is a subset check.
    assert {
        "Read",
        "Glob",
        "Grep",
        "edit",
        "write",
    } <= set(tools.keys())
    # Provider-safe names everywhere: letters/digits/underscore, no dots.
    import re as _re

    for name in tools.keys():
        assert _re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name)


def test_build_fs_tools_default_mode_is_dry_run(tmp_path: Path) -> None:
    _, workspace, _ = _ctx_and_workspace(tmp_path)
    tools = build_fs_tools(workspace)
    # The default closure is the safe one — a daemon that forgets the
    # flag emits diff artifacts but does not write.
    assert tools["edit"].mode is FsWriteMode.DRY_RUN  # type: ignore[attr-defined]
    assert tools["write"].mode is FsWriteMode.DRY_RUN  # type: ignore[attr-defined]


def test_edit_tools_are_high_risk(tmp_path: Path) -> None:
    """Write-side fs tools are high-risk so PermissionGuard treats them as
    privileged. A policy permitting medium-risk tools must NOT accidentally
    enable file mutation."""
    _, workspace, _ = _ctx_and_workspace(tmp_path)
    tools = build_fs_tools(workspace)
    assert tools["edit"].risk_level == "high"
    assert tools["write"].risk_level == "high"
    # And the direct dataclass defaults agree (no caller can downgrade
    # them by omitting the kwarg).
    assert ReplaceTextTool(workspace=workspace).risk_level == "high"
    assert WriteFileTool(workspace=workspace).risk_level == "high"


# ---------------------------------------------------------------------------
# edit — unique match + dry-run
# ---------------------------------------------------------------------------


def test_edit_apply_writes_on_unique_match(tmp_path: Path) -> None:
    ctx, workspace, store = _ctx_and_workspace(tmp_path)
    target = workspace.root / "a.py"
    target.write_text("def foo():\n    return 1\n")
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke(
        {"path": "a.py", "old": "return 1", "new": "return 2"}, ctx
    )
    assert result.success is True
    _assert_output_json_safe(result)
    assert result.output["applied"] is True
    assert result.output["path"] == "a.py"
    assert result.output["before_sha256"] == _sha256("def foo():\n    return 1\n")
    assert result.output["after_sha256"] == _sha256("def foo():\n    return 2\n")
    assert result.output["added"] == 1
    assert result.output["removed"] == 1
    assert target.read_text() == "def foo():\n    return 2\n"
    # The full diff is the artifact, not inline.
    assert len(result.artifacts) == 1
    diff_bytes = store.get(result.artifacts[0])
    diff = diff_bytes.decode("utf-8")
    assert "-    return 1" in diff
    assert "+    return 2" in diff
    assert diff.startswith("--- a/a.py")


def test_edit_dry_run_does_not_write(tmp_path: Path) -> None:
    ctx, workspace, store = _ctx_and_workspace(tmp_path)
    target = workspace.root / "a.py"
    target.write_text("hello\n")
    original_bytes = target.read_bytes()
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.DRY_RUN)
    result = tool.invoke({"path": "a.py", "old": "hello", "new": "world"}, ctx)
    assert result.success is True
    assert result.output["applied"] is False
    # File on disk is byte-identical.
    assert target.read_bytes() == original_bytes
    # But the proposed-diff artifact IS produced.
    assert len(result.artifacts) == 1
    diff = store.get(result.artifacts[0]).decode("utf-8")
    assert "-hello" in diff and "+world" in diff


def test_edit_zero_match_fails_no_write(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    target = workspace.root / "a.py"
    target.write_text("foo\n")
    original = target.read_bytes()
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "a.py", "old": "missing", "new": "x"}, ctx)
    assert result.success is False
    assert "not found" in result.summary
    assert target.read_bytes() == original


def test_edit_multi_match_fails_no_write(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    target = workspace.root / "a.py"
    target.write_text("foo\nfoo\nfoo\n")
    original = target.read_bytes()
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "a.py", "old": "foo", "new": "bar"}, ctx)
    assert result.success is False
    assert "matches 3 times" in result.summary
    assert "must be unique" in result.summary
    # Apply mode + matching too many times = STILL no write.
    assert target.read_bytes() == original


def test_edit_replace_all_replaces_every_match(tmp_path: Path) -> None:
    # replace_all=True relaxes the exactly-once contract to "replace every
    # match" — N>1 succeeds and the diff naturally reflects all replacements.
    ctx, workspace, store = _ctx_and_workspace(tmp_path)
    target = workspace.root / "a.py"
    target.write_text("foo\nfoo\nfoo\n")
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke(
        {"path": "a.py", "old": "foo", "new": "bar", "replace_all": True}, ctx
    )
    assert result.success is True
    _assert_output_json_safe(result)
    assert result.output["applied"] is True
    assert result.output["before_sha256"] == _sha256("foo\nfoo\nfoo\n")
    assert result.output["after_sha256"] == _sha256("bar\nbar\nbar\n")
    # Every line changed → the diff carries all three replacements.
    assert result.output["added"] == 3
    assert result.output["removed"] == 3
    assert target.read_text() == "bar\nbar\nbar\n"
    diff = store.get(result.artifacts[0]).decode("utf-8")
    assert diff.count("+bar") == 3
    assert diff.count("-foo") == 3


def test_edit_replace_all_false_still_rejects_multi_match(tmp_path: Path) -> None:
    # Without replace_all (or with it explicitly false) an ambiguous N>1 match
    # is refused: the tool cannot know which occurrence was meant.
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    target = workspace.root / "a.py"
    target.write_text("foo\nfoo\n")
    original = target.read_bytes()
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke(
        {"path": "a.py", "old": "foo", "new": "bar", "replace_all": False}, ctx
    )
    assert result.success is False
    assert "matches 2 times" in result.summary
    assert "must be unique" in result.summary
    assert target.read_bytes() == original


def test_edit_replace_all_single_match_still_works(tmp_path: Path) -> None:
    # replace_all=True on a unique match behaves like the normal single edit.
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    target = workspace.root / "a.py"
    target.write_text("def foo():\n    return 1\n")
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke(
        {"path": "a.py", "old": "return 1", "new": "return 2", "replace_all": True},
        ctx,
    )
    assert result.success is True
    assert result.output["applied"] is True
    assert target.read_text() == "def foo():\n    return 2\n"


def test_edit_replace_all_zero_match_still_fails(tmp_path: Path) -> None:
    # replace_all does NOT relax the zero-match error — nothing to replace.
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    target = workspace.root / "a.py"
    target.write_text("foo\n")
    original = target.read_bytes()
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke(
        {"path": "a.py", "old": "missing", "new": "x", "replace_all": True}, ctx
    )
    assert result.success is False
    assert "not found" in result.summary
    assert target.read_bytes() == original


def test_edit_escape_rejected(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    # Even apply mode + escape = no write attempt.
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "/etc/passwd", "old": "x", "new": "y"}, ctx)
    assert result.success is False
    assert "outside workspace" in result.summary


def test_edit_not_a_file(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    (workspace.root / "sub").mkdir()
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "sub", "old": "x", "new": "y"}, ctx)
    assert result.success is False
    assert "not a file" in result.summary


def test_edit_binary_rejected(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    (workspace.root / "bin").write_bytes(b"\x00\xffbinary")
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "bin", "old": "x", "new": "y"}, ctx)
    assert result.success is False
    assert "utf-8" in result.summary


@pytest.mark.parametrize(
    "args",
    [
        {"old": "x", "new": "y"},                # missing path
        {"path": "", "old": "x", "new": "y"},    # empty path
        {"path": "a.py", "old": "", "new": "y"}, # empty old
        {"path": "a.py", "old": "x", "new": 5},  # non-string new
        {"path": 1, "old": "x", "new": "y"},     # non-string path
    ],
)
def test_edit_arg_validation(tmp_path: Path, args: dict[str, object]) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.py").write_text("x\n")
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke(args, ctx)
    assert result.success is False


# ---------------------------------------------------------------------------
# write — create-new + read-first overwrite + cap
# ---------------------------------------------------------------------------


def test_write_apply_creates_new_file(tmp_path: Path) -> None:
    ctx, workspace, store = _ctx_and_workspace(tmp_path)
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "new.txt", "content": "hello\n"}, ctx)
    assert result.success is True
    _assert_output_json_safe(result)
    assert result.output["applied"] is True
    assert result.output["bytes"] == 6
    assert result.output["before_sha256"] == _sha256("")
    assert result.output["after_sha256"] == _sha256("hello\n")
    target = workspace.root / "new.txt"
    assert target.read_text() == "hello\n"
    # Diff artifact contains the "create-new" diff (only `+` lines after the header).
    diff = store.get(result.artifacts[0]).decode("utf-8")
    assert "+hello" in diff
    assert "--- a/new.txt" in diff


def test_write_dry_run_does_not_create(tmp_path: Path) -> None:
    ctx, workspace, store = _ctx_and_workspace(tmp_path)
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.DRY_RUN)
    result = tool.invoke({"path": "new.txt", "content": "hello\n"}, ctx)
    assert result.success is True
    assert result.output["applied"] is False
    assert not (workspace.root / "new.txt").exists()
    # Proposed diff artifact is still produced.
    assert store.get(result.artifacts[0]).decode("utf-8").startswith("--- a/new.txt")


def test_write_existing_path_without_read_rejected(tmp_path: Path) -> None:
    # Overwriting a file you have NOT read this session is refused by the
    # read-first precondition — no blind clobber, and no write.
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    (workspace.root / "exists.txt").write_text("old\n")
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "exists.txt", "content": "new\n"}, ctx)
    assert result.success is False
    assert "must read" in result.summary
    assert "read-first precondition" in result.summary
    assert (workspace.root / "exists.txt").read_text() == "old\n"


def test_write_existing_path_after_read_overwrites(tmp_path: Path) -> None:
    # `read` offloads the file body into the content-addressed store, and that
    # blob is the evidence the precondition looks for.
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    (workspace.root / "exists.txt").write_text("old\n")
    reader = ReadFileTool(workspace=workspace)
    read_result = reader.invoke({"file_path": "exists.txt"}, ctx)
    assert read_result.success is True
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "exists.txt", "content": "new\n"}, ctx)
    assert result.success is True
    assert result.output["applied"] is True
    assert result.output["before_sha256"] == _sha256("old\n")
    assert result.output["after_sha256"] == _sha256("new\n")
    assert (workspace.root / "exists.txt").read_text() == "new\n"
    # The summary marks an overwrite (vs a create).
    assert result.summary.startswith("overwrite ")


def test_write_existing_path_stale_read_rejected(tmp_path: Path) -> None:
    # If the file changed on disk AFTER the read, the precondition fails: the
    # store has the OLD bytes, not the current ones, so the overwrite is a
    # blind clobber of content the model never saw.
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    target = workspace.root / "stale.txt"
    target.write_text("v1\n")
    reader = ReadFileTool(workspace=workspace)
    reader.invoke({"path": "stale.txt"}, ctx)
    target.write_text("v2-changed-on-disk\n")
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "stale.txt", "content": "v3\n"}, ctx)
    assert result.success is False
    assert "read-first precondition" in result.summary
    assert target.read_text() == "v2-changed-on-disk\n"


def test_write_exceeds_cap_rejected(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    big = "x" * (WRITE_FILE_MAX_BYTES + 1)
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "big.txt", "content": big}, ctx)
    assert result.success is False
    assert "exceeds" in result.summary
    assert not (workspace.root / "big.txt").exists()


def test_write_at_cap_accepted(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    body = "x" * WRITE_FILE_MAX_BYTES
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "edge.txt", "content": body}, ctx)
    assert result.success is True
    assert (workspace.root / "edge.txt").read_bytes() == body.encode("utf-8")


def test_write_escape_rejected(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "/tmp/x.txt", "content": "x"}, ctx)
    assert result.success is False
    assert "outside workspace" in result.summary


def test_write_parent_dir_must_exist(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "missing/dir/x.txt", "content": "x"}, ctx)
    assert result.success is False
    assert "parent directory" in result.summary


def test_write_inside_sub_dir_succeeds(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    (workspace.root / "sub").mkdir()
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": "sub/x.txt", "content": "x"}, ctx)
    assert result.success is True
    assert (workspace.root / "sub" / "x.txt").read_text() == "x"
    assert result.output["path"] == "sub/x.txt"


# ---------------------------------------------------------------------------
# Restricted write — injected path whitelist (the plan preset)
# ---------------------------------------------------------------------------


def test_restricted_write_allows_matching_path(tmp_path: Path) -> None:
    # A write built with allowed_path_globs=("plans/*.md",)
    # may write a plans/*.md file (the plan preset's only writable surface).
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    (workspace.root / "plans").mkdir()
    tool = WriteFileTool(
        workspace=workspace,
        mode=FsWriteMode.APPLY,
        allowed_path_globs=("plans/*.md",),
    )
    result = tool.invoke({"path": "plans/feature.md", "content": "# Plan\n"}, ctx)
    assert result.success is True
    assert (workspace.root / "plans" / "feature.md").read_text() == "# Plan\n"


def test_restricted_write_rejects_path_outside_whitelist(tmp_path: Path) -> None:
    # A path outside the whitelist is refused by the path guard — no file
    # created. This is the physical "plan can only touch plans/*.md" boundary.
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    tool = WriteFileTool(
        workspace=workspace,
        mode=FsWriteMode.APPLY,
        allowed_path_globs=("plans/*.md",),
    )
    result = tool.invoke({"path": "src/main.py", "content": "print()\n"}, ctx)
    assert result.success is False
    assert "writable allow-list" in result.summary
    assert not (workspace.root / "src").exists()


def test_restricted_write_rejects_wrong_extension_in_plans(tmp_path: Path) -> None:
    # The glob is plans/*.md — a non-.md file under plans/ is still refused.
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    (workspace.root / "plans").mkdir()
    tool = WriteFileTool(
        workspace=workspace,
        mode=FsWriteMode.APPLY,
        allowed_path_globs=("plans/*.md",),
    )
    result = tool.invoke({"path": "plans/notes.txt", "content": "x"}, ctx)
    assert result.success is False
    assert "writable allow-list" in result.summary
    assert not (workspace.root / "plans" / "notes.txt").exists()


def test_restricted_write_guard_precedes_read_first_check(tmp_path: Path) -> None:
    # The path guard fires BEFORE the read-first precondition: an existing
    # out-of-whitelist file the model never read is rejected by the path guard
    # (not the read-first error), and is left byte-identical.
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    (workspace.root / "src").mkdir()
    target = workspace.root / "src" / "main.py"
    target.write_text("old\n")
    tool = WriteFileTool(
        workspace=workspace,
        mode=FsWriteMode.APPLY,
        allowed_path_globs=("plans/*.md",),
    )
    result = tool.invoke({"path": "src/main.py", "content": "new\n"}, ctx)
    assert result.success is False
    assert "writable allow-list" in result.summary
    assert "read-first precondition" not in result.summary
    assert target.read_text() == "old\n"


def test_unrestricted_write_default_globs_empty(tmp_path: Path) -> None:
    # Default (empty globs) = unrestricted: any in-workspace path is writable.
    # Presets that inject no whitelist (main, general-purpose) rely on this.
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    assert tool.allowed_path_globs == ()
    result = tool.invoke({"path": "anywhere.py", "content": "x"}, ctx)
    assert result.success is True


def test_build_fs_tools_threads_write_path_globs(tmp_path: Path) -> None:
    # The assembly seam: build_fs_tools(write_path_globs=...) injects the
    # whitelist into the write tool only; read/glob/grep/edit are untouched.
    _, workspace, _ = _ctx_and_workspace(tmp_path)
    pack = build_fs_tools(
        workspace,
        mode=FsWriteMode.APPLY,
        write_path_globs=("plans/*.md",),
    )
    assert pack["write"].allowed_path_globs == ("plans/*.md",)  # type: ignore[attr-defined]
    # The default (no kwarg) build leaves write unrestricted.
    default_pack = build_fs_tools(workspace, mode=FsWriteMode.APPLY)
    assert default_pack["write"].allowed_path_globs == ()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "args",
    [
        {"content": "x"},                       # missing path
        {"path": "", "content": "x"},           # empty path
        {"path": "x.txt"},                      # missing content
        {"path": "x.txt", "content": 5},        # non-string content
        {"path": 1, "content": "x"},            # non-string path
    ],
)
def test_write_arg_validation(tmp_path: Path, args: dict[str, object]) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke(args, ctx)
    assert result.success is False


# ---------------------------------------------------------------------------
# Byte budgets
# ---------------------------------------------------------------------------


def test_edit_output_under_byte_budget_with_long_path(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    # Long path in workspace — output must still fit canonical encoding.
    deep = workspace.root
    for component in ["a" * 100, "b" * 100, "c" * 100]:
        deep = deep / component
        deep.mkdir()
    target = deep / "f.py"
    target.write_text("hello\n")
    relpath = str(target.relative_to(workspace.root))
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.DRY_RUN)
    result = tool.invoke({"path": relpath, "old": "hello", "new": "world"}, ctx)
    assert result.success is True
    assert len(_encode_output(result.output)) <= INLINE_OUTPUT_MAX_BYTES


def test_edit_summary_bounded_for_long_path(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    long_dir = workspace.root / ("d" * 200)
    long_dir.mkdir()
    target = long_dir / ("f" * 200 + ".py")
    target.write_text("hello\n")
    rel = str(target.relative_to(workspace.root))
    tool = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.DRY_RUN)
    result = tool.invoke({"path": rel, "old": "hello", "new": "world"}, ctx)
    assert result.success is True
    # Summary embeds the path — it must stay bounded.
    assert len(result.summary.encode("utf-8")) < 512


# ---------------------------------------------------------------------------
# Out-of-workspace writes — the host authorization seam (write_roots)
# ---------------------------------------------------------------------------


def _authorized_ctx(store: InMemoryContentStore) -> ToolContext:
    """A ctx carrying the task id the write_roots resolver is keyed by."""
    return ToolContext(artifact_store=store, metadata={"task_id": "task-1"})


def test_write_outside_the_workspace_is_refused_without_authorization(
    tmp_path: Path,
) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    other = tmp_path / "neighbour"
    other.mkdir()
    tool = WriteFileTool(workspace=workspace, mode=FsWriteMode.APPLY)
    result = tool.invoke({"path": str(other / "a.txt"), "content": "x"}, ctx)
    assert result.success is False
    assert "outside workspace" in result.summary
    assert not (other / "a.txt").exists()


def test_write_outside_the_workspace_lands_once_authorized(tmp_path: Path) -> None:
    ctx, workspace, store = _ctx_and_workspace(tmp_path)
    other = tmp_path / "neighbour"
    other.mkdir()
    tool = WriteFileTool(
        workspace=workspace,
        mode=FsWriteMode.APPLY,
        write_roots=lambda task_id: [str(other)] if task_id == "task-1" else [],
    )
    result = tool.invoke(
        {"path": str(other / "a.txt"), "content": "x"}, _authorized_ctx(store)
    )
    assert result.success is True
    assert (other / "a.txt").read_text() == "x"
    # No relative form outside the workspace ⇒ the absolute path is displayed.
    assert result.output["path"] == (other / "a.txt").resolve().as_posix()


def test_authorization_is_keyed_by_task(tmp_path: Path) -> None:
    # The resolver answers per task, so one task's grant is not another's.
    ctx, workspace, store = _ctx_and_workspace(tmp_path)
    other = tmp_path / "neighbour"
    other.mkdir()
    tool = WriteFileTool(
        workspace=workspace,
        mode=FsWriteMode.APPLY,
        write_roots=lambda task_id: [str(other)] if task_id == "task-1" else [],
    )
    foreign = ToolContext(artifact_store=store, metadata={"task_id": "task-2"})
    result = tool.invoke({"path": str(other / "a.txt"), "content": "x"}, foreign)
    assert result.success is False
    assert not (other / "a.txt").exists()


def test_authorization_failure_denies_rather_than_grants(tmp_path: Path) -> None:
    # A resolver that raises must not open the fence — the degenerate case
    # fails closed.
    ctx, workspace, store = _ctx_and_workspace(tmp_path)
    other = tmp_path / "neighbour"
    other.mkdir()

    def boom(task_id: str):
        raise RuntimeError("grant store is down")

    tool = WriteFileTool(
        workspace=workspace, mode=FsWriteMode.APPLY, write_roots=boom
    )
    result = tool.invoke(
        {"path": str(other / "a.txt"), "content": "x"}, _authorized_ctx(store)
    )
    assert result.success is False
    assert not (other / "a.txt").exists()


def test_authorization_without_a_task_id_denies(tmp_path: Path) -> None:
    ctx, workspace, _ = _ctx_and_workspace(tmp_path)
    other = tmp_path / "neighbour"
    other.mkdir()
    tool = WriteFileTool(
        workspace=workspace,
        mode=FsWriteMode.APPLY,
        write_roots=lambda task_id: [str(other)],
    )
    result = tool.invoke({"path": str(other / "a.txt"), "content": "x"}, ctx)
    assert result.success is False


def test_edit_outside_the_workspace_respects_authorization(tmp_path: Path) -> None:
    ctx, workspace, store = _ctx_and_workspace(tmp_path)
    other = tmp_path / "neighbour"
    other.mkdir()
    target = other / "a.txt"
    target.write_text("hello world", encoding="utf-8")
    unauthorized = ReplaceTextTool(workspace=workspace, mode=FsWriteMode.APPLY)
    assert unauthorized.invoke(
        {"path": str(target), "old": "world", "new": "there"}, ctx
    ).success is False
    assert target.read_text() == "hello world"

    tool = ReplaceTextTool(
        workspace=workspace,
        mode=FsWriteMode.APPLY,
        write_roots=lambda task_id: [str(other)],
    )
    result = tool.invoke(
        {"path": str(target), "old": "world", "new": "there"}, _authorized_ctx(store)
    )
    assert result.success is True
    assert target.read_text() == "hello there"
