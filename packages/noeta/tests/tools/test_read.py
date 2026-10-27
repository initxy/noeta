"""``Read``: ``cat -n`` output, offset / limit, size and token caps, images,
the read-first record, and unfenced path resolution (ported from 0.6)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from noeta.execenv.local import LocalExecEnv
from noeta.tools.fs_state import FS
from noeta.tools.grep import GrepTool
from noeta.tools.read import IMAGE_MAX_BYTES, ReadTool

from .conftest import Harness, images_of, text_of


async def test_read_small_file_cat_n_form(h: Harness, ws: Path) -> None:
    (ws / "hello.txt").write_text("hi\nthere\n")
    out = await h.call(ReadTool(), file_path="hello.txt")
    assert not out.is_error
    assert text_of(out) == "     1\thi\n     2\tthere"


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
    text = text_of(out)
    assert text.startswith("     3\tline3\n     4\tline4")
    assert "Showing lines 3-4 of 10 total lines" in text
    assert "offset=5" in text
    assert text.endswith(
        "\n\n(Showing lines 3-4 of 10 total lines. Use offset=5 to continue reading.)"
    )


async def test_read_empty_file_warns(h: Harness, ws: Path) -> None:
    (ws / "empty.txt").write_text("")
    out = await h.call(ReadTool(), file_path="empty.txt")
    assert not out.is_error
    text = text_of(out)
    assert text == "(The file exists but has empty contents.)"
    assert "<system-reminder" not in text


async def test_read_offset_past_end_warns_with_line_count(h: Harness, ws: Path) -> None:
    (ws / "short.txt").write_text("one\ntwo\n")
    out = await h.call(ReadTool(), file_path="short.txt", offset=10)
    assert not out.is_error
    assert text_of(out) == (
        "(The file has only 2 lines, fewer than the requested offset 10. "
        "Nothing was read.)"
    )


async def test_read_refuses_a_window_past_the_token_ceiling(h: Harness, ws: Path) -> None:
    (ws / "big.txt").write_text(("x" * 1999 + "\n") * 1500)
    out = await h.call(ReadTool(), file_path="big.txt", limit=1500)
    assert out.is_error
    assert "exceeds maximum allowed tokens (25000)" in text_of(out)
    assert h.records == []  # a refused read records nothing


async def test_read_medium_file_fits_in_full(h: Harness, ws: Path) -> None:
    (ws / "mid.txt").write_text("".join(f"line {i}\n" for i in range(1500)))
    out = await h.call(ReadTool(), file_path="mid.txt")
    text = text_of(out)
    assert "Showing lines" not in text
    assert text.endswith("  1500\tline 1499")


async def test_read_default_limit_is_2000_lines(h: Harness, ws: Path) -> None:
    (ws / "long.txt").write_text("".join(f"{i}\n" for i in range(2500)))
    text = text_of(await h.call(ReadTool(), file_path="long.txt"))
    assert "Showing lines 1-2000 of 2500 total lines. Use offset=2001" in text


async def test_read_clips_overlong_line(h: Harness, ws: Path) -> None:
    (ws / "wide.txt").write_text("x" * 5000 + "\nshort\n")
    lines = text_of(await h.call(ReadTool(), file_path="wide.txt")).splitlines()
    assert lines[0].endswith("… [line truncated]")
    assert len(lines[0]) < 2200
    assert lines[1] == "     2\tshort"


async def test_read_clips_overlong_line_crlf(h: Harness, ws: Path) -> None:
    (ws / "wide.txt").write_bytes(b"y" * 5000 + b"\r\nnext\r\n")
    lines = text_of(await h.call(ReadTool(), file_path="wide.txt")).splitlines()
    assert "… [line truncated]" in lines[0]
    assert lines[1] == "     2\tnext"


async def test_read_crlf_lines_drop_the_carriage_return(h: Harness, ws: Path) -> None:
    (ws / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    assert text_of(await h.call(ReadTool(), file_path="crlf.txt")) == "     1\ta\n     2\tb"


async def test_read_last_line_without_newline_counts(h: Harness, ws: Path) -> None:
    (ws / "n.txt").write_bytes(b"a\nb")
    assert text_of(await h.call(ReadTool(), file_path="n.txt")) == "     1\ta\n     2\tb"


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


async def test_read_missing_file(h: Harness) -> None:
    out = await h.call(ReadTool(), file_path="nope.txt")
    assert out.is_error
    assert text_of(out) == "Read: not a file: 'nope.txt'"


async def test_read_reaches_outside_the_workspace(h: Harness, ws: Path) -> None:
    outside = ws.parent / "elsewhere.txt"
    outside.write_text("outside\n")
    out = await h.call(ReadTool(), file_path=str(outside))
    assert not out.is_error
    assert "outside" in text_of(out)
    assert out.meta["path"] == str(outside)  # shown absolute


async def test_read_binary_rejected(h: Harness, ws: Path) -> None:
    (ws / "blob.bin").write_bytes(b"\x00\x01\x02")
    out = await h.call(ReadTool(), file_path="blob.bin")
    assert out.is_error
    assert text_of(out) == "Read: 'blob.bin' is not utf-8 text"


async def test_read_stray_invalid_bytes_decoded_leniently(h: Harness, ws: Path) -> None:
    (ws / "legacy.txt").write_bytes(b"caf\xe9 latte\n")
    out = await h.call(ReadTool(), file_path="legacy.txt")
    assert not out.is_error
    text = text_of(out)
    assert "caf� latte" in text
    assert "(Non-utf8 bytes were replaced with U+FFFD.)" in text


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


async def test_read_image_surfaces_for_vision(h: Harness, ws: Path) -> None:
    (ws / "shot.png").write_bytes(_PNG)
    out = await h.call(ReadTool(), file_path="shot.png")
    assert not out.is_error
    [img] = images_of(out)
    assert img.media_type == "image/png"
    assert img.data == _PNG
    assert text_of(out) == f"Read image shot.png (image/png, {len(_PNG)} bytes)"
    assert h.records == []  # images are not recorded for read-first


async def test_read_image_detected_by_content_not_extension(h: Harness, ws: Path) -> None:
    (ws / "shot.dat").write_bytes(_PNG)
    out = await h.call(ReadTool(), file_path="shot.dat")
    assert images_of(out)


async def test_read_other_image_kinds(h: Harness, ws: Path) -> None:
    kinds = {
        "a.jpg": (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        "a.gif": (b"GIF89a" + b"\x00" * 8, "image/gif"),
        "a.webp": (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    }
    for name, (body, media_type) in kinds.items():
        (ws / name).write_bytes(body)
        [img] = images_of(await h.call(ReadTool(), file_path=name))
        assert img.media_type == media_type


async def test_read_image_over_limit_degrades(h: Harness, ws: Path) -> None:
    (ws / "huge.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (IMAGE_MAX_BYTES + 1))
    out = await h.call(ReadTool(), file_path="huge.png")
    assert out.is_error
    assert text_of(out) == (
        f"Read: 'huge.png' is a PNG image of {IMAGE_MAX_BYTES + 9} bytes, over "
        "the 5MB inline limit — crop/resize it smaller before reading"
    )


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
    text = text_of(out)
    assert text.startswith("250000\tline 250000\n250001\tline 250001\n250002\tline 250002")
    assert "of 300000 total lines" in text
    assert h.fs.digest(str(ws / "big.log")) == hashlib.sha256(body).hexdigest()


async def test_read_refuses_a_whole_file_over_256kb(h: Harness, ws: Path) -> None:
    (ws / "wide.txt").write_text(("y" * 150 + "\n") * 3000)
    out = await h.call(ReadTool(), file_path="wide.txt")
    assert out.is_error
    assert text_of(out) == (
        "Read: File content (442.4KB) exceeds maximum allowed size (256KB). "
        "Use offset and limit parameters to read specific portions of the "
        "file, or search for specific content instead of reading the whole "
        "file."
    )


async def test_read_refuses_more_than_25000_tokens(h: Harness, ws: Path) -> None:
    (ws / "wide.txt").write_text(("y" * 150 + "\n") * 3000)
    out = await h.call(ReadTool(), file_path="wide.txt", limit=1000)
    assert out.is_error
    assert text_of(out) == (
        "Read: File content (37750 tokens) exceeds maximum allowed tokens "
        "(25000). Use offset and limit parameters to read specific portions "
        "of the file, or search for specific content instead of reading the "
        "whole file."
    )
    ok = await h.call(ReadTool(), file_path="wide.txt", limit=500)
    assert not ok.is_error


async def test_read_json_counts_two_chars_per_token(h: Harness, ws: Path) -> None:
    (ws / "d.json").write_text(("y" * 150 + "\n") * 600)
    out = await h.call(ReadTool(), file_path="d.json", limit=600)
    assert out.is_error
    assert "exceeds maximum allowed tokens" in text_of(out)
    (ws / "d.txt").write_text(("y" * 150 + "\n") * 600)
    assert not (await h.call(ReadTool(), file_path="d.txt", limit=600)).is_error


async def test_read_clips_a_huge_single_line_without_buffering_it(ws: Path) -> None:
    h = Harness(env=_NoWholeReads(str(ws)))
    (ws / "min.js").write_bytes(b"x" * (8 * 1024 * 1024) + b"\nnext\n")
    out = await h.call(ReadTool(), file_path="min.js", limit=2)
    lines = text_of(out).splitlines()
    assert lines[0].endswith("… [line truncated]")
    assert lines[1] == "     2\tnext"


async def test_read_numbering_matches_grep_across_odd_separators(
    h: Harness, ws: Path
) -> None:
    (ws / "ff.py").write_text("a = 1\n\x0c\nb = 2 c = 3\x1cd\nTARGET = 3\n")
    grep = await h.call(GrepTool(), pattern="TARGET", output_mode="content", **{"-n": True})
    assert "ff.py:4:TARGET = 3" in text_of(grep)
    read = await h.call(ReadTool(), file_path="ff.py", offset=4, limit=1)
    assert text_of(read).startswith("     4\tTARGET = 3")
