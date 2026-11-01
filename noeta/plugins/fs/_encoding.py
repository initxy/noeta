"""Which files the file tools treat as text, and in what encoding (Claude
Code's rules).

A file that starts with the UTF-16LE byte-order mark (``FF FE``) is
UTF-16LE text: ``Read`` shows it decoded and ``Edit`` / ``Write`` write it
back as UTF-16LE behind its mark. Anything else is read as UTF-8, and a
UTF-8 byte-order mark is kept on a write. A file is *not text* when it
starts with the UTF-16BE mark (``FE FF``), has a NUL byte in its first
:data:`NUL_PROBE_BYTES` bytes, or is not valid UTF-8: ``Read`` still shows
it (undecodable bytes as U+FFFD) with :func:`read_note` after it, and
``Edit`` / ``Write`` / ``NotebookEdit`` refuse it with :func:`refusal`,
since they never convert an encoding. Line endings are ``Edit``'s own
business.
"""

from __future__ import annotations

import codecs
from typing import Literal

__all__ = [
    "NUL_PROBE_BYTES",
    "UTF8_BOM",
    "UTF16BE_BOM",
    "UTF16LE_BOM",
    "Encoding",
    "NotText",
    "decode",
    "encode",
    "encoding_of",
    "not_text",
    "read_note",
    "refusal",
]

Encoding = Literal["utf-8", "utf-16-le"]
NotText = Literal["utf16be", "nul", "not_utf8"]

UTF8_BOM = b"\xef\xbb\xbf"
UTF16LE_BOM = b"\xff\xfe"
UTF16BE_BOM = b"\xfe\xff"
#: A NUL byte this early marks a file as not text.
NUL_PROBE_BYTES = 8000

_WHAT: dict[NotText, str] = {
    "not_utf8": ("holds bytes that do not decode as UTF-8: it is in another encoding "
                 "(Windows-1252, Latin-1, GBK, Shift-JIS and the like) or it is binary"),
    "nul": (f"has a zero byte in its first {NUL_PROBE_BYTES:,} bytes: it is binary, UTF-16 or "
            "UTF-32 text without a byte-order mark, or text with NUL characters in it"),
    "utf16be": ("is big-endian UTF-16 (its first two bytes are FE FF), an encoding the file "
                "tools cannot write"),
}


def encoding_of(head: bytes) -> Encoding:
    """The encoding a file whose first bytes are ``head`` is read in."""
    return "utf-16-le" if head.startswith(UTF16LE_BOM) else "utf-8"


def not_text(raw: bytes) -> NotText | None:
    """Why the file holding ``raw`` is not text, or ``None`` when it is."""
    if encoding_of(raw) != "utf-8":
        return None
    if raw.startswith(UTF16BE_BOM):
        return "utf16be"
    if b"\0" in raw[:NUL_PROBE_BYTES]:
        return "nul"
    try:
        codecs.decode(raw, "utf-8")
    except UnicodeDecodeError:
        return "not_utf8"
    return None


def decode(raw: bytes) -> str:
    """``raw`` as text in its encoding, any byte-order mark kept as U+FEFF
    and undecodable bytes as U+FFFD."""
    return raw.decode(encoding_of(raw), "replace")


def encode(text: str, like: bytes) -> bytes:
    """``text`` in the encoding of the file that held ``like``, behind that
    file's byte-order mark when it had one and ``text`` does not."""
    encoding = encoding_of(like)
    mark = UTF16LE_BOM if encoding == "utf-16-le" else UTF8_BOM
    if like.startswith(mark) and text and not text.startswith("﻿"):
        text = "﻿" + text
    return text.encode(encoding, "surrogatepass")


def read_note(reason: NotText) -> str:
    """``Read``'s note after a file that is not text."""
    shown = " Bytes that could not be decoded are shown as U+FFFD." \
        if reason == "not_utf8" else ""
    return (f"[Not text to the file tools: this file {_WHAT[reason]}.{shown} Edit and Write "
            "will not change it. Change it with a shell command that keeps its encoding, or "
            "ask the user whether it should become UTF-8.]")


def refusal(reason: NotText) -> str:
    """``Edit`` / ``Write`` / ``NotebookEdit``'s error for a file that is
    not text."""
    return (f"Nothing was written: this file {_WHAT[reason]}. The file tools write UTF-8 "
            "(or UTF-16LE that starts with its byte-order mark) and never change a file's "
            "encoding by themselves. If it is text, make the change with a shell command that "
            "reads and writes the file in its own encoding (iconv, for example), or ask the "
            "user whether to convert it to UTF-8 first. To replace it with new UTF-8 content "
            "regardless, delete the file, then Write it.")
