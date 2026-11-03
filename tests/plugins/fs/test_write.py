"""``Write``: create, read-first overwrite, cap, path globs, write fence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from noeta.core.tool import ToolContext
from noeta.plugins.fs import EditTool, ReadTool, WriteTool
from noeta.plugins.fs.write import WRITE_FILE_MAX_BYTES
from tests.plugins.conftest import Harness, text_of


async def _read(h: Harness, path: str) -> None:
    out = await h.call(ReadTool(), file_path=path)
    assert not out.is_error, text_of(out)


def test_write_metadata() -> None:
    tool = WriteTool()
    assert tool.name == "Write"
    assert tool.read_only is False
    assert tool.clearable is True
    assert tool.allowed_path_globs == ()
    schema = tool.input_schema
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["required"] == ["file_path", "content"]
    assert schema["additionalProperties"] is False
    assert all(p["description"] for p in schema["properties"].values())


async def test_write_creates_new_file(h: Harness, ws: Path) -> None:
    out = await h.call(WriteTool(), file_path="new.txt", content="hello\n")
    assert not out.is_error
    assert text_of(out) == f"File created successfully at: {ws / 'new.txt'} (what you wrote is the file's current state; no need to Read it back)"
    assert (ws / "new.txt").read_text() == "hello\n"
    diff = out.meta["diff"]
    assert "+hello" in diff
    assert "--- a/new.txt" in diff
    assert out.meta["created"] is True
    assert out.display is not None and out.display.kind == "diff"


async def test_write_existing_path_without_read_rejected(h: Harness, ws: Path) -> None:
    h.approved = True  # asked about: the read-first check holds
    (ws / "exists.txt").write_text("old\n")
    out = await h.call(WriteTool(), file_path="exists.txt", content="new\n")
    assert out.is_error
    assert text_of(out) == (
        "<tool_use_error>Read the file first: overwriting a file this conversation has not "
        "read is refused.</tool_use_error>"
    )
    assert (ws / "exists.txt").read_text() == "old\n"


async def test_an_allowed_overwrite_needs_no_read_first(h: Harness, ws: Path) -> None:
    h.allowed_outright = True
    (ws / "exists.txt").write_text("old\n")
    out = await h.call(WriteTool(), file_path="exists.txt", content="new\n")
    assert not out.is_error, text_of(out)
    assert (ws / "exists.txt").read_text() == "new\n"


@pytest.mark.parametrize("model", ["claude-opus-4-6", "claude-sonnet-4-5-20250929",
                                   "claude-haiku-4-5", "claude-3-5-sonnet-20241022"])
async def test_older_claude_models_keep_the_read_first_check(
        h: Harness, ws: Path, model: str) -> None:
    h.allowed_outright, h.model = True, model
    (ws / "exists.txt").write_text("old\n")
    out = await h.call(WriteTool(), file_path="exists.txt", content="new\n")
    assert out.is_error and "this conversation has not read" in text_of(out)
    h.model = "claude-opus-4-7"
    out = await h.call(WriteTool(), file_path="exists.txt", content="new\n")
    assert not out.is_error


async def test_without_a_permission_layer_an_unread_file_is_never_overwritten(
        h: Harness, ws: Path) -> None:
    # Nobody asked and nothing allowed it outright (a host running the file
    # tools without the permissions plugin): the read-first check holds.
    (ws / "exists.txt").write_text("old\n")
    out = await h.call(WriteTool(), file_path="exists.txt", content="new\n")
    assert out.is_error and "this conversation has not read" in text_of(out)
    out = await h.call(EditTool(), file_path="exists.txt", old_string="old", new_string="x")
    assert out.is_error and "this conversation has not read" in text_of(out)
    assert (ws / "exists.txt").read_text() == "old\n"


async def test_an_overwrite_after_a_cut_read_is_refused_even_when_allowed(
        h: Harness, ws: Path) -> None:
    # A read cut to fit the token cap showed only part: Claude Code refuses a
    # whole-file Write over it whatever the permissions say.
    (ws / "wide.txt").write_text("".join("y" * 150 + "\n" for _ in range(1700)))
    await _read(h, "wide.txt")
    out = await h.call(WriteTool(), file_path="wide.txt", content="new\n")
    assert out.is_error and "this conversation has not read" in text_of(out)


async def test_a_write_of_noeta_md_has_the_file_state_note(h: Harness, ws: Path) -> None:
    out = await h.call(WriteTool(), file_path="NOETA.md", content="a\n")
    assert text_of(out) == (f"File created successfully at: {ws / 'NOETA.md'} "
                            "(what you wrote is the file's current state; no need to Read it "
                            "back)")


async def test_write_onto_a_directory_says_to_name_the_file(h: Harness, ws: Path) -> None:
    (ws / "pkg").mkdir()
    out = await h.call(WriteTool(), file_path="pkg", content="x")
    assert text_of(out) == ("<tool_use_error>pkg is a directory. To create a file in it, put the file's "
                            "name at the end of file_path.</tool_use_error>")


async def test_an_overwrite_keeps_the_files_byte_order_mark(h: Harness, ws: Path) -> None:
    (ws / "bom.txt").write_bytes(b"\xef\xbb\xbfold\n")
    await _read(h, "bom.txt")
    assert not (await h.call(WriteTool(), file_path="bom.txt", content="new\n")).is_error
    assert (ws / "bom.txt").read_bytes() == b"\xef\xbb\xbfnew\n"


async def test_write_existing_path_after_read_overwrites(h: Harness, ws: Path) -> None:
    (ws / "exists.txt").write_text("old\n")
    await _read(h, "exists.txt")
    out = await h.call(WriteTool(), file_path="exists.txt", content="new\n")
    assert not out.is_error
    assert text_of(out) == (f"The file {ws / 'exists.txt'} has been updated successfully."
                            " (what you wrote is the file's current state; no need to Read it back)")
    assert (ws / "exists.txt").read_text() == "new\n"
    assert out.meta["created"] is False
    assert "-old" in out.meta["diff"] and "+new" in out.meta["diff"]


async def test_write_existing_path_stale_read_rejected(h: Harness, ws: Path) -> None:
    target = ws / "stale.txt"
    target.write_text("v1\n")
    await _read(h, "stale.txt")
    target.write_text("v2-changed-on-disk\n")
    out = await h.call(WriteTool(), file_path="stale.txt", content="v3\n")
    assert out.is_error
    assert text_of(out) == (
        "<tool_use_error>File has been modified since it was read. Read it again before "
        "overwriting it.</tool_use_error>"
    )
    assert target.read_text() == "v2-changed-on-disk\n"


async def test_write_then_overwrite_chains(h: Harness, ws: Path) -> None:
    assert not (await h.call(WriteTool(), file_path="f.txt", content="v1\n")).is_error
    out = await h.call(WriteTool(), file_path="f.txt", content="v2\n")
    assert not out.is_error, text_of(out)
    assert (ws / "f.txt").read_text() == "v2\n"


async def test_write_then_edit_chains_and_the_check_still_runs(h: Harness, ws: Path) -> None:
    # A file the model wrote is one it has seen, so an edit chains; an edit
    # whose string is gone still fails (the check ran).
    target = ws / "note.txt"
    assert not (await h.call(WriteTool(), file_path="note.txt", content="one\n")).is_error
    first = await h.call(EditTool(), file_path="note.txt", old_string="one", new_string="x")
    assert not first.is_error
    await _read(h, "note.txt")
    second = await h.call(EditTool(), file_path="note.txt", old_string="one", new_string="two")
    assert second.is_error
    assert target.read_text() == "x\n"


async def test_write_exceeds_cap_rejected(h: Harness, ws: Path) -> None:
    big = "x" * (WRITE_FILE_MAX_BYTES + 1)
    out = await h.call(WriteTool(), file_path="big.txt", content=big)
    assert out.is_error
    assert text_of(out) == (
        f"<tool_use_error>content {WRITE_FILE_MAX_BYTES + 1}B exceeds {WRITE_FILE_MAX_BYTES}B cap</tool_use_error>"
    )
    assert not (ws / "big.txt").exists()


async def test_write_over_old_64k_limit_accepted(h: Harness, ws: Path) -> None:
    body = "line\n" * 20_000
    out = await h.call(WriteTool(), file_path="big.txt", content=body)
    assert not out.is_error
    assert (ws / "big.txt").read_text() == body


async def test_write_escape_rejected(h: Harness, ws: Path) -> None:
    out = await h.call(WriteTool(), file_path="/tmp/x.txt", content="x")
    assert out.is_error
    assert text_of(out) == f"<tool_use_error>path '/tmp/x.txt' resolves outside workspace {str(ws)!r}</tool_use_error>"


async def test_write_dotdot_escape_rejected(h: Harness, ws: Path) -> None:
    out = await h.call(WriteTool(), file_path="../x.txt", content="x")
    assert out.is_error
    assert "outside workspace" in text_of(out)
    assert not (ws.parent / "x.txt").exists()


async def test_write_through_symlink_out_of_workspace_rejected(h: Harness, ws: Path) -> None:
    outside = ws.parent / "outside"
    outside.mkdir()
    os.symlink(outside, ws / "link")
    out = await h.call(WriteTool(), file_path="link/x.txt", content="x")
    assert out.is_error
    assert "outside workspace" in text_of(out)
    assert not (outside / "x.txt").exists()


async def test_write_string_prefix_sibling_is_outside(h: Harness, ws: Path) -> None:
    sibling = Path(str(ws) + "-old")
    sibling.mkdir()
    out = await h.call(WriteTool(), file_path=str(sibling / "a.txt"), content="x")
    assert out.is_error
    assert not (sibling / "a.txt").exists()


async def test_write_creates_missing_parent_dirs(h: Harness, ws: Path) -> None:
    out = await h.call(WriteTool(), file_path="missing/dir/x.txt", content="x")
    assert not out.is_error
    assert (ws / "missing" / "dir" / "x.txt").read_text() == "x"


async def test_write_inside_sub_dir(h: Harness, ws: Path) -> None:
    (ws / "sub").mkdir()
    out = await h.call(WriteTool(), file_path="sub/x.txt", content="x")
    assert not out.is_error
    assert text_of(out) == f"File created successfully at: {ws / 'sub' / 'x.txt'} (what you wrote is the file's current state; no need to Read it back)"


async def test_write_over_a_binary_file_after_read_is_rejected(h: Harness, ws: Path) -> None:
    (ws / "b.bin").write_bytes(b"\xff\xfe")
    # Read records nothing for a non-text file, so the read-first check refuses.
    out = await h.call(WriteTool(), file_path="b.bin", content="x")
    assert out.is_error


async def test_write_keeps_file_permissions(h: Harness, ws: Path) -> None:
    target = ws / "run.sh"
    target.write_text("echo a\n")
    target.chmod(0o750)
    await _read(h, "run.sh")
    assert not (await h.call(WriteTool(), file_path="run.sh", content="echo b\n")).is_error
    assert target.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"content": "x"}, "requires non-empty 'file_path'"),
        ({"file_path": "", "content": "x"}, "requires non-empty 'file_path'"),
        ({"file_path": "x.txt"}, "requires string 'content'"),
        ({"file_path": "x.txt", "content": 5}, "requires string 'content'"),
        ({"file_path": 1, "content": "x"}, "requires non-empty 'file_path'"),
    ],
)
async def test_write_arg_validation(h: Harness, args: dict[str, Any], message: str) -> None:
    out = await WriteTool().run(args, h.ctx())
    assert out.is_error
    assert text_of(out) == f"<tool_use_error>Write {message}</tool_use_error>"


# -- allowed_path_globs -------------------------------------------------------


async def test_restricted_write_allows_matching_path(h: Harness, ws: Path) -> None:
    tool = WriteTool(allowed_path_globs=("plans/*.md",))
    out = await h.call(tool, file_path="plans/feature.md", content="# Plan\n")
    assert not out.is_error
    assert (ws / "plans" / "feature.md").read_text() == "# Plan\n"


async def test_restricted_write_rejects_path_outside_whitelist(h: Harness, ws: Path) -> None:
    tool = WriteTool(allowed_path_globs=("plans/*.md",))
    out = await h.call(tool, file_path="src/main.py", content="print()\n")
    assert out.is_error
    assert text_of(out) == (
        "<tool_use_error>path 'src/main.py' is outside the writable allow-list "
        "(plans/*.md); this agent may only write matching paths</tool_use_error>"
    )
    assert not (ws / "src").exists()


async def test_restricted_write_rejects_wrong_extension(h: Harness, ws: Path) -> None:
    tool = WriteTool(allowed_path_globs=("plans/*.md",))
    out = await h.call(tool, file_path="plans/notes.txt", content="x")
    assert out.is_error
    assert "writable allow-list" in text_of(out)


async def test_restricted_write_guard_precedes_read_first_check(h: Harness, ws: Path) -> None:
    (ws / "src").mkdir()
    target = ws / "src" / "main.py"
    target.write_text("old\n")
    tool = WriteTool(allowed_path_globs=("plans/*.md",))
    out = await h.call(tool, file_path="src/main.py", content="new\n")
    assert out.is_error
    assert "writable allow-list" in text_of(out)
    assert "this conversation has not read" not in text_of(out)
    assert target.read_text() == "old\n"


def test_allowed_globs_are_sorted() -> None:
    assert WriteTool(allowed_path_globs=("b/*", "a/*")).allowed_path_globs == ("a/*", "b/*")


@pytest.mark.parametrize(
    ("globs", "path", "allowed"),
    [
        (("plans/*.md",), "plans/x.md", True),
        (("plans/*.md",), "plans/deep/x.md", False),
        (("plans/**/*.md",), "plans/deep/nested/x.md", True),
        (("plans/**/*.md",), "plans/x.md", True),
        (("*.{md,txt}",), "notes.txt", True),
        (("*.md",), "sub/x.md", False),
    ],
)
async def test_write_globs_star_does_not_cross_slash(
    h: Harness, globs: tuple[str, ...], path: str, allowed: bool
) -> None:
    out = await h.call(WriteTool(allowed_path_globs=globs), file_path=path, content="x")
    assert out.is_error is (not allowed), text_of(out)


# -- out-of-workspace writes (write_roots) ------------------------------------


def _neighbour(ws: Path) -> Path:
    other = ws.parent / "neighbour"
    other.mkdir()
    return other


async def test_write_outside_refused_without_authorization(h: Harness, ws: Path) -> None:
    other = _neighbour(ws)
    out = await h.call(WriteTool(), file_path=str(other / "a.txt"), content="x")
    assert out.is_error
    assert "outside workspace" in text_of(out)
    assert not (other / "a.txt").exists()


async def test_write_outside_lands_once_authorized(h: Harness, ws: Path) -> None:
    other = _neighbour(ws)
    tool = WriteTool(write_roots=lambda ctx: [str(other)] if ctx.task_id == "task-1" else [])
    out = await h.call(tool, file_path=str(other / "a.txt"), content="x")
    assert not out.is_error, text_of(out)
    assert (other / "a.txt").read_text() == "x"
    assert text_of(out) == f"File created successfully at: {other / 'a.txt'} (what you wrote is the file's current state; no need to Read it back)"


async def test_authorization_covers_subdirectories(h: Harness, ws: Path) -> None:
    other = _neighbour(ws)
    tool = WriteTool(write_roots=lambda ctx: [str(other)])
    out = await h.call(tool, file_path=str(other / "src" / "deep" / "a.py"), content="x")
    assert not out.is_error


async def test_authorization_is_keyed_by_the_context(h: Harness, ws: Path) -> None:
    other = _neighbour(ws)
    tool = WriteTool(write_roots=lambda ctx: [str(other)] if ctx.task_id == "task-1" else [])
    h.task_id = "task-2"
    out = await h.call(tool, file_path=str(other / "a.txt"), content="x")
    assert out.is_error
    assert not (other / "a.txt").exists()


async def test_authorization_failure_denies_rather_than_grants(h: Harness, ws: Path) -> None:
    other = _neighbour(ws)

    def boom(ctx: ToolContext) -> list[str]:
        raise RuntimeError("grant store is down")

    out = await h.call(WriteTool(write_roots=boom), file_path=str(other / "a.txt"), content="x")
    assert out.is_error
    assert not (other / "a.txt").exists()


async def test_relative_authorized_root_grants_nothing(h: Harness, ws: Path) -> None:
    other = _neighbour(ws)
    tool = WriteTool(write_roots=lambda ctx: ["neighbour", "", 5])  # type: ignore[list-item]
    out = await h.call(tool, file_path=str(other / "a.txt"), content="x")
    assert out.is_error


async def test_authorized_write_still_refuses_an_unread_overwrite(h: Harness, ws: Path) -> None:
    h.approved = True
    other = _neighbour(ws)
    target = other / "keep.txt"
    target.write_text("keep\n")
    tool = WriteTool(write_roots=lambda ctx: ["/"])
    out = await h.call(tool, file_path=str(target), content="clobber")
    assert out.is_error
    assert "this conversation has not read" in text_of(out)
    assert target.read_text() == "keep\n"


async def test_symlink_into_an_authorized_root_is_admitted(h: Harness, ws: Path) -> None:
    other = _neighbour(ws)
    (other / "a.txt").write_text("x")
    os.symlink(other / "a.txt", ws / "link.txt")
    await _read(h, "link.txt")
    refused = await h.call(WriteTool(), file_path="link.txt", content="y")
    assert refused.is_error
    tool = WriteTool(write_roots=lambda ctx: [str(other)])
    out = await h.call(tool, file_path="link.txt", content="y")
    assert not out.is_error, text_of(out)
    assert (other / "a.txt").read_text() == "y"
