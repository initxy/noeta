"""``Read`` / ``Glob`` / ``Grep`` — the read-only fs tools.

Reads are deliberately unfenced while writes are not: observation is reversible,
so naming an absolute path reads it wherever it points. These tests pin that
asymmetry together with the plain-text output contracts — ``Read`` in ``cat -n``
form, ``Glob`` as a newline path list (mtime-sorted), ``Grep`` in ripgrep-style
lines with output modes — plus the budgets, the walk filtering, and the ReDoS
guard that stops ``Grep`` running a pattern which would pin the worker thread.

Each tool runs against a real ``InMemoryContentStore`` so the artifact path is
real.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.tool import InMemoryFileReadRegistry
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.limits import INLINE_CONTENT_MAX_BYTES, INLINE_OUTPUT_MAX_BYTES
from noeta.builtins.fs.impl import GlobTool, GrepTool, ReadFileTool, build_fs_tools
from noeta.runtime.workspace import WorkspaceRoot


def _ctx_and_workspace(tmp_path: Path) -> tuple[ToolContext, WorkspaceRoot]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = InMemoryContentStore()
    ctx = ToolContext(
        artifact_store=store, file_read_registry=InMemoryFileReadRegistry()
    )
    return ctx, WorkspaceRoot.from_path(workspace)


# ---------------------------------------------------------------------------
# build_fs_tools
# ---------------------------------------------------------------------------


def test_build_fs_tools_exposes_reference_names(tmp_path: Path) -> None:
    _, workspace = _ctx_and_workspace(tmp_path)
    tools = build_fs_tools(workspace)
    # A subset check — the pack also carries the edit tools. Directory listing
    # gets no tool of its own; ``Glob`` covers it.
    assert {"Read", "Glob", "Grep"} <= set(tools.keys())
    assert "list_dir" not in tools
    # Provider-safe: letters/digits/underscore only, no dots or spaces.
    for name in tools.keys():
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) is not None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def test_read_tool_name(tmp_path: Path) -> None:
    _, workspace = _ctx_and_workspace(tmp_path)
    assert ReadFileTool(workspace=workspace).name == "Read"


def test_read_small_file_cat_n_form(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "hello.txt").write_text("hi\nthere\n")
    result = ReadFileTool(workspace=workspace).invoke(
        {"file_path": "hello.txt"}, ctx
    )
    assert result.success is True
    assert result.output == "     1\thi\n     2\tthere"
    # Even a small read offloads the FULL body as an artifact (deterministic
    # ref so audit reads it) — that artifact has the same bytes as on disk.
    assert len(result.artifacts) == 1


def test_read_records_the_read_in_the_registry(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    target = workspace.root / "hello.txt"
    target.write_text("hi\n")
    result = ReadFileTool(workspace=workspace).invoke(
        {"file_path": "hello.txt"}, ctx
    )
    assert result.success is True
    assert ctx.file_read_registry is not None
    import hashlib

    expected = hashlib.sha256(b"hi\n").hexdigest()
    assert ctx.file_read_registry.digest(str(target.resolve())) == expected


def test_read_offset_limit_slice_keeps_true_line_numbers(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    body = "".join(f"line{i}\n" for i in range(1, 11))
    (workspace.root / "many.txt").write_text(body)
    result = ReadFileTool(workspace=workspace).invoke(
        {"file_path": "many.txt", "offset": 3, "limit": 2}, ctx
    )
    assert result.success is True
    assert result.output.startswith("     3\tline3\n     4\tline4")
    assert "Showing lines 3-4 of 10 total lines" in result.output
    assert "offset=5" in result.output


def test_read_empty_file_warns(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "empty.txt").write_text("")
    result = ReadFileTool(workspace=workspace).invoke(
        {"file_path": "empty.txt"}, ctx
    )
    assert result.success is True
    assert "exists but has empty contents" in result.output
    assert "<system-reminder>" in result.output


def test_read_offset_past_end_warns_with_line_count(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "short.txt").write_text("one\ntwo\n")
    result = ReadFileTool(workspace=workspace).invoke(
        {"file_path": "short.txt", "offset": 10}, ctx
    )
    assert result.success is True
    assert "only 2 lines" in result.output


def test_read_offload_when_large(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    # Lines under the per-line clip whose TOTAL bytes cleanly exceed the
    # inline safety ceiling (this exercises the byte fence, not the window).
    big = ("x" * 1999 + "\n") * 1500  # ~3 MB, well over the 1 MiB ceiling
    (workspace.root / "big.txt").write_text(big)
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "big.txt"}, ctx)
    assert result.success is True
    assert len(result.output.encode("utf-8")) <= INLINE_CONTENT_MAX_BYTES + 512
    assert "Showing lines 1-" in result.output
    # The artifact carries the FULL file body, not the shrunk excerpt.
    assert len(result.artifacts) == 1
    assert result.artifacts[0].size == len(big.encode("utf-8"))


def test_read_medium_file_fits_in_full(tmp_path: Path) -> None:
    # A ~1500-line file of normal-width lines returns in full, untruncated: the
    # ceiling is sized so ordinary source files never force re-read paging.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    body = "".join(f"line {i}\n" for i in range(1500))
    (workspace.root / "mid.txt").write_text(body)
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "mid.txt"}, ctx)
    assert result.success is True
    assert "Showing lines" not in result.output
    assert result.output.endswith(f"  1500\tline 1499")


def test_read_clips_overlong_line(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "wide.txt").write_text("x" * 5000 + "\nshort\n")
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "wide.txt"}, ctx)
    assert result.success is True
    first_line = result.output.splitlines()[0]
    assert "… [line truncated]" in first_line
    assert len(first_line) < 2200
    assert result.output.splitlines()[1] == "     2\tshort"


def test_read_clips_overlong_line_crlf(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "wide.txt").write_bytes(b"y" * 5000 + b"\r\nnext\r\n")
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "wide.txt"}, ctx)
    assert result.success is True
    lines = result.output.splitlines()
    assert "… [line truncated]" in lines[0]
    assert lines[1] == "     2\tnext"


def test_read_missing_file_path_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = ReadFileTool(workspace=workspace).invoke({}, ctx)
    assert result.success is False
    assert "file_path" in result.summary


def test_read_not_a_file(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "adir").mkdir()
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "adir"}, ctx)
    assert result.success is False


def test_read_reaches_outside_the_workspace(tmp_path: Path) -> None:
    # Reads are unfenced: an absolute path outside the workspace reads fine.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("outside\n")
    result = ReadFileTool(workspace=workspace).invoke(
        {"file_path": str(outside)}, ctx
    )
    assert result.success is True
    assert "outside" in result.output


def test_read_binary_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "blob.bin").write_bytes(b"\x00\x01\x02")
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "blob.bin"}, ctx)
    assert result.success is False
    assert "not utf-8" in result.summary


def test_read_stray_invalid_bytes_decoded_leniently(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "legacy.txt").write_bytes(b"caf\xe9 latte\n")
    result = ReadFileTool(workspace=workspace).invoke(
        {"file_path": "legacy.txt"}, ctx
    )
    assert result.success is True
    assert "caf� latte" in result.output
    assert "Non-utf8 bytes were replaced" in result.output


_PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # magic + fake body
)


def test_read_image_surfaces_for_vision(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "shot.png").write_bytes(_PNG)
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "shot.png"}, ctx)
    assert result.success is True
    assert result.images and result.images[0].media_type == "image/png"
    assert "Read image shot.png" in result.output
    assert "image/png" in result.output


def test_read_image_detected_by_content_not_extension(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "shot.dat").write_bytes(_PNG)
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "shot.dat"}, ctx)
    assert result.success is True
    assert result.images


def test_read_image_over_limit_degrades(tmp_path: Path) -> None:
    from noeta.builtins.fs.impl.read import IMAGE_MAX_BYTES

    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "huge.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * (IMAGE_MAX_BYTES + 1)
    )
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "huge.png"}, ctx)
    assert result.success is False
    assert "crop/resize" in result.summary


def test_read_pdf_still_degrades(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "doc.pdf").write_bytes(b"%PDF-1.7 fake body")
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "doc.pdf"}, ctx)
    assert result.success is False
    assert "PDF" in result.summary


def test_read_non_image_binary_still_not_utf8(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "blob.tar").write_bytes(b"\xffnothing magic\x00")
    result = ReadFileTool(workspace=workspace).invoke({"file_path": "blob.tar"}, ctx)
    assert result.success is False


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------


def test_glob_matches_relative_pattern(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.py").write_text("")
    (workspace.root / "sub").mkdir()
    (workspace.root / "sub" / "b.py").write_text("")
    (workspace.root / "c.txt").write_text("")
    result = GlobTool(workspace=workspace).invoke({"pattern": "**/*.py"}, ctx)
    assert result.success is True
    assert set(result.output.splitlines()) == {"a.py", "sub/b.py"}


def test_glob_sorts_by_mtime_newest_first(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    old = workspace.root / "old.py"
    new = workspace.root / "new.py"
    old.write_text("")
    new.write_text("")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    result = GlobTool(workspace=workspace).invoke({"pattern": "*.py"}, ctx)
    assert result.output.splitlines() == ["new.py", "old.py"]


def test_glob_skips_hidden_and_dependency_dirs(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "keep.py").write_text("")
    (workspace.root / "node_modules").mkdir()
    (workspace.root / "node_modules" / "dep.py").write_text("")
    (workspace.root / ".hidden").mkdir()
    (workspace.root / ".hidden" / "h.py").write_text("")
    result = GlobTool(workspace=workspace).invoke({"pattern": "**/*.py"}, ctx)
    assert result.output.splitlines() == ["keep.py"]


def test_glob_no_matches(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GlobTool(workspace=workspace).invoke({"pattern": "*.zig"}, ctx)
    assert result.success is True
    assert result.output == "No files found"


def test_glob_pattern_required(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GlobTool(workspace=workspace).invoke({}, ctx)
    assert result.success is False


def test_glob_absolute_pattern_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GlobTool(workspace=workspace).invoke({"pattern": "/etc/*"}, ctx)
    assert result.success is False
    assert "relative" in result.summary


def test_glob_dotdot_pattern_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GlobTool(workspace=workspace).invoke({"pattern": "../*"}, ctx)
    assert result.success is False


def test_glob_searches_an_absolute_path_outside_the_workspace(
    tmp_path: Path,
) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.md").write_text("")
    result = GlobTool(workspace=workspace).invoke(
        {"pattern": "*.md", "path": str(outside)}, ctx
    )
    assert result.success is True
    assert result.output.splitlines() == [str((outside / "x.md").resolve())]


def test_glob_path_scopes_to_a_subdirectory(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.py").write_text("")
    (workspace.root / "sub").mkdir()
    (workspace.root / "sub" / "b.py").write_text("")
    result = GlobTool(workspace=workspace).invoke(
        {"pattern": "*.py", "path": "sub"}, ctx
    )
    assert result.output.splitlines() == ["sub/b.py"]


def test_glob_path_must_be_a_string(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GlobTool(workspace=workspace).invoke(
        {"pattern": "*.py", "path": 7}, ctx
    )
    assert result.success is False


def test_glob_missing_path_yields_no_matches(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GlobTool(workspace=workspace).invoke(
        {"pattern": "*.py", "path": "nowhere"}, ctx
    )
    assert result.success is True
    assert result.output == "No files found"


def test_glob_drops_symlink_to_outside(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    outside = tmp_path / "secret.py"
    outside.write_text("")
    (workspace.root / "leak.py").symlink_to(outside)
    result = GlobTool(workspace=workspace).invoke({"pattern": "*.py"}, ctx)
    assert result.success is True
    assert result.output == "No files found"


def test_glob_drops_a_symlink_escaping_the_searched_tree(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "inner").mkdir()
    real = workspace.root / "real.py"
    real.write_text("")
    (workspace.root / "inner" / "leak.py").symlink_to(real)
    result = GlobTool(workspace=workspace).invoke(
        {"pattern": "*.py", "path": "inner"}, ctx
    )
    assert result.success is True
    assert result.output == "No files found"


def test_glob_bounded_under_many_matches(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    for i in range(230):
        (workspace.root / f"f{i:03}.py").write_text("")
    result = GlobTool(workspace=workspace).invoke({"pattern": "*.py"}, ctx)
    assert result.success is True
    shown = [
        line
        for line in result.output.splitlines()
        if not line.startswith("(Results truncated")
    ]
    assert len(shown) == 200
    assert "showing 200 of 230" in result.output


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------


def _grep_fixture(tmp_path: Path) -> tuple[ToolContext, WorkspaceRoot]:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.py").write_text("alpha\nneedle here\nomega\n")
    (workspace.root / "b.py").write_text("nothing\n")
    (workspace.root / "c.txt").write_text("needle again\nand needle twice\n")
    return ctx, workspace


def test_grep_default_mode_lists_files(tmp_path: Path) -> None:
    ctx, workspace = _grep_fixture(tmp_path)
    result = GrepTool(workspace=workspace).invoke({"pattern": "needle"}, ctx)
    assert result.success is True
    assert set(result.output.splitlines()) == {"a.py", "c.txt"}


def test_grep_no_matches(tmp_path: Path) -> None:
    ctx, workspace = _grep_fixture(tmp_path)
    result = GrepTool(workspace=workspace).invoke({"pattern": "unicorn"}, ctx)
    assert result.success is True
    assert result.output == "No matches found"


def test_grep_content_mode_with_line_numbers(tmp_path: Path) -> None:
    ctx, workspace = _grep_fixture(tmp_path)
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle", "output_mode": "content", "-n": True}, ctx
    )
    assert result.success is True
    lines = result.output.splitlines()
    assert "a.py:2:needle here" in lines
    assert "c.txt:1:needle again" in lines
    assert "c.txt:2:and needle twice" in lines


def test_grep_content_mode_without_line_numbers(tmp_path: Path) -> None:
    ctx, workspace = _grep_fixture(tmp_path)
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle here", "output_mode": "content"}, ctx
    )
    assert result.output.splitlines() == ["a.py:needle here"]


def test_grep_case_insensitive(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "x.py").write_text("NEEDLE\n")
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle", "-i": True}, ctx
    )
    assert result.output.splitlines() == ["x.py"]


def test_grep_context_lines(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "ctx.txt").write_text(
        "one\ntwo\nhit\nfour\nfive\nsix\nhit\neight\n"
    )
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "hit", "output_mode": "content", "-n": True, "-C": 1}, ctx
    )
    lines = result.output.splitlines()
    assert "ctx.txt-2-two" in lines
    assert "ctx.txt:3:hit" in lines
    assert "ctx.txt-4-four" in lines
    assert "--" in lines  # the two context groups are separated
    assert "ctx.txt-6-six" in lines
    assert "ctx.txt:7:hit" in lines
    assert "ctx.txt-8-eight" in lines


def test_grep_count_mode(tmp_path: Path) -> None:
    ctx, workspace = _grep_fixture(tmp_path)
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle", "output_mode": "count"}, ctx
    )
    assert set(result.output.splitlines()) == {"a.py:1", "c.txt:2"}


def test_grep_head_limit(tmp_path: Path) -> None:
    ctx, workspace = _grep_fixture(tmp_path)
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle", "output_mode": "content", "head_limit": 1}, ctx
    )
    body = [
        line
        for line in result.output.splitlines()
        if not line.startswith("(Showing")
    ]
    assert len(body) == 1
    assert "Showing 1 of 3 matches" in result.output


def test_grep_multiline_spans_lines(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "m.py").write_text("def foo(\n    x\n):\n    pass\n")
    result = GrepTool(workspace=workspace).invoke(
        {
            "pattern": r"foo\(.*?\):",
            "multiline": True,
            "output_mode": "content",
            "-n": True,
        },
        ctx,
    )
    lines = result.output.splitlines()
    assert "m.py:1:def foo(" in lines
    assert "m.py:3:):" in lines


def test_grep_skips_hidden_and_dependency_dirs(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "keep.py").write_text("needle\n")
    (workspace.root / "node_modules").mkdir()
    (workspace.root / "node_modules" / "dep.py").write_text("needle\n")
    (workspace.root / ".git").mkdir()
    (workspace.root / ".git" / "config").write_text("needle\n")
    result = GrepTool(workspace=workspace).invoke({"pattern": "needle"}, ctx)
    assert result.output.splitlines() == ["keep.py"]


def test_grep_scoped_to_path(tmp_path: Path) -> None:
    ctx, workspace = _grep_fixture(tmp_path)
    (workspace.root / "sub").mkdir()
    (workspace.root / "sub" / "d.py").write_text("needle deep\n")
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle", "path": "sub"}, ctx
    )
    assert result.output.splitlines() == ["sub/d.py"]


def test_grep_glob_filter(tmp_path: Path) -> None:
    ctx, workspace = _grep_fixture(tmp_path)
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle", "glob": "**/*.py"}, ctx
    )
    assert result.output.splitlines() == ["a.py"]


def test_grep_on_single_file(tmp_path: Path) -> None:
    ctx, workspace = _grep_fixture(tmp_path)
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle", "path": "c.txt", "output_mode": "content", "-n": True},
        ctx,
    )
    assert result.output.splitlines() == [
        "c.txt:1:needle again",
        "c.txt:2:and needle twice",
    ]


def test_grep_reaches_outside_the_workspace(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "far.txt").write_text("needle far\n")
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle", "path": str(outside)}, ctx
    )
    assert result.success is True
    assert result.output.splitlines() == [str((outside / "far.txt").resolve())]


def test_grep_invalid_regex_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GrepTool(workspace=workspace).invoke({"pattern": "([unclosed"}, ctx)
    assert result.success is False
    assert "invalid regex" in result.summary


def test_grep_bad_output_mode_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "x", "output_mode": "lines"}, ctx
    )
    assert result.success is False
    assert "output_mode" in result.summary


def test_grep_rejects_catastrophic_backtracking_pattern(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GrepTool(workspace=workspace).invoke({"pattern": "(a+)+$"}, ctx)
    assert result.success is False
    assert "catastrophic backtracking" in result.summary


def test_grep_rejects_overlapping_alternation_backtracking(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    for pattern in ["(a|a)*", "(a|ab)*x", "(a?|b)+"]:
        result = GrepTool(workspace=workspace).invoke({"pattern": pattern}, ctx)
        assert result.success is False, pattern


def test_grep_allows_ordinary_quantifiers(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "n.py").write_text("aaa needle\n")
    result = GrepTool(workspace=workspace).invoke({"pattern": "a+ needle"}, ctx)
    assert result.success is True
    assert result.output.splitlines() == ["n.py"]


def test_grep_skips_binary_files(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "blob.bin").write_bytes(b"\x00needle\x00")
    (workspace.root / "ok.txt").write_text("needle\n")
    result = GrepTool(workspace=workspace).invoke({"pattern": "needle"}, ctx)
    assert result.output.splitlines() == ["ok.txt"]


def test_grep_caps_long_lines(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "wide.txt").write_text("needle " + "x" * 5000 + "\n")
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle", "output_mode": "content"}, ctx
    )
    assert result.success is True
    line = result.output.splitlines()[0]
    assert "… [line truncated]" in line
    assert len(line) < 2200


def test_grep_many_matches_reports_total(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "many.txt").write_text("needle\n" * 120)
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "needle", "output_mode": "content"}, ctx
    )
    assert result.success is True
    assert "Showing 50 of 120 matches" in result.output


@pytest.mark.parametrize(
    "arg_overrides",
    [
        {"pattern": ""},
        {"pattern": "x", "path": 3},
        {"pattern": "x", "glob": 3},
        {"pattern": "x", "-A": -1},
        {"pattern": "x", "head_limit": 0},
    ],
)
def test_grep_arg_validation(tmp_path: Path, arg_overrides: dict[str, object]) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GrepTool(workspace=workspace).invoke(dict(arg_overrides), ctx)
    assert result.success is False


# ---------------------------------------------------------------------------
# path resolution details shared with skill packs
# ---------------------------------------------------------------------------


def test_read_reaches_skill_root_by_absolute_path(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    skill_root = tmp_path / "skills" / "demo"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# demo\n")
    result = ReadFileTool(workspace=workspace).invoke(
        {"file_path": str(skill_root / "SKILL.md")}, ctx
    )
    assert result.success is True
    assert "# demo" in result.output


def test_read_follows_a_symlink_out_of_the_workspace(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    outside = tmp_path / "target.txt"
    outside.write_text("linked\n")
    (workspace.root / "link.txt").symlink_to(outside)
    result = ReadFileTool(workspace=workspace).invoke(
        {"file_path": "link.txt"}, ctx
    )
    assert result.success is True
    assert "linked" in result.output


def test_read_relative_path_still_resolves_against_the_workspace(
    tmp_path: Path,
) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "rel.txt").write_text("relative\n")
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)  # NOT the workspace
        result = ReadFileTool(workspace=workspace).invoke(
            {"file_path": "rel.txt"}, ctx
        )
    finally:
        os.chdir(cwd)
    assert result.success is True
    assert "relative" in result.output


def test_read_rejects_an_empty_path(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = ReadFileTool(workspace=workspace).invoke({"file_path": ""}, ctx)
    assert result.success is False
