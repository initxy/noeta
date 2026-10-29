"""``Edit``: exact unique match, read-first, CRLF, the result line, write fence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from noeta.core.tool import ToolContext
from noeta.plugins.fs import EditTool, ReadTool
from tests.plugins.conftest import Harness, text_of


async def _read(h: Harness, path: str) -> None:
    out = await h.call(ReadTool(), file_path=path)
    assert not out.is_error, text_of(out)


def test_edit_metadata() -> None:
    tool = EditTool()
    assert tool.name == "Edit"
    assert tool.read_only is False
    assert tool.clearable is True
    schema = tool.input_schema
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["required"] == ["file_path", "old_string", "new_string"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["replace_all"]["default"] is False
    assert all(p["description"] for p in schema["properties"].values())


async def test_edit_writes_on_unique_match(h: Harness, ws: Path) -> None:
    target = ws / "a.py"
    target.write_text("def foo():\n    return 1\n")
    await _read(h, "a.py")
    out = await h.call(EditTool(), file_path="a.py", old_string="return 1", new_string="return 2")
    assert not out.is_error
    text = text_of(out)
    assert text == f"The file {ws / 'a.py'} has been updated successfully. (what you wrote is the file's current state; no need to Read it back)"
    assert target.read_text() == "def foo():\n    return 2\n"
    diff = out.meta["diff"]
    assert "-    return 1" in diff
    assert "+    return 2" in diff
    assert diff.startswith("--- a/a.py")
    assert (out.meta["added"], out.meta["removed"]) == (1, 1)
    assert out.display is not None and out.display.kind == "diff"
    assert out.display.data["diff"] == diff


async def test_edit_output_is_exact(h: Harness, ws: Path) -> None:
    (ws / "a.txt").write_text("a\nb\nc\n")
    await _read(h, "a.txt")
    out = await h.call(EditTool(), file_path="a.txt", old_string="b", new_string="B")
    assert text_of(out) == f"The file {ws / 'a.txt'} has been updated successfully. (what you wrote is the file's current state; no need to Read it back)"


async def test_an_edit_of_noeta_md_read_whole_has_the_file_state_note(
        h: Harness, ws: Path) -> None:
    # Claude Code's CLAUDE.md edit after a whole read carries the note (r2 m1/0071).
    (ws / "NOETA.md").write_text("a\n")
    await _read(h, "NOETA.md")
    out = await h.call(EditTool(), file_path="NOETA.md", old_string="a", new_string="b")
    assert text_of(out) == (f"The file {ws / 'NOETA.md'} has been updated successfully. "
                            "(what you wrote is the file's current state; no need to Read it "
                            "back)")


async def test_an_edit_without_the_whole_file_in_view_has_no_note(h: Harness, ws: Path) -> None:
    h.allowed_outright = True
    target = ws / "a.py"
    target.write_text("one\ntwo\nthree\n")
    await h.call(ReadTool(), file_path="a.py", offset=2)
    out = await h.call(EditTool(), file_path="a.py", old_string="two", new_string="TWO")
    assert text_of(out) == f"The file {target} has been updated successfully."
    # The next edit still lacks the rest of the file.
    out = await h.call(EditTool(), file_path="a.py", old_string="TWO", new_string="2")
    assert text_of(out) == f"The file {target} has been updated successfully."
    # Never read (an edit allowed outright): no note either.
    (ws / "b.py").write_text("x\n")
    out = await h.call(EditTool(), file_path="b.py", old_string="x", new_string="y")
    assert text_of(out) == f"The file {ws / 'b.py'} has been updated successfully."
    # A limited read that reached the end saw the whole file.
    (ws / "c.py").write_text("x\n")
    await h.call(ReadTool(), file_path="c.py", limit=50)
    out = await h.call(EditTool(), file_path="c.py", old_string="x", new_string="y")
    assert text_of(out).endswith("no need to Read it back)")


async def test_a_replace_all_result_says_every_occurrence_was_replaced(
        h: Harness, ws: Path) -> None:
    (ws / "a.txt").write_text("x x\n")
    await _read(h, "a.txt")
    out = await h.call(EditTool(), file_path="a.txt", old_string="x", new_string="y",
                       replace_all=True)
    assert text_of(out) == (
        f"The file {ws / 'a.txt'} has been updated: every occurrence was replaced. "
        "(what you wrote is the file's current state; no need to Read it back)")


async def test_consecutive_edits_chain_without_a_reread(h: Harness, ws: Path) -> None:
    (ws / "a.py").write_text("one\ntwo\n")
    await _read(h, "a.py")
    first = await h.call(EditTool(), file_path="a.py", old_string="one", new_string="ONE")
    assert not first.is_error
    second = await h.call(EditTool(), file_path="a.py", old_string="two", new_string="TWO")
    assert not second.is_error, text_of(second)
    assert (ws / "a.py").read_text() == "ONE\nTWO\n"


async def test_edit_without_read_first_rejected(h: Harness, ws: Path) -> None:
    # A person was asked about this edit: Claude Code keeps the read-first
    # check for a write that is not allowed outright.
    h.approved = True
    target = ws / "a.py"
    target.write_text("hello\n")
    out = await h.call(EditTool(), file_path="a.py", old_string="hello", new_string="world")
    assert out.is_error
    assert text_of(out) == (
        "Edit: Read the file first: editing a file this conversation has not read is "
        "refused."
    )
    assert target.read_text() == "hello\n"


async def test_an_allowed_edit_needs_no_read_first(h: Harness, ws: Path) -> None:
    # Claude Code skips the read-first check for a write its permissions allow
    # outright (an Edit after the model saw the file through Bash `cat`).
    h.allowed_outright = True
    target = ws / "a.py"
    target.write_text("hello\n")
    out = await h.call(EditTool(), file_path="a.py", old_string="hello", new_string="world")
    assert not out.is_error, text_of(out)
    assert target.read_text() == "world\n"


async def test_an_allowed_edit_applies_over_changes_since_the_read(h: Harness, ws: Path) -> None:
    h.allowed_outright = True
    target = ws / "a.py"
    target.write_text("v1\nkeep\n")
    await _read(h, "a.py")
    target.write_text("v1\nkeep\nadded on disk\n")
    out = await h.call(EditTool(), file_path="a.py", old_string="v1", new_string="v2")
    assert not out.is_error, text_of(out)
    assert target.read_text() == "v2\nkeep\nadded on disk\n"
    assert text_of(out) == (
        f"The file {target} has been updated successfully. (the file had changed on disk "
        "since your last Read; the edit applied cleanly, but the file holds other changes you "
        "have not seen, so Read it before an edit that depends on the text around it)")
    # Where old_string no longer picks one place, the model must re-read.
    target.write_text("v2\nv2\n")
    out = await h.call(EditTool(), file_path="a.py", old_string="v2", new_string="v3")
    assert text_of(out) == (
        "Edit: File has been modified since it was read. Read it again before editing it.")


async def test_edit_stale_read_rejected(h: Harness, ws: Path) -> None:
    h.approved = True  # asked about: no recovery over a changed file
    target = ws / "a.py"
    target.write_text("v1\n")
    await _read(h, "a.py")
    target.write_text("v2-changed-on-disk\n")
    out = await h.call(EditTool(), file_path="a.py", old_string="v2", new_string="v3")
    assert out.is_error
    assert text_of(out) == (
        "Edit: File has been modified since it was read. Read it again before editing it."
    )
    assert target.read_text() == "v2-changed-on-disk\n"


async def test_an_empty_old_string_creates_a_file(h: Harness, ws: Path) -> None:
    h.approved = True  # no read is needed to create a file, asked about or not
    out = await h.call(EditTool(), file_path="new/dir/a.txt", old_string="", new_string="hi\n")
    assert not out.is_error, text_of(out)
    assert (ws / "new" / "dir" / "a.txt").read_text() == "hi\n"
    assert text_of(out) == (f"The file {ws / 'new/dir/a.txt'} has been updated successfully. "
                            "(what you wrote is the file's current state; no need to Read it "
                            "back)")
    # An existing empty file is filled; one with content is refused.
    (ws / "empty.txt").write_text("  \n")
    assert not (await h.call(EditTool(), file_path="empty.txt", old_string="",
                             new_string="x\n")).is_error
    assert (ws / "empty.txt").read_text() == "x\n"
    out = await h.call(EditTool(), file_path="empty.txt", old_string="", new_string="y\n")
    assert text_of(out) == (
        "Edit: An empty old_string creates a new file, but this file already has content. "
        "Give the text to replace instead.")


async def test_straight_quotes_match_curly_ones_and_keep_the_files_style(
        h: Harness, ws: Path) -> None:
    (ws / "a.md").write_text("He said \u201chello\u201d and it\u2019s fine.\n")
    await _read(h, "a.md")
    out = await h.call(EditTool(), file_path="a.md", old_string='said "hello" and it\'s',
                       new_string='said "bye" and it\'s')
    assert not out.is_error, text_of(out)
    assert (ws / "a.md").read_text() == "He said \u201cbye\u201d and it\u2019s fine.\n"


async def test_unicode_escapes_and_their_characters_match_both_ways(
        h: Harness, ws: Path) -> None:
    (ws / "a.json").write_text('{"name": "caf\\u00E9", "city": "Z\u00fcrich"}\n')
    await _read(h, "a.json")
    # The file spells é as an escape; the model wrote the character.
    out = await h.call(EditTool(), file_path="a.json", old_string='"caf\u00e9"',
                       new_string='"na\u00efve caf\u00e9"')
    assert not out.is_error, text_of(out)
    # The file holds ü; the model wrote the escape.
    out = await h.call(EditTool(), file_path="a.json", old_string='"Z\\u00fcrich"',
                       new_string='"M\\u00fcnchen"')
    assert not out.is_error, text_of(out)
    assert (ws / "a.json").read_text() == (
        '{"name": "na\\u00EFve caf\\u00E9", "city": "M\u00fcnchen"}\n')
    out = await h.call(EditTool(), file_path="a.json", old_string="\u00e9t\u00e9",
                       new_string="x")
    assert text_of(out).endswith("Read the file again and copy the exact text.)")


async def test_deleting_a_line_takes_its_line_break(h: Harness, ws: Path) -> None:
    (ws / "a.py").write_text("keep\ndrop me\nkeep too\n")
    await _read(h, "a.py")
    assert not (await h.call(EditTool(), file_path="a.py", old_string="drop me",
                             new_string="")).is_error
    assert (ws / "a.py").read_text() == "keep\nkeep too\n"
    (ws / "b.txt").write_bytes(b"one\r\ntwo\r\n")
    await _read(h, "b.txt")
    assert not (await h.call(EditTool(), file_path="b.txt", old_string="one",
                             new_string="")).is_error
    assert (ws / "b.txt").read_bytes() == b"two\r\n"


async def test_a_file_over_1gb_is_refused_unread(h: Harness, ws: Path) -> None:
    with open(ws / "huge.log", "wb") as f:
        f.truncate(1536 * 1024 * 1024)  # sparse: nothing is written
    out = await h.call(EditTool(), file_path="huge.log", old_string="a", new_string="b")
    assert text_of(out) == (
        "Edit: At 1.5GB the file is too large for Edit, which takes files up to 1GB.")


async def test_a_notebook_is_left_to_notebook_edit(h: Harness, ws: Path) -> None:
    (ws / "n.ipynb").write_text('{"cells": []}')
    out = await h.call(EditTool(), file_path="n.ipynb", old_string="[]", new_string="[1]")
    assert text_of(out) == (
        "Edit: This is a Jupyter notebook: change its cells with NotebookEdit.")


async def test_edit_zero_match_fails_no_write(h: Harness, ws: Path) -> None:
    target = ws / "a.py"
    target.write_text("foo\n")
    await _read(h, "a.py")
    out = await h.call(EditTool(), file_path="a.py", old_string="missing", new_string="x")
    assert out.is_error
    # Claude Code echoes the string it looked for.
    assert text_of(out) == "Edit: old_string does not occur in the file.\nLooked for: missing"
    assert target.read_text() == "foo\n"


async def test_edit_multi_match_fails_no_write(h: Harness, ws: Path) -> None:
    target = ws / "a.py"
    target.write_text("foo\nfoo\nfoo\n")
    await _read(h, "a.py")
    out = await h.call(EditTool(), file_path="a.py", old_string="foo", new_string="bar")
    assert out.is_error
    assert text_of(out) == (
        "Edit: old_string occurs 3 times in the file and replace_all is false, so "
        "nothing was changed. Set replace_all to true to change all 3, or add nearby "
        "lines to old_string until it matches only one."
    )
    assert target.read_text() == "foo\nfoo\nfoo\n"


async def test_edit_replace_all_false_still_rejects_multi_match(h: Harness, ws: Path) -> None:
    (ws / "a.py").write_text("foo\nfoo\n")
    await _read(h, "a.py")
    out = await h.call(
        EditTool(), file_path="a.py", old_string="foo", new_string="bar", replace_all=False
    )
    assert out.is_error
    assert "old_string occurs 2 times" in text_of(out)
    assert (ws / "a.py").read_text() == "foo\nfoo\n"


async def test_edit_same_old_and_new_rejected(h: Harness, ws: Path) -> None:
    (ws / "a.py").write_text("foo\n")
    await _read(h, "a.py")
    out = await h.call(EditTool(), file_path="a.py", old_string="foo", new_string="foo")
    assert out.is_error
    assert text_of(out) == "Edit: old_string and new_string must be different"


async def test_edit_replace_all_replaces_every_match(h: Harness, ws: Path) -> None:
    target = ws / "a.py"
    target.write_text("foo\nfoo\nfoo\n")
    await _read(h, "a.py")
    out = await h.call(
        EditTool(), file_path="a.py", old_string="foo", new_string="bar", replace_all=True
    )
    assert not out.is_error
    assert text_of(out).startswith(f"The file {ws / 'a.py'} has been updated: every ")
    assert target.read_text() == "bar\nbar\nbar\n"
    assert out.meta["diff"].count("+bar") == 3
    assert out.meta["diff"].count("-foo") == 3
    assert out.meta["replacements"] == 3


async def test_edit_replace_all_single_match_still_works(h: Harness, ws: Path) -> None:
    (ws / "a.py").write_text("def foo():\n    return 1\n")
    await _read(h, "a.py")
    out = await h.call(
        EditTool(),
        file_path="a.py",
        old_string="return 1",
        new_string="return 2",
        replace_all=True,
    )
    assert not out.is_error
    assert (ws / "a.py").read_text() == "def foo():\n    return 2\n"


async def test_edit_replace_all_zero_match_still_fails(h: Harness, ws: Path) -> None:
    (ws / "a.py").write_text("foo\n")
    await _read(h, "a.py")
    out = await h.call(
        EditTool(), file_path="a.py", old_string="missing", new_string="x", replace_all=True
    )
    assert out.is_error
    assert "old_string does not occur in the file." in text_of(out)


async def test_edit_deletes_with_empty_new_string(h: Harness, ws: Path) -> None:
    (ws / "a.txt").write_text("keep\ndrop\nkeep2\n")
    await _read(h, "a.txt")
    out = await h.call(EditTool(), file_path="a.txt", old_string="drop\n", new_string="")
    assert not out.is_error
    assert (ws / "a.txt").read_text() == "keep\nkeep2\n"


async def test_edit_escape_rejected(h: Harness, ws: Path) -> None:
    out = await h.call(EditTool(), file_path="/etc/passwd", old_string="x", new_string="y")
    assert out.is_error
    assert text_of(out) == (
        f"Edit: path '/etc/passwd' resolves outside workspace {str(ws)!r}"
    )


async def test_edit_dotdot_escape_rejected(h: Harness, ws: Path) -> None:
    (ws.parent / "outside.txt").write_text("x\n")
    out = await h.call(EditTool(), file_path="../outside.txt", old_string="x", new_string="y")
    assert out.is_error
    assert "outside workspace" in text_of(out)
    assert (ws.parent / "outside.txt").read_text() == "x\n"


async def test_edit_symlink_out_of_the_workspace_rejected(h: Harness, ws: Path) -> None:
    other = ws.parent / "other.txt"
    other.write_text("x\n")
    os.symlink(other, ws / "link.txt")
    out = await h.call(EditTool(), file_path="link.txt", old_string="x", new_string="y")
    assert out.is_error
    assert "outside workspace" in text_of(out)


async def test_edit_through_a_symlink_inside_edits_the_target(h: Harness, ws: Path) -> None:
    (ws / "real.txt").write_text("x\n")
    os.symlink(ws / "real.txt", ws / "link.txt")
    await _read(h, "link.txt")
    out = await h.call(EditTool(), file_path="link.txt", old_string="x", new_string="y")
    assert not out.is_error, text_of(out)
    assert (ws / "real.txt").read_text() == "y\n"
    assert (ws / "link.txt").is_symlink()


async def test_edit_not_a_file(h: Harness, ws: Path) -> None:
    (ws / "sub").mkdir()
    out = await h.call(EditTool(), file_path="sub", old_string="x", new_string="y")
    assert out.is_error
    assert text_of(out) == "Edit: not a file: 'sub'"


async def test_edit_missing_file(h: Harness, ws: Path) -> None:
    out = await h.call(EditTool(), file_path="nope.txt", old_string="x", new_string="y")
    assert out.is_error
    assert text_of(out) == f"Edit: No file at that path (the working directory is {ws})."


async def test_edit_binary_rejected(h: Harness, ws: Path) -> None:
    (ws / "bin").write_bytes(b"\x00\xffbinary")
    out = await h.call(EditTool(), file_path="bin", old_string="x", new_string="y")
    assert out.is_error
    assert text_of(out) == "Edit: 'bin' is not utf-8 text"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"old_string": "x", "new_string": "y"}, "requires non-empty 'file_path'"),
        ({"file_path": "", "old_string": "x", "new_string": "y"}, "requires non-empty 'file_path'"),
        ({"file_path": "a.py", "old_string": 3, "new_string": "y"}, "requires string 'old_string'"),
        ({"file_path": "a.py", "old_string": "x", "new_string": 5}, "requires string 'new_string'"),
        ({"file_path": 1, "old_string": "x", "new_string": "y"}, "requires non-empty 'file_path'"),
    ],
)
async def test_edit_arg_validation(
    h: Harness, ws: Path, args: dict[str, Any], message: str
) -> None:
    (ws / "a.py").write_text("x\n")
    out = await EditTool().run(args, h.ctx())
    assert out.is_error
    assert text_of(out) == f"Edit: {message}"


# -- CRLF ---------------------------------------------------------------------


async def test_crlf_multi_line_edit_matches_and_keeps_crlf(h: Harness, ws: Path) -> None:
    target = ws / "crlf.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    await _read(h, "crlf.txt")
    out = await h.call(
        EditTool(), file_path="crlf.txt", old_string="alpha\nbeta", new_string="A\nB"
    )
    assert not out.is_error, text_of(out)
    assert target.read_bytes() == b"A\r\nB\r\ngamma\r\n"


async def test_crlf_single_line_edit_writes_crlf_breaks(h: Harness, ws: Path) -> None:
    target = ws / "crlf.txt"
    target.write_bytes(b"alpha\r\ngamma\r\n")
    await _read(h, "crlf.txt")
    out = await h.call(EditTool(), file_path="crlf.txt", old_string="gamma", new_string="G\nH")
    assert not out.is_error
    assert target.read_bytes() == b"alpha\r\nG\r\nH\r\n"


async def test_lf_file_edit_is_unchanged(h: Harness, ws: Path) -> None:
    target = ws / "lf.txt"
    target.write_bytes(b"a\nb\n")
    await _read(h, "lf.txt")
    out = await h.call(EditTool(), file_path="lf.txt", old_string="a\nb", new_string="x\ny")
    assert not out.is_error
    assert target.read_bytes() == b"x\ny\n"


async def test_edit_keeps_file_permissions(h: Harness, ws: Path) -> None:
    target = ws / "run.sh"
    target.write_text("echo a\n")
    target.chmod(0o755)
    await _read(h, "run.sh")
    out = await h.call(EditTool(), file_path="run.sh", old_string="a", new_string="b")
    assert not out.is_error
    assert target.stat().st_mode & 0o777 == 0o755


# -- out-of-workspace writes (write_roots) ------------------------------------


def _neighbour(ws: Path) -> Path:
    other = ws.parent / "neighbour"
    other.mkdir()
    return other


async def test_edit_outside_the_workspace_respects_authorization(
    h: Harness, ws: Path
) -> None:
    other = _neighbour(ws)
    target = other / "a.txt"
    target.write_text("hello world", encoding="utf-8")
    await _read(h, str(target))
    refused = await h.call(
        EditTool(), file_path=str(target), old_string="world", new_string="there"
    )
    assert refused.is_error
    assert "outside workspace" in text_of(refused)
    assert target.read_text() == "hello world"

    tool = EditTool(write_roots=lambda ctx: [str(other)])
    out = await h.call(tool, file_path=str(target), old_string="world", new_string="there")
    assert not out.is_error, text_of(out)
    assert target.read_text() == "hello there"
    # No relative form outside the workspace: the absolute path is shown.
    assert str(target) in text_of(out)


async def test_edit_authorization_failure_denies(h: Harness, ws: Path) -> None:
    other = _neighbour(ws)
    target = other / "a.txt"
    target.write_text("x")
    await _read(h, str(target))

    def boom(ctx: ToolContext) -> list[str]:
        raise RuntimeError("grant store is down")

    out = await h.call(EditTool(write_roots=boom), file_path=str(target), old_string="x", new_string="y")
    assert out.is_error
    assert target.read_text() == "x"


async def test_an_edit_of_a_very_large_file_skips_the_diff(h: Harness, ws: Path) -> None:
    # difflib is slow on many lines (seconds per megabyte): past a size the
    # edit is made without one.
    target = ws / "big.txt"
    target.write_text("".join(f"line {i}\n" for i in range(200_000)))
    await h.call(ReadTool(), file_path="big.txt", limit=1)
    out = await h.call(EditTool(), file_path="big.txt", old_string="line 7\n",
                       new_string="line seven\n")
    assert not out.is_error, text_of(out)
    assert "line seven\n" in target.read_text()
    assert out.meta["diff"] == ""
    assert out.display is not None and out.display.fallback == "big.txt (too large to diff)"
