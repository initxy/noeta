"""Line breaks inside CJK text. Textual wraps only at whitespace, so a run of
Chinese or Japanese (which has none) is one long "word": a line breaks early
at the last space before it and the run is folded wherever it overflows.
Here each CJK character is a place to break, as a terminal or browser does,
with an opening bracket kept with what follows it and closing punctuation
(，。、）…) with what precedes it, so neither starts or ends a line alone.
Applies to every wrapped text of the app: replies, notices and the prompt."""

from __future__ import annotations

import re

from rich import _wrap as rich_wrap
from textual import _wrap

_CJK = ("[\u2e80-\u2fff\u3040-\u30ff\u3100-\u31ff\u3400-\u4dbf\u4e00-\u9fff"
        "\uac00-\ud7af\uf900-\ufaff\U00020000-\U0002fa1f]")
#: Punctuation that never starts a line.
_CLOSE = ("[\u3001\u3002\u3009\u300b\u300d\u300f\u3011"
          "\uff01\uff09\uff0c\uff1a\uff1b\uff1f\u201d\u2019\u2026]")
#: Punctuation that never ends a line.
_OPEN = "[\u3008\u300a\u300c\u300e\u3010\uff08\u201c\u2018]"

#: What stays on one line: a CJK character with the brackets and punctuation
#: around it, else a word of anything else; any other character alone, so
#: every text is consumed.
_WORD = rf"(?:{_OPEN}*{_CJK}{_CLOSE}*|(?:(?!{_CJK}|{_OPEN})\S)+{_CLOSE}*|\S)"
#: Textual's chunks (the prompt's lines): a word and the spaces after it, or spaces.
CHUNK = re.compile(rf"{_WORD}\s*|\s+")
#: Rich's words (every other text: replies, notices): spaces, a word, spaces.
WORD = re.compile(rf"\s*{_WORD}\s*")


def install() -> None:
    """Make Textual's and Rich's wrapping break inside CJK text."""
    _wrap.re_chunk = CHUNK
    rich_wrap.re_word = WORD
