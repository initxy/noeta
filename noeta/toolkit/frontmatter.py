"""Splitting a markdown file's leading frontmatter from its body, one way for
every file noeta reads with one (agent files, skills, commands, rules,
memories, Claude Code plugins).

The file opens with a ``---`` line and the frontmatter runs to the next
``---`` line; trailing blanks on either line are allowed, a UTF-8 byte order
mark ahead of the first is skipped, and lines may end in CRLF. What the
frontmatter holds is each caller's to read (YAML, or the skills' own strict
reader).
"""

from __future__ import annotations

import re

_OPEN = re.compile(r"---[ \t]*\r?\n")
_CLOSE = re.compile(r"^---[ \t]*\r?$", re.M)
_BOM = "﻿"


def opens(text: str) -> bool:
    """Whether ``text`` starts with a frontmatter's opening ``---`` line."""
    return _OPEN.match(text.removeprefix(_BOM)) is not None


def split(text: str) -> tuple[str | None, str]:
    """``(frontmatter, body)``: the lines between the two ``---`` lines,
    LF-joined, and the text after the closing one, unchanged. With no
    frontmatter (no opening line, or no closing one) it is ``None`` and the
    body is all of ``text`` but a byte order mark."""
    text = text.removeprefix(_BOM)
    opening = _OPEN.match(text)
    if opening is None:
        return None, text
    closing = _CLOSE.search(text, opening.end())
    if closing is None:
        return None, text
    head = text[opening.end():closing.start()].replace("\r\n", "\n")
    body = text[closing.end():]
    return head.removesuffix("\n"), body.removeprefix("\n")
