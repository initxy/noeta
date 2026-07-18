"""Phase 4 I1 — read-only fs tools (`read` / `glob` / `grep`).

``read_file`` renamed to ``read``; ``list_dir`` retired.

Each tool is exercised against a real ``InMemoryContentStore`` so the
artifact path (large ``read``) is real, and every ``ToolResult.output``
is checked against ``runtime.tool._encode_output`` for the B1 invariant
(stdlib json.dumps survives — no raw ``ContentRef`` leaked inline).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.tool import _encode_output
from noeta.storage.memory import InMemoryContentStore
from noeta.tools._limits import INLINE_CONTENT_MAX_BYTES, INLINE_OUTPUT_MAX_BYTES
from noeta.tools.fs import (
    GlobTool,
    GrepTool,
    ReadFileTool,
    WorkspaceRoot,
    build_fs_tools,
)


def _ctx_and_workspace(tmp_path: Path) -> tuple[ToolContext, WorkspaceRoot]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = InMemoryContentStore()
    ctx = ToolContext(artifact_store=store)
    return ctx, WorkspaceRoot.from_path(workspace)


def _assert_output_json_safe(result: ToolResult) -> None:
    """B1: ToolResult.output must survive stdlib json.dumps."""
    _encode_output(result.output)


# ---------------------------------------------------------------------------
# build_fs_tools
# ---------------------------------------------------------------------------


def test_build_fs_tools_exposes_snake_case_names(tmp_path: Path) -> None:
    _, workspace = _ctx_and_workspace(tmp_path)
    tools = build_fs_tools(workspace)
    # I1 ships the read-only three (list_dir retired,
    # read_file renamed to read); I2 adds edit / write.
    assert {"read", "glob", "grep"} <= set(tools.keys())
    assert "list_dir" not in tools
    # Provider-safe: no dots, no upper case, no spaces — B15.
    for name in tools.keys():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name) is not None


# ---------------------------------------------------------------------------
# read (renamed from read_file)
# ---------------------------------------------------------------------------


def test_read_tool_name_is_read(tmp_path: Path) -> None:
    _, workspace = _ctx_and_workspace(tmp_path)
    assert ReadFileTool(workspace=workspace).name == "read"


def test_read_file_inline_small_file(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "hello.txt").write_text("hi\nthere\n")
    result = ReadFileTool(workspace=workspace).invoke({"path": "hello.txt"}, ctx)
    assert result.success is True
    _assert_output_json_safe(result)
    assert result.output["path"] == "hello.txt"
    assert result.output["content"] == "hi\nthere\n"
    assert result.output["total_lines"] == 2
    assert result.output["truncated"] is False
    # Even a small read offloads the FULL body as an artifact (deterministic
    # ref so resume reads it) — that artifact has the same bytes as on disk.
    assert len(result.artifacts) == 1


def test_read_file_offset_limit_slice(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    body = "".join(f"line{i}\n" for i in range(1, 11))
    (workspace.root / "many.txt").write_text(body)
    result = ReadFileTool(workspace=workspace).invoke(
        {"path": "many.txt", "offset": 3, "limit": 2}, ctx
    )
    assert result.success is True
    assert result.output["content"] == "line3\nline4\n"
    assert result.output["lines_read"] == 2
    assert result.output["offset"] == 3
    assert result.output["truncated"] is True  # didn't reach end


def test_read_file_offload_when_large(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    # Many short lines whose TOTAL bytes cleanly exceed the inline budget
    # (each line stays under the per-line clip, so this exercises the total
    # ceiling, not the per-line path).
    big = ("x" * 100 + "\n") * 1500  # ~151 KB, well over 64 KB
    (workspace.root / "big.txt").write_text(big)
    result = ReadFileTool(workspace=workspace).invoke({"path": "big.txt"}, ctx)
    assert result.success is True
    _assert_output_json_safe(result)
    # Inline encoding is under the hard ceiling.
    assert len(_encode_output(result.output)) <= INLINE_CONTENT_MAX_BYTES
    assert result.output["truncated"] is True
    # The artifact carries the FULL file body, not the shrunk excerpt.
    assert len(result.artifacts) == 1
    assert result.artifacts[0].size == len(big.encode("utf-8"))


def test_read_file_medium_file_fits_inline(tmp_path: Path) -> None:
    # A ~1500-line file of normal-width lines returns in full, untruncated —
    # the whole point of the widened budget (no forced re-read paging).
    ctx, workspace = _ctx_and_workspace(tmp_path)
    body = "".join(f"line {i}\n" for i in range(1500))
    (workspace.root / "mid.txt").write_text(body)
    result = ReadFileTool(workspace=workspace).invoke({"path": "mid.txt"}, ctx)
    assert result.success is True
    assert result.output["truncated"] is False
    assert result.output["total_lines"] == 1500
    assert result.output["lines_read"] == 1500
    assert result.output["content"] == body
    assert len(_encode_output(result.output)) <= INLINE_CONTENT_MAX_BYTES


def test_read_file_clips_overlong_line(tmp_path: Path) -> None:
    # A single minified line is clipped (with a marker) so it can't dominate
    # the inline budget; the artifact keeps the full untouched body.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    long_line = "a" * 50_000
    body = f"short\n{long_line}\ntail\n"
    (workspace.root / "min.js").write_text(body)
    result = ReadFileTool(workspace=workspace).invoke({"path": "min.js"}, ctx)
    assert result.success is True
    assert result.output["truncated"] is True  # a line was clipped
    lines = result.output["content"].splitlines()
    assert lines[0] == "short"
    assert lines[1].startswith("a" * 2000)
    assert "[line truncated]" in lines[1]
    assert len(lines[1]) < len(long_line)
    assert lines[2] == "tail"
    assert result.output["total_lines"] == 3
    # full body survives in the artifact
    assert result.artifacts[0].size == len(body.encode("utf-8"))


def test_read_file_clips_overlong_line_crlf(tmp_path: Path) -> None:
    # Clipping preserves a CRLF ending and the line count.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    body = "a" * 50_000 + "\r\n" + "next\r\n"
    (workspace.root / "crlf.txt").write_bytes(body.encode("utf-8"))
    result = ReadFileTool(workspace=workspace).invoke({"path": "crlf.txt"}, ctx)
    assert result.success is True
    assert result.output["total_lines"] == 2
    content_lines = result.output["content"].split("\r\n")
    assert content_lines[0].startswith("a" * 2000)
    assert "[line truncated]" in content_lines[0]
    assert content_lines[1] == "next"


def test_read_file_missing_path_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = ReadFileTool(workspace=workspace).invoke({}, ctx)
    assert result.success is False
    assert "non-empty 'path'" in result.summary


def test_read_file_not_a_file(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "sub").mkdir()
    result = ReadFileTool(workspace=workspace).invoke({"path": "sub"}, ctx)
    assert result.success is False
    assert "not a file" in result.summary


def test_read_file_escape_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (tmp_path / "outside.txt").write_text("secret")
    result = ReadFileTool(workspace=workspace).invoke(
        {"path": "../outside.txt"}, ctx
    )
    assert result.success is False
    assert "outside workspace" in result.summary


def test_read_file_binary_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "bin").write_bytes(b"\x00\xffnot utf-8")
    result = ReadFileTool(workspace=workspace).invoke({"path": "bin"}, ctx)
    assert result.success is False
    assert "utf-8" in result.summary


def test_read_file_stray_invalid_bytes_decoded_leniently(tmp_path: Path) -> None:
    # A text file with a few invalid bytes (legacy encoding remnants) is not
    # binary — it decodes with U+FFFD replacements instead of failing, and the
    # summary says so. Only a NUL byte marks real binary (see rejection above).
    ctx, workspace = _ctx_and_workspace(tmp_path)
    body = b"val x = 1 // caf\xe9\nval y = 2\n"
    (workspace.root / "legacy.kt").write_bytes(body)
    result = ReadFileTool(workspace=workspace).invoke({"path": "legacy.kt"}, ctx)
    assert result.success is True
    assert result.output["total_lines"] == 2
    assert "caf�" in result.output["content"]
    assert "val y = 2" in result.output["content"]
    assert "non-utf8 bytes replaced" in result.summary
    # The artifact still holds the original raw bytes.
    assert result.artifacts[0].size == len(body)


# ---------------------------------------------------------------------------
# read image
#
# A ``read`` of a supported image (png/jpeg/gif/webp) now surfaces the bytes as
# a ``ToolResult.images`` ContentRef so a vision model can SEE the image; read
# always emits (the vision gate lives in the adapter). PDFs and over-limit
# images still degrade with a precise message; a non-image binary keeps the
# generic "not utf-8" error.
# ---------------------------------------------------------------------------


# Minimal magic-byte headers; the detector sniffs content, not the extension.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_JPEG_MAGIC = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
_GIF_MAGIC = b"GIF89a" + b"\x00" * 16
_WEBP_MAGIC = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
_PDF_MAGIC = b"%PDF-1.7\n" + b"\x00" * 8


@pytest.mark.parametrize(
    ("name", "body", "media_type"),
    [
        ("pic.png", _PNG_MAGIC, "image/png"),
        ("photo.jpg", _JPEG_MAGIC, "image/jpeg"),
        ("anim.gif", _GIF_MAGIC, "image/gif"),
        ("shot.webp", _WEBP_MAGIC, "image/webp"),
    ],
)
def test_read_image_surfaces_for_vision(
    tmp_path: Path, name: str, body: bytes, media_type: str
) -> None:
    # A supported image is surfaced (success) with its bytes in
    # ``ToolResult.images`` and a small metadata ``output`` (path/media_type/
    # bytes). The image ref carries the magic-byte-derived media type.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / name).write_bytes(body)
    result = ReadFileTool(workspace=workspace).invoke({"path": name}, ctx)
    assert result.success is True
    _assert_output_json_safe(result)
    assert len(result.images) == 1
    assert result.images[0].media_type == media_type
    assert result.images[0].size == len(body)
    assert result.output == {
        "path": name,
        "media_type": media_type,
        "bytes": len(body),
    }
    assert "image" in result.summary
    # The image bytes really landed in the content store under that ref.
    assert ctx.artifact_store.get(result.images[0]) == body


def test_read_image_detected_by_content_not_extension(tmp_path: Path) -> None:
    # A PNG with a misleading ``.txt`` name is still caught by magic bytes and
    # surfaced as an image, not decoded as text.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "really_an_image.txt").write_bytes(_PNG_MAGIC)
    result = ReadFileTool(workspace=workspace).invoke(
        {"path": "really_an_image.txt"}, ctx
    )
    assert result.success is True
    assert len(result.images) == 1
    assert result.images[0].media_type == "image/png"
    assert result.output["media_type"] == "image/png"


def test_read_image_over_limit_degrades(tmp_path: Path) -> None:
    # An image over the inline byte limit degrades (no auto-resize in v1) with
    # an actionable message and emits no image.
    from noeta.tools.fs.read import IMAGE_MAX_BYTES

    ctx, workspace = _ctx_and_workspace(tmp_path)
    big = _PNG_MAGIC + b"\x00" * (IMAGE_MAX_BYTES + 1)
    (workspace.root / "huge.png").write_bytes(big)
    result = ReadFileTool(workspace=workspace).invoke({"path": "huge.png"}, ctx)
    assert result.success is False
    assert "PNG image" in result.summary
    assert "inline limit" in result.summary
    assert result.images == []
    assert result.output is None


def test_read_pdf_still_degrades(tmp_path: Path) -> None:
    # A PDF is a separate (document) wire shape — still degrades, no image.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "doc.pdf").write_bytes(_PDF_MAGIC)
    result = ReadFileTool(workspace=workspace).invoke({"path": "doc.pdf"}, ctx)
    assert result.success is False
    assert "PDF document" in result.summary
    assert "not supported yet" in result.summary
    assert "not utf-8" not in result.summary
    assert result.images == []
    assert result.output is None


def test_read_non_image_binary_still_not_utf8(tmp_path: Path) -> None:
    # Non-image binary keeps the existing generic "not utf-8" error — only
    # recognised image / PDF magic bytes get special handling.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "blob.dat").write_bytes(b"\x00\x01\x02\xffnope")
    result = ReadFileTool(workspace=workspace).invoke({"path": "blob.dat"}, ctx)
    assert result.success is False
    assert "not utf-8" in result.summary
    assert "image" not in result.summary


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


def test_glob_matches_relative_pattern(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.py").write_text("x")
    (workspace.root / "sub").mkdir()
    (workspace.root / "sub" / "b.py").write_text("x")
    (workspace.root / "c.txt").write_text("x")
    result = GlobTool(workspace=workspace).invoke({"pattern": "**/*.py"}, ctx)
    assert result.success is True
    _assert_output_json_safe(result)
    assert result.output["matches"] == ["a.py", "sub/b.py"]
    assert result.output["truncated"] is False


def test_glob_pattern_required(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GlobTool(workspace=workspace).invoke({}, ctx)
    assert result.success is False


def test_glob_absolute_pattern_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GlobTool(workspace=workspace).invoke({"pattern": "/etc/*"}, ctx)
    assert result.success is False
    assert "workspace-relative" in result.summary


def test_glob_dotdot_pattern_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GlobTool(workspace=workspace).invoke({"pattern": "../*"}, ctx)
    assert result.success is False


def test_glob_drops_symlink_to_outside(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("x")
    (workspace.root / "inside.py").write_text("x")
    (workspace.root / "link.py").symlink_to(outside)
    result = GlobTool(workspace=workspace).invoke({"pattern": "*.py"}, ctx)
    assert result.success is True
    assert "inside.py" in result.output["matches"]
    assert "link.py" not in result.output["matches"]


def test_glob_bounded_under_many_matches(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    for i in range(300):
        (workspace.root / f"file_{i:04d}.txt").write_text("x")
    result = GlobTool(workspace=workspace).invoke({"pattern": "*.txt"}, ctx)
    assert result.success is True
    assert len(_encode_output(result.output)) <= INLINE_OUTPUT_MAX_BYTES
    assert result.output["total"] == 300
    assert result.output["truncated"] is True


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def test_grep_finds_matches(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.py").write_text("def foo():\n    pass\n")
    (workspace.root / "b.py").write_text("def bar():\n    return foo()\n")
    result = GrepTool(workspace=workspace).invoke({"pattern": r"foo"}, ctx)
    assert result.success is True
    _assert_output_json_safe(result)
    matches = result.output["matches"]
    assert {(m["path"], m["line_number"]) for m in matches} == {
        ("a.py", 1),
        ("b.py", 2),
    }


def test_grep_scoped_to_path(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.py").write_text("foo\n")
    (workspace.root / "sub").mkdir()
    (workspace.root / "sub" / "b.py").write_text("foo\n")
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": r"foo", "path": "sub"}, ctx
    )
    assert result.success is True
    assert [m["path"] for m in result.output["matches"]] == ["sub/b.py"]


def test_grep_glob_filter(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.py").write_text("foo\n")
    (workspace.root / "a.txt").write_text("foo\n")
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "foo", "glob": "*.py"}, ctx
    )
    assert result.success is True
    assert [m["path"] for m in result.output["matches"]] == ["a.py"]


def test_grep_invalid_regex_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GrepTool(workspace=workspace).invoke({"pattern": "[unclosed"}, ctx)
    assert result.success is False
    assert "invalid regex" in result.summary


def test_grep_rejects_catastrophic_backtracking_pattern(tmp_path: Path) -> None:
    # A nested-quantifier pattern that would backtrack exponentially is refused
    # up front: grep runs in-process on the engine worker thread and CPython's
    # ``re`` holds the GIL for the whole match, so there is no way to time it
    # out — the only safe guard is to never run it. Without this guard
    # ``(a+)+$`` over a line of many 'a's with no trailing match hangs the step.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.txt").write_text("a" * 40 + "!\n")
    result = GrepTool(workspace=workspace).invoke({"pattern": r"(a+)+$"}, ctx)
    assert result.success is False
    assert "nested quantifiers" in result.summary


def test_grep_rejects_overlapping_alternation_backtracking(tmp_path: Path) -> None:
    # The alternation-ambiguity ReDoS class the nested-quantifier check misses:
    # an unbounded repeat over an overlapping alternation ((a|a)*, (a|ab)*,
    # (a?|b)+) backtracks exponentially on a long run of the shared char with no
    # trailing match. Refused up front for the same GIL/no-timeout reason.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.txt").write_text("a" * 40 + "!\n")
    for pat in (r"(a|a)*$", r"(a|ab)*$", r"(a?|b)+$"):
        result = GrepTool(workspace=workspace).invoke({"pattern": pat}, ctx)
        assert result.success is False, pat
        assert "catastrophic backtracking" in result.summary, pat


def test_grep_allows_ordinary_quantifiers(tmp_path: Path) -> None:
    # Sibling / single quantifiers and DISJOINT alternations are not overlap-
    # prone and must keep working after the ReDoS guard.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.txt").write_text("hello   world\n")
    for pat in (r"\s+world", r"(foo|hello)\s*world", r".*world", r"(cat|dog)*world"):
        result = GrepTool(workspace=workspace).invoke({"pattern": pat}, ctx)
        assert result.success is True, pat
        assert result.output["total"] == 1, pat


def test_grep_skips_binary_files(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "bin").write_bytes(b"\x00\xff\xff\xfeunreadable")
    (workspace.root / "text.txt").write_text("hello world\n")
    result = GrepTool(workspace=workspace).invoke({"pattern": "hello"}, ctx)
    assert result.success is True
    assert [m["path"] for m in result.output["matches"]] == ["text.txt"]


def test_grep_caps_long_lines(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    long_line = "x" * 5000 + "needle" + "y" * 5000
    (workspace.root / "big.txt").write_text(long_line + "\n")
    result = GrepTool(workspace=workspace).invoke({"pattern": "needle"}, ctx)
    assert result.success is True
    assert len(_encode_output(result.output)) <= INLINE_OUTPUT_MAX_BYTES
    assert len(result.output["matches"]) == 1
    assert len(result.output["matches"][0]["line"].encode("utf-8")) <= 400


def test_grep_total_vs_inline_when_many_matches(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    lines = "".join("foo\n" for _ in range(200))
    (workspace.root / "many.txt").write_text(lines)
    result = GrepTool(workspace=workspace).invoke({"pattern": "foo"}, ctx)
    assert result.success is True
    assert result.output["total"] == 200
    assert result.output["truncated"] is True
    assert len(result.output["matches"]) <= 50


def test_grep_escape_rejected(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "x", "path": "/etc"}, ctx
    )
    assert result.success is False
    assert "outside workspace" in result.summary


def test_grep_on_single_file(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    (workspace.root / "a.py").write_text("hello world\n")
    result = GrepTool(workspace=workspace).invoke(
        {"pattern": "world", "path": "a.py"}, ctx
    )
    assert result.success is True
    assert [m["line_number"] for m in result.output["matches"]] == [1]


@pytest.mark.parametrize(
    "arg_overrides",
    [
        {"pattern": ""},
        {"pattern": "x", "path": 1},
        {"pattern": 5},
        {"pattern": "x", "glob": 7},
    ],
)
def test_grep_arg_validation(tmp_path: Path, arg_overrides: dict[str, object]) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    result = GrepTool(workspace=workspace).invoke(arg_overrides, ctx)
    assert result.success is False


# ---------------------------------------------------------------------------
# Read reaches skill roots outside the workspace via skill_roots
# ---------------------------------------------------------------------------


def _skill_root(tmp_path: Path, files: dict[str, str]) -> Path:
    """A skill pack dir OUTSIDE the workspace, with bundled files."""
    root = tmp_path / "skills" / "pack"
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root.resolve()


def test_read_reaches_skill_root_by_absolute_path(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    root = _skill_root(tmp_path, {"references/NOTE.md": "CONVENTION: be terse."})
    tool = ReadFileTool(workspace=workspace, skill_roots=(root,))
    abs_path = str(root / "references" / "NOTE.md")
    result = tool.invoke({"path": abs_path}, ctx)
    _assert_output_json_safe(result)
    assert result.success is True
    assert result.output["content"] == "CONVENTION: be terse."
    # display is the absolute POSIX path (it is not under the workspace).
    assert result.output["path"] == (root / "references" / "NOTE.md").as_posix()


def test_read_without_skill_roots_still_walls_off_outside(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    root = _skill_root(tmp_path, {"NOTE.md": "x"})
    # default skill_roots=() — the absolute path escapes the workspace.
    tool = ReadFileTool(workspace=workspace)
    result = tool.invoke({"path": str(root / "NOTE.md")}, ctx)
    assert result.success is False
    assert "outside workspace" in result.summary


def test_read_rejects_path_outside_all_roots(tmp_path: Path) -> None:
    ctx, workspace = _ctx_and_workspace(tmp_path)
    root = _skill_root(tmp_path, {"NOTE.md": "x"})
    other = tmp_path / "elsewhere.md"
    other.write_text("secret", encoding="utf-8")
    tool = ReadFileTool(workspace=workspace, skill_roots=(root,))
    result = tool.invoke({"path": str(other)}, ctx)
    assert result.success is False
    assert "secret" not in str(result.output)


def test_read_skill_root_symlink_escape_refused(tmp_path: Path) -> None:
    import os

    ctx, workspace = _ctx_and_workspace(tmp_path)
    root = _skill_root(tmp_path, {})
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, root / "LINK.md")
    tool = ReadFileTool(workspace=workspace, skill_roots=(root,))
    result = tool.invoke({"path": str(root / "LINK.md")}, ctx)
    assert result.success is False
    assert result.output is None or "secret" not in str(result.output)


def test_read_relative_path_ignores_skill_roots(tmp_path: Path) -> None:
    # a relative path always resolves against the workspace, never a skill
    # root — only absolute targets may land in an extra root.
    ctx, workspace = _ctx_and_workspace(tmp_path)
    root = _skill_root(tmp_path, {"NOTE.md": "from skill"})
    tool = ReadFileTool(workspace=workspace, skill_roots=(root,))
    result = tool.invoke({"path": "NOTE.md"}, ctx)  # not in the workspace
    assert result.success is False
