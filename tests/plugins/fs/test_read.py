"""``Read``: ``cat -n`` output, offset / limit, size and token caps, images,
the read-first record, and unfenced path resolution (ported from 0.6)."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.plugins.fs import read as read_module
from noeta.plugins.fs.read import ReadTool
from noeta.plugins.fs.state import FS
from noeta.toolkit.images import image_dimensions
from tests.images import png
from tests.plugins.conftest import Harness, images_of, text_of


async def test_read_small_file_cat_n_form(h: Harness, ws: Path) -> None:
    (ws / "hello.txt").write_text("hi\nthere\n")
    out = await h.call(ReadTool(), file_path="hello.txt")
    assert not out.is_error
    # No padding; the final newline shows as a last, empty line.
    assert text_of(out) == "1\thi\n2\tthere\n3\t"


async def test_read_records_the_read(h: Harness, ws: Path) -> None:
    target = ws / "hello.txt"
    target.write_text("hi\n")
    out = await h.call(ReadTool(), file_path="hello.txt")
    assert not out.is_error
    expected = hashlib.sha256(b"hi\n").hexdigest()
    assert h.fs.digest(str(target)) == expected
    [(ext, kind, data)] = h.records
    assert (ext, kind) == (FS, "read")
    assert data["path"] == str(target)
    assert data["mtime"] == os.stat(target).st_mtime


async def test_read_offset_limit_slice_keeps_true_line_numbers(h: Harness, ws: Path) -> None:
    (ws / "many.txt").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    out = await h.call(ReadTool(), file_path="many.txt", offset=3, limit=2)
    assert text_of(out) == "3\tline3\n4\tline4"  # no footer
    tail = await h.call(ReadTool(), file_path="many.txt", offset=9, limit=5)
    assert text_of(tail) == "9\tline9\n10\tline10\n11\t"


async def test_read_empty_file_warns(h: Harness, ws: Path) -> None:
    (ws / "empty.txt").write_text("")
    out = await h.call(ReadTool(), file_path="empty.txt")
    assert not out.is_error
    assert text_of(out) == "Warning: the file is there but holds no content."


async def test_read_offset_past_end_warns_with_line_count(h: Harness, ws: Path) -> None:
    # Lines are counted as Claude Code counts them: a final newline starts
    # one more (empty) line.
    (ws / "short.txt").write_text("one\ntwo\n")
    out = await h.call(ReadTool(), file_path="short.txt", offset=10)
    assert not out.is_error
    assert text_of(out) == (
        "Warning: offset 10 is past the end of the file, which has 3 lines.")
    last = await h.call(ReadTool(), file_path="short.txt", offset=3)
    assert text_of(last) == "3\t"


async def test_read_refuses_a_window_past_the_token_ceiling(h: Harness, ws: Path) -> None:
    (ws / "big.txt").write_text(("x" * 1999 + "\n") * 1500)
    out = await h.call(ReadTool(), file_path="big.txt", limit=1500)
    assert out.is_error
    assert "over the 25000-token limit" in text_of(out)
    assert h.records == []  # a refused read records nothing


async def test_read_medium_file_fits_in_full(h: Harness, ws: Path) -> None:
    (ws / "mid.txt").write_text("".join(f"line {i}\n" for i in range(1500)))
    out = await h.call(ReadTool(), file_path="mid.txt")
    assert text_of(out).endswith("\n1500\tline 1499\n1501\t")


async def test_read_without_limit_returns_every_line_that_fits(h: Harness, ws: Path) -> None:
    # Claude Code's reader applies no line count of its own: without ``limit``
    # the whole file comes back, bounded only by the size and token ceilings.
    (ws / "long.txt").write_text("".join(f"{i}\n" for i in range(2500)))
    text = text_of(await h.call(ReadTool(), file_path="long.txt"))
    assert text.endswith("\n2500\t2499\n2501\t")


async def test_read_keeps_a_long_line_whole(h: Harness, ws: Path) -> None:
    # Claude Code cuts no line short; only the size and token ceilings apply.
    (ws / "wide.txt").write_text("x" * 5000 + "\nshort\n")
    lines = text_of(await h.call(ReadTool(), file_path="wide.txt")).splitlines()
    assert lines[0] == "1\t" + "x" * 5000
    assert lines[1] == "2\tshort"


async def test_read_keeps_a_long_crlf_line_whole(h: Harness, ws: Path) -> None:
    (ws / "wide.txt").write_bytes(b"y" * 5000 + b"\r\nnext\r\n")
    lines = text_of(await h.call(ReadTool(), file_path="wide.txt")).splitlines()
    assert lines[0] == "1\t" + "y" * 5000
    assert lines[1] == "2\tnext"


async def test_read_hides_a_byte_order_mark(h: Harness, ws: Path) -> None:
    (ws / "bom.txt").write_bytes(b"\xef\xbb\xbfa\nb\n")
    assert text_of(await h.call(ReadTool(), file_path="bom.txt")) == "1\ta\n2\tb\n3\t"


async def test_read_crlf_lines_drop_the_carriage_return(h: Harness, ws: Path) -> None:
    (ws / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    assert text_of(await h.call(ReadTool(), file_path="crlf.txt")) == "1\ta\n2\tb\n3\t"


async def test_read_last_line_without_newline_counts(h: Harness, ws: Path) -> None:
    (ws / "n.txt").write_bytes(b"a\nb")
    assert text_of(await h.call(ReadTool(), file_path="n.txt")) == "1\ta\n2\tb"


async def test_read_missing_file_path_rejected(h: Harness) -> None:
    out = await h.call(ReadTool())
    assert out.is_error
    assert text_of(out) == "Read: requires non-empty 'file_path'"


async def test_read_rejects_an_empty_path(h: Harness) -> None:
    out = await h.call(ReadTool(), file_path="")
    assert out.is_error


async def test_read_not_a_file(h: Harness, ws: Path) -> None:
    (ws / "adir").mkdir()
    out = await h.call(ReadTool(), file_path="adir")
    assert out.is_error
    assert text_of(out) == "Read: not a file: 'adir'"


async def test_read_missing_file(h: Harness, ws: Path) -> None:
    out = await h.call(ReadTool(), file_path="nope.txt")
    assert out.is_error
    assert text_of(out) == f"Read: No file at that path (the working directory is {ws})."


async def test_read_missing_file_suggests_another_extension(h: Harness, ws: Path) -> None:
    (ws / "app.ts").write_text("x\n")
    out = await h.call(ReadTool(), file_path="app.js")
    assert text_of(out) == (
        f"Read: No file at that path (the working directory is {ws}). Did you mean app.ts?")


async def test_read_missing_file_suggests_the_path_inside_the_workspace(
        h: Harness, ws: Path) -> None:
    (ws / "src").mkdir()
    (ws / "src" / "m.py").write_text("x\n")
    out = await h.call(ReadTool(), file_path=str(ws.parent / "src" / "m.py"))
    assert text_of(out).endswith(f"Did you mean {ws / 'src' / 'm.py'}?")


@pytest.mark.parametrize("path", ["/dev/zero", "/dev/stdin", "/dev/tty", "/dev/pts/3",
                                  "/proc/self/environ", "/proc/12/fd/1", "/proc/self/mem"])
async def test_read_refuses_device_and_process_files(h: Harness, path: str) -> None:
    out = await h.call(ReadTool(), file_path=path)
    assert out.is_error
    assert text_of(out) == f"Read: Read does not read device or process files, and {path} is one."


async def test_read_reaches_outside_the_workspace(h: Harness, ws: Path) -> None:
    outside = ws.parent / "elsewhere.txt"
    outside.write_text("outside\n")
    out = await h.call(ReadTool(), file_path=str(outside))
    assert not out.is_error
    assert "outside" in text_of(out)
    assert out.meta["path"] == str(outside)  # shown absolute


async def test_read_binary_rejected(h: Harness, ws: Path) -> None:
    (ws / "blob.xyz").write_bytes(b"\x00\x01\x02")
    out = await h.call(ReadTool(), file_path="blob.xyz")
    assert out.is_error
    assert text_of(out) == "Read: 'blob.xyz' is not utf-8 text"


@pytest.mark.parametrize("name", ["blob.bin", "Archive.ZIP", "lib.so", "data.sqlite", "x.pyc"])
async def test_read_refuses_a_binary_extension_unopened(h: Harness, ws: Path, name: str) -> None:
    # Claude Code refuses these by name, before looking at the file at all.
    out = await h.call(ReadTool(), file_path=name)
    ext = name[name.rindex("."):].lower()
    assert text_of(out) == (
        f"Read: Read does not open binary files, and a {ext} file is one. Open it with a "
        "skill made for the format, or with a shell command or script that understands it.")


async def test_read_stray_invalid_bytes_decoded_leniently(h: Harness, ws: Path) -> None:
    (ws / "legacy.txt").write_bytes(b"caf\xe9 latte\n")
    out = await h.call(ReadTool(), file_path="legacy.txt")
    assert not out.is_error
    text = text_of(out)
    assert "caf� latte" in text
    assert "(Non-utf8 bytes were replaced with U+FFFD.)" in text


_PNG = png(4, 3)


async def test_read_image_surfaces_for_vision(h: Harness, ws: Path) -> None:
    (ws / "shot.png").write_bytes(_PNG)
    out = await h.call(ReadTool(), file_path="shot.png")
    assert not out.is_error
    [img] = images_of(out)
    assert img.media_type == "image/png"
    assert img.data == _PNG
    # Claude Code's result is the image alone.
    assert out.content == [img]
    assert h.records == []  # images are not recorded for read-first


async def test_read_image_without_extension_detected_by_content(h: Harness, ws: Path) -> None:
    (ws / "shot").write_bytes(_PNG)
    out = await h.call(ReadTool(), file_path="shot")
    assert images_of(out)


async def test_read_image_kind_follows_the_extension(h: Harness, ws: Path) -> None:
    # Claude Code goes by the extension; content is sniffed only without one.
    (ws / "fake.png").write_text("<html>login</html>\n")
    out = await h.call(ReadTool(), file_path="fake.png")
    assert out.is_error
    assert text_of(out) == (
        "Read: fake.png has an image extension, but its bytes are not a PNG, JPEG, GIF or "
        "WebP image (often a saved error or login page). Check it with `file`, or look at "
        "it as text through Bash (`head -c 500`).")
    (ws / "pic.txt").write_bytes(_PNG)
    assert not images_of(await h.call(ReadTool(), file_path="pic.txt"))


async def test_read_other_image_kinds(h: Harness, ws: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    for name, fmt, media_type in (("a.jpg", "JPEG", "image/jpeg"), ("a.gif", "GIF", "image/gif"),
                                  ("a.webp", "WEBP", "image/webp")):
        pil.new("RGB", (5, 4)).save(ws / name, fmt)
        [img] = images_of(await h.call(ReadTool(), file_path=name))
        assert img.media_type == media_type


async def test_read_image_over_the_file_limit_degrades(h: Harness, ws: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_module, "IMAGE_MAX_BYTES", 100)
    (ws / "huge.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 101)
    out = await h.call(ReadTool(), file_path="huge.png")
    assert out.is_error
    assert text_of(out) == (
        "Read: 'huge.png' is a PNG image of 109 bytes, over the 0MB image read limit — "
        "resize it or export a smaller copy, then read that"
    )


async def test_read_shrinks_a_large_image_to_claude_codes_box(h: Harness, ws: Path) -> None:
    pytest.importorskip("PIL")
    (ws / "big.png").write_bytes(png(4000, 1000))
    out = await h.call(ReadTool(), file_path="big.png")
    [img] = images_of(out)
    assert image_dimensions(img.data) == (2000, 500)
    assert out.content == [img]
    # How the pixels map back to the file follows the result as a note.
    [note] = [d["text"] for e, k, d in h.records if (e, k) == (FS, "notice")]
    assert "scaled from 4000x1000 to 2000x500 px; multiply coordinates by 2.00" in note


async def test_an_image_that_cannot_be_sent_never_reaches_the_history(
        h: Harness, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)  # no image library
    (ws / "wide.png").write_bytes(png(9000, 10))
    out = await h.call(ReadTool(), file_path="wide.png")
    assert out.is_error and not images_of(out)
    assert "9000x10 px" in text_of(out) and "noeta[images]" in text_of(out)
    (ws / "corrupt.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    out = await h.call(ReadTool(), file_path="corrupt.png")
    assert out.is_error and not images_of(out)


async def test_read_fake_pdf_reports_failure(h: Harness, ws: Path) -> None:
    (ws / "doc.pdf").write_bytes(b"%PDF-1.7 fake body")
    out = await h.call(ReadTool(), file_path="doc.pdf")
    assert out.is_error
    assert "PDF" in text_of(out)


async def test_read_non_image_binary_still_not_utf8(h: Harness, ws: Path) -> None:
    (ws / "blob.tar").write_bytes(b"\xffnothing magic\x00")
    assert (await h.call(ReadTool(), file_path="blob.tar")).is_error


async def test_read_reaches_skill_root_by_absolute_path(h: Harness, ws: Path) -> None:
    skill_root = ws.parent / "skills" / "demo"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# demo\n")
    out = await h.call(ReadTool(), file_path=str(skill_root / "SKILL.md"))
    assert "# demo" in text_of(out)


async def test_read_follows_a_symlink_and_records_the_target(h: Harness, ws: Path) -> None:
    outside = ws.parent / "target.txt"
    outside.write_text("linked\n")
    (ws / "link.txt").symlink_to(outside)
    out = await h.call(ReadTool(), file_path="link.txt")
    assert "linked" in text_of(out)
    # Keyed by the canonical path, the form Edit / Write resolve to.
    assert h.fs.digest(str(outside)) is not None
    assert h.fs.digest(str(ws / "link.txt")) is None


async def test_read_relative_path_resolves_against_the_workspace(
    h: Harness, ws: Path
) -> None:
    (ws / "rel.txt").write_text("relative\n")
    cwd = os.getcwd()
    try:
        os.chdir(ws.parent)  # NOT the workspace
        out = await h.call(ReadTool(), file_path="rel.txt")
    finally:
        os.chdir(cwd)
    assert "relative" in text_of(out)


# ---------------------------------------------------------------------------
# Streaming: the window is read in chunks, the whole file is hashed
# ---------------------------------------------------------------------------


class _NoWholeReads(LocalExecEnv):
    async def read_bytes(self, path: str) -> bytes:  # pragma: no cover - must not run
        raise AssertionError("Read must not load the whole file")


async def test_read_streams_the_window_and_hashes_the_whole_file(ws: Path) -> None:
    h = Harness(env=_NoWholeReads(str(ws)))
    body = b"".join(b"line %d\n" % i for i in range(1, 300_001))  # ~3.3 MB
    (ws / "big.log").write_bytes(body)
    out = await h.call(ReadTool(), file_path="big.log", offset=250_000, limit=3)
    assert text_of(out) == "250000\tline 250000\n250001\tline 250001\n250002\tline 250002"
    assert h.fs.digest(str(ws / "big.log")) == hashlib.sha256(body).hexdigest()


async def test_read_refuses_a_whole_file_over_256kb(h: Harness, ws: Path) -> None:
    (ws / "wide.txt").write_text(("y" * 150 + "\n") * 3000)
    out = await h.call(ReadTool(), file_path="wide.txt")
    assert out.is_error
    assert text_of(out) == (
        "Read: At 442.4KB the file is over the 256KB limit for a read of the "
        "whole file. Read a part with offset and limit, or search the file for "
        "what you need."
    )
    big = await h.call(ReadTool(), file_path="wide.txt", allow_large=True)
    assert not big.is_error and text_of(big).startswith("1\t" + "y" * 150)


async def test_read_refuses_more_than_25000_tokens(h: Harness, ws: Path) -> None:
    (ws / "wide.txt").write_text(("y" * 150 + "\n") * 3000)
    out = await h.call(ReadTool(), file_path="wide.txt", limit=1000)
    assert out.is_error
    assert text_of(out) == (
        "Read: At 37750 tokens the file is over the 25000-token limit for one "
        "read. Read a part with offset and limit, search the file for what you "
        "need, or set allow_large when you need all of it."
    )
    ok = await h.call(ReadTool(), file_path="wide.txt", limit=500)
    assert not ok.is_error


async def test_a_whole_read_past_the_token_cap_returns_the_first_page(
        h: Harness, ws: Path) -> None:
    # Claude Code pages a whole-file read instead of refusing it: the lines
    # that fit come back, and a note after the result says how to go on.
    body = "".join(f"{i:05d} " + "y" * 143 + "\n" for i in range(1700))  # 255 KB
    (ws / "wide.txt").write_text(body)
    out = await h.call(ReadTool(), file_path="wide.txt")
    assert not out.is_error
    lines = text_of(out).split("\n")
    assert lines[0] == "1\t00000 " + "y" * 143
    shown = len(lines)
    assert 400 < shown < 600 and lines[-1].startswith(f"{shown}\t")
    notes = [d["text"] for e, k, d in h.records if (e, k) == (FS, "notice")]
    assert notes == [
        f"[Partial view, cut to fit the read limit: {ws / 'wide.txt'} shows lines 1-{shown} "
        f"of 1701 (63750 tokens; the cap is 25000). For the next part call Read with "
        f"offset={shown + 1} limit={shown}, or search the file for the section you need. "
        "allow_large: true returns more of it; use that only when every part matters or the "
        "user wants the file entire. What you need may lie past this part; check before "
        "relying on it alone.]"]
    assert h.fs.reads[str(ws / "wide.txt")].partial
    # A window asked for explicitly is still refused, as before.
    assert (await h.call(ReadTool(), file_path="wide.txt", limit=1000)).is_error


async def test_a_page_of_one_long_line_is_cut_by_characters(h: Harness, ws: Path) -> None:
    (ws / "min.js").write_text("z" * 200_000)
    out = await h.call(ReadTool(), file_path="min.js")
    assert not out.is_error
    assert text_of(out).startswith("1\tzzz") and len(text_of(out)) < 100_000
    [note] = [d["text"] for e, k, d in h.records if (e, k) == (FS, "notice")]
    assert "lines are too long to page by line" in note


async def test_a_cut_read_does_not_count_as_read_for_an_edit(h: Harness, ws: Path) -> None:
    from noeta.plugins.fs import EditTool
    h.approved = True  # asked about: the read-first check holds
    (ws / "wide.txt").write_text("".join("y" * 150 + "\n" for _ in range(1700)))
    await h.call(ReadTool(), file_path="wide.txt")
    out = await h.call(EditTool(), file_path="wide.txt", old_string="y\n", new_string="z\n",
                       replace_all=True)
    assert out.is_error and "this conversation has not read" in text_of(out)


async def test_read_checks_a_large_window_with_the_exact_count(h: Harness, ws: Path) -> None:
    (ws / "wide.txt").write_text(("y" * 150 + "\n") * 3000)
    counted: list[int] = []

    async def count(text: str) -> int | None:
        counted.append(len(text))
        return 20_000  # under the ceiling, though the estimate (37750) is over

    ctx = h.ctx()
    ctx.count_tokens = count
    out = await ReadTool().run({"file_path": "wide.txt", "limit": 1000}, ctx)
    assert not out.is_error and counted == [151 * 1000 - 1]
    small = h.ctx()
    small.count_tokens = count
    # Under a quarter of the ceiling the estimate stands: no count request.
    assert not (await ReadTool().run({"file_path": "wide.txt", "limit": 100}, small)).is_error
    assert len(counted) == 1


async def test_read_json_counts_two_chars_per_token(h: Harness, ws: Path) -> None:
    (ws / "d.json").write_text(("y" * 150 + "\n") * 600)
    out = await h.call(ReadTool(), file_path="d.json", limit=600)
    assert out.is_error
    assert "-token limit for one read" in text_of(out)
    (ws / "d.txt").write_text(("y" * 150 + "\n") * 600)
    assert not (await h.call(ReadTool(), file_path="d.txt", limit=600)).is_error


async def test_read_refuses_a_huge_single_line_without_buffering_it(ws: Path) -> None:
    h = Harness(env=_NoWholeReads(str(ws)))
    (ws / "min.js").write_bytes(b"x" * (8 * 1024 * 1024) + b"\nnext\n")
    out = await h.call(ReadTool(), file_path="min.js", limit=2)
    assert out.is_error
    assert "-token limit for one read" in text_of(out)
    ok = await h.call(ReadTool(), file_path="min.js", offset=2, limit=1)
    assert text_of(ok) == "2\tnext"


async def test_read_numbering_splits_on_newline_only(h: Harness, ws: Path) -> None:
    (ws / "ff.py").write_text("a = 1\n\x0c\nb = 2 c = 3\x1cd\nTARGET = 3\n")
    read = await h.call(ReadTool(), file_path="ff.py", offset=4, limit=1)
    assert text_of(read) == "4\tTARGET = 3"


async def test_an_unchanged_range_is_not_sent_twice(h: Harness, ws: Path) -> None:
    target = ws / "a.txt"
    target.write_text("one\ntwo\n")
    tool = ReadTool()

    async def read(**input: Any) -> str:
        ctx = h.ctx()
        ctx.by_model = True
        return text_of(await tool.run({"file_path": "a.txt", **input}, ctx))

    assert (await read()).startswith("1\tone")
    assert (await read(offset=0)).startswith("File unchanged since you last read this range")
    # An extension's own read (a compaction restoring files) gets the content.
    assert text_of(await h.call(tool, file_path="a.txt")).startswith("1\tone")
    assert await read(limit=1) == "1\tone"
    target.write_text("one\ntwo\nthree\n")
    os.utime(target, (1, 1))
    assert await read(limit=1) == "1\tone"


async def test_an_unchanged_range_must_repeat_the_same_arguments(h: Harness, ws: Path) -> None:
    # Claude Code compares the arguments as given: no limit and limit=2000 differ.
    (ws / "a.txt").write_text("one\ntwo\n")
    tool = ReadTool()

    async def read(**input: Any) -> str:
        ctx = h.ctx()
        ctx.by_model = True
        return text_of(await tool.run({"file_path": "a.txt", **input}, ctx))

    assert (await read()).startswith("1\tone")
    assert (await read(limit=2000)).startswith("1\tone")
    assert (await read(limit=2000)).startswith("File unchanged")


async def test_an_unchanged_notebook_is_not_sent_twice(h: Harness, ws: Path) -> None:
    (ws / "n.ipynb").write_text(
        '{"cells": [{"id": "a", "cell_type": "code", "source": "x = 1", "outputs": []}],'
        ' "metadata": {}, "nbformat": 4, "nbformat_minor": 5}')
    tool = ReadTool()

    async def read() -> str:
        ctx = h.ctx()
        ctx.by_model = True
        return text_of(await tool.run({"file_path": "n.ipynb"}, ctx))

    assert (await read()).startswith('<cell id="a">')
    assert (await read()).startswith("File unchanged")


def test_a_compaction_forgets_read_ranges() -> None:
    from noeta.plugins.fs.state import reduce_fs
    st = reduce_fs(None, "read", {"path": "/a", "digest": "d", "mtime": 1.0, "range": [1, 9]})
    assert st.reads["/a"].range == [1, 9]
    st = reduce_fs(st, "forget_ranges", {})
    assert st.reads["/a"].range is None and st.reads["/a"].digest == "d"


def test_read_schema() -> None:
    schema = ReadTool().input_schema
    props = schema["properties"]
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert props["offset"]["minimum"] == 0 and props["offset"]["maximum"] == 2**53 - 1
    assert props["limit"]["exclusiveMinimum"] == 0 and "minimum" not in props["limit"]
    assert props["allow_large"]["type"] == "boolean"
    assert all(p["description"] for p in props.values())
