"""Path-glob matching shared by ``Glob`` and ``Write``'s path whitelist.

pathlib-style segments — ``*`` / ``?`` / ``[...]`` stay inside one path
segment, ``**`` spans zero or more — plus ``{a,b}`` brace alternatives, the
dialect ``Grep``'s ripgrep ``glob`` already speaks.
"""

from __future__ import annotations

import fnmatch
from typing import Sequence


__all__ = ["compile_glob", "glob_matches"]

#: Ceiling on the alternatives one pattern may expand to — a guard against
#: ``{a,b}{c,d}…`` blowing up, far above any hand-written pattern.
_MAX_BRACE_EXPANSIONS = 256


def _split_top_level(body: str) -> list[str]:
    """``body`` split on commas outside any nested ``{...}``."""
    parts: list[str] = []
    depth = 0
    last = 0
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(body[last:i])
            last = i + 1
    parts.append(body[last:])
    return parts


def expand_braces(pattern: str) -> list[str]:
    """Every alternative of ``pattern``'s ``{a,b}`` groups (nested allowed).

    A group without a top-level comma (``{x}``) or an unbalanced brace stays
    literal, as in ripgrep and bash.
    """
    search_from = 0
    while True:
        open_at = pattern.find("{", search_from)
        if open_at < 0:
            return [pattern]
        depth = 0
        close_at = -1
        for i in range(open_at, len(pattern)):
            if pattern[i] == "{":
                depth += 1
            elif pattern[i] == "}":
                depth -= 1
                if depth == 0:
                    close_at = i
                    break
        if close_at < 0:
            return [pattern]
        alts = _split_top_level(pattern[open_at + 1 : close_at])
        if len(alts) > 1:
            head, tail = pattern[:open_at], pattern[close_at + 1 :]
            out: list[str] = []
            for alt in alts:
                for expanded in expand_braces(head + alt + tail):
                    out.append(expanded)
                    if len(out) > _MAX_BRACE_EXPANSIONS:
                        raise ValueError(
                            f"glob {pattern!r} expands to more than "
                            f"{_MAX_BRACE_EXPANSIONS} alternatives"
                        )
            return out
        search_from = open_at + 1


def _segments(pattern: str) -> tuple[str, ...]:
    """Split a relative glob pattern into path segments, dropping no-ops."""
    return tuple(seg for seg in pattern.split("/") if seg not in ("", "."))


def _match(parts: Sequence[str], pats: Sequence[str]) -> bool:
    if not pats:
        return not parts
    head, rest = pats[0], pats[1:]
    if head == "**":
        return any(_match(parts[i:], rest) for i in range(len(parts) + 1))
    return (
        bool(parts)
        and fnmatch.fnmatchcase(parts[0], head)
        and _match(parts[1:], rest)
    )


def compile_glob(pattern: str) -> tuple[tuple[str, ...], ...]:
    """``pattern`` pre-split into its brace alternatives' segments.

    Raises ``ValueError`` for a pattern with too many alternatives."""
    return tuple(_segments(p) for p in expand_braces(pattern))


def glob_matches(
    parts: Sequence[str], compiled: tuple[tuple[str, ...], ...]
) -> bool:
    """Whether the relative path ``parts`` matches any compiled alternative."""
    return any(_match(parts, pats) for pats in compiled)
