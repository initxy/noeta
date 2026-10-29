"""Claude Code's argument placeholders in a skill or a markdown command's
body: what typing ``/<name> <args>`` (or the ``Skill`` tool's ``args``)
fills in."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Sequence

__all__ = ["ARGUMENTS_PLACEHOLDER", "MAX_ARGUMENT_NAMES", "MAX_ARGUMENT_NAME_LEN",
           "substitute_arguments"]

#: Claude Code's argument placeholder, filled by :func:`substitute_arguments`.
ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"

#: Stands in for an escaped ``\$`` while placeholders are filled.
_ESCAPED_DOLLAR = "￿"


def _split_arguments(args: str) -> list[str]:
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


#: Claude Code's limits on ``arguments`` names: how many, and how long each.
MAX_ARGUMENT_NAMES = 64
MAX_ARGUMENT_NAME_LEN = 64


def substitute_arguments(body: str, args: str | None, names: Sequence[str] = (),
                         escape: Callable[[str], str] | None = None) -> str:
    """``body`` with its argument placeholders filled the way Claude Code
    fills them: ``$ARGUMENTS`` is the whole argument string,
    ``$ARGUMENTS[N]`` and ``$N`` the N-th (from 0) shell-quoted word (left as
    written when there is no such word), ``$<name>`` the word at that name's
    place in ``names`` (the skill's ``arguments``), and ``\\$`` keeps a
    ``$`` literal. ``escape`` transforms each value filled in.
    When arguments are given but no placeholder took them, they follow the
    body on an ``ARGUMENTS:`` line. ``None`` (no arguments passed) leaves the
    body as written."""
    if args is None:
        return body
    words: list[str] | None = None

    def word(i: int) -> str | None:
        nonlocal words
        words = words if words is not None else _split_arguments(args)
        return words[i] if i < len(words) else None

    used = False

    def fill(value: str) -> str:
        nonlocal used
        used = True
        if escape is not None:
            value = escape(value)
        return value.replace("$", _ESCAPED_DOLLAR)

    kept = [(n, i) for i, n in enumerate(names) if n and len(n) <= MAX_ARGUMENT_NAME_LEN]
    # Longest first, so ``$file`` never eats the start of ``$filename``.
    named = sorted(kept[:MAX_ARGUMENT_NAMES], key=lambda item: -len(item[0]))
    escapable = "|".join([r"\d", "ARGUMENTS",
                          *(re.escape(n) + r"(?![\[\w])" for n, _ in named)])
    text = body.replace(_ESCAPED_DOLLAR, "�")
    text = re.sub(rf"(?<!\\)\\\$(?={escapable})", _ESCAPED_DOLLAR, text)
    for name, index in named:
        def by_name(_m: re.Match[str], index: int = index) -> str:
            return fill(word(index) or "")
        text = re.sub(r"\$" + re.escape(name) + r"(?![\[\w])", by_name, text)

    def indexed(m: re.Match[str]) -> str:
        found = word(int(m.group(1)))
        return m.group(0).replace("$", _ESCAPED_DOLLAR, 1) if found is None else fill(found)

    def positional(m: re.Match[str]) -> str:
        found = word(int(m.group(1)))
        return m.group(0) if found is None else fill(found)

    text = re.sub(r"\$ARGUMENTS\[(\d+)\]", indexed, text)
    text = re.sub(r"\$(\d+)(?!\w)", positional, text)
    text = re.sub(re.escape(ARGUMENTS_PLACEHOLDER), lambda _m: fill(args), text)
    if not used and args:
        text += f"\nARGUMENTS: {args}"
    return text.replace(_ESCAPED_DOLLAR, "$")
