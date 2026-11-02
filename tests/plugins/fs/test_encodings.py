"""Files that are not plain UTF-8, as Claude Code handles them: UTF-16LE
behind its byte-order mark is read decoded and written back as UTF-16LE; a
UTF-8 byte-order mark is kept; a UTF-16BE file, one with a NUL byte early on,
or one that is not UTF-8 is shown by ``Read`` with a note and refused by
``Edit``, ``Write`` and ``NotebookEdit``."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from noeta.core.tool import Tool
from noeta.plugins.fs import EditTool, NotebookEditTool, ReadTool, WriteTool
from noeta.plugins.fs._encoding import (
    NUL_PROBE_BYTES,
    NotText,
    decode,
    encode,
    not_text,
    read_note,
    refusal,
)
from noeta.plugins.fs.state import FS, KIND_READ, reduce_fs
from tests.plugins.conftest import Harness, text_of

UTF16 = b"\xff\xfe" + "first line\r\nzweite Zeile ü\r\n".encode("utf-16-le")


async def test_utf16le_is_read_decoded_and_edited_in_place(h: Harness, ws: Path) -> None:
    (ws / "w.txt").write_bytes(UTF16)
    out = await h.call(ReadTool(), file_path="w.txt")
    assert not out.is_error and "not_text" not in out.meta
    assert text_of(out) == "1\tfirst line\n2\tzweite Zeile ü\n3\t"
    out = await h.call(EditTool(), file_path="w.txt", old_string="first line\nzweite",
                       new_string="1st line\nsecond")
    assert not out.is_error, text_of(out)
    # The byte-order mark, the encoding and the CRLF line ends all stay.
    assert (ws / "w.txt").read_bytes() == b"\xff\xfe" + (
        "1st line\r\nsecond Zeile ü\r\n".encode("utf-16-le"))


async def test_write_over_utf16le_keeps_its_encoding(h: Harness, ws: Path) -> None:
    (ws / "w.txt").write_bytes(UTF16)
    assert not (await h.call(ReadTool(), file_path="w.txt")).is_error
    out = await h.call(WriteTool(), file_path="w.txt", content="new\n")
    assert not out.is_error, text_of(out)
    assert (ws / "w.txt").read_bytes() == b"\xff\xfe" + "new\n".encode("utf-16-le")
    # The write counts as read: a second edit needs no new Read.
    out = await h.call(EditTool(), file_path="w.txt", old_string="new", new_string="newer")
    assert not out.is_error, text_of(out)
    assert (ws / "w.txt").read_bytes() == b"\xff\xfe" + "newer\n".encode("utf-16-le")


async def test_a_utf8_byte_order_mark_is_kept_by_edit(h: Harness, ws: Path) -> None:
    (ws / "b.txt").write_bytes(b"\xef\xbb\xbfalpha\n")
    out = await h.call(ReadTool(), file_path="b.txt")
    assert text_of(out) == "1\talpha\n2\t"
    assert not (await h.call(EditTool(), file_path="b.txt", old_string="alpha",
                             new_string="beta")).is_error
    assert (ws / "b.txt").read_bytes() == b"\xef\xbb\xbfbeta\n"


@pytest.mark.parametrize(("raw", "reason"), [
    (b"\xfe\xff\x00a\x00\n", "utf16be"),
    (b"text\x00more\n", "nul"),
    (b"caf\xe9\n", "not_utf8"),
    # Binary data that happens to start with the UTF-16LE mark: an odd
    # length, and a lone surrogate.
    (b"\xff\xfea\x00\n\x00\x07", "not_utf16"),
    (b"\xff\xfea\x00\x00\xd8b\x00", "not_utf16"),
])
async def test_a_file_that_is_not_text_is_shown_and_refused(
        h: Harness, ws: Path, raw: bytes, reason: NotText) -> None:
    (ws / "x.txt").write_bytes(raw)
    out = await h.call(ReadTool(), file_path="x.txt")
    assert not out.is_error and out.meta["not_text"] == reason
    assert text_of(out).endswith("\n\n" + read_note(reason))
    calls: list[tuple[Tool, dict[str, Any]]] = [
        (EditTool(), {"old_string": "a", "new_string": "b"}),
        (WriteTool(), {"content": "replaced\n"})]
    for tool, args in calls:
        out = await h.call(tool, file_path="x.txt", **args)
        assert out.is_error
        assert text_of(out) == f"{tool.name}: {refusal(reason)}"
    assert (ws / "x.txt").read_bytes() == raw


async def test_a_notebook_that_is_not_text_is_refused(h: Harness, ws: Path) -> None:
    raw = b'{"cells": [], "x": "\xe9"}'
    (ws / "n.ipynb").write_bytes(raw)
    h.state[FS] = reduce_fs(None, KIND_READ, {"path": str(ws / "n.ipynb"),
                                              "digest": hashlib.sha256(raw).hexdigest()})
    out = await h.call(NotebookEditTool(), notebook_path="n.ipynb", new_source="x",
                       edit_mode="insert", cell_type="code")
    assert text_of(out) == f"NotebookEdit: {refusal('not_utf8')}"


async def test_a_nul_past_the_probe_leaves_the_file_text(h: Harness, ws: Path) -> None:
    (ws / "late.txt").write_bytes(b"a" * NUL_PROBE_BYTES + b"\x00\n")
    out = await h.call(ReadTool(), file_path="late.txt")
    assert not out.is_error and "not_text" not in out.meta


def test_the_rules_on_whole_byte_strings() -> None:
    assert not_text(b"") is None and not_text(b"\xff\xfe\x00\xd8") == "not_utf16"
    assert not_text(b"\xff\xfea\x00") is None
    assert not_text(b"\xef\xbb\xbfok") is None
    assert not_text(b"a" * NUL_PROBE_BYTES + b"\x00") is None
    assert decode(UTF16) == "﻿first line\r\nzweite Zeile ü\r\n"
    assert encode("x", b"") == b"x"
    assert encode("x", b"\xef\xbb\xbfy") == b"\xef\xbb\xbfx"
    assert encode("﻿x", b"\xef\xbb\xbfy") == b"\xef\xbb\xbfx"
    assert encode("", b"\xff\xfe") == b""
