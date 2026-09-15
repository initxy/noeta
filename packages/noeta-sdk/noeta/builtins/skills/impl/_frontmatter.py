"""Minimal strict frontmatter parser for SKILL.md, private to ``skills``.

The dialect is deliberately narrow: stdlib only (no pyyaml) and every value a
string — nothing is converted to a bool, a number or a list, and an inline
``allowed-tools: [Read, Bash]`` stays the literal ``"[Read, Bash]"`` — so parsing
the same disk state always yields the same fields and the composer's
``semi_stable`` segment stays cache-friendly.

What YAML treats as syntax rather than content is read as YAML does: a value
written as one quoted scalar (``"..."`` with its escapes, ``'...'`` with ``''``)
is unquoted, folding a quoted value that spans lines; a ``#`` comment line is
skipped, and so is a comment after a value (``name: x  # id``); a leading BOM is
ignored. A quoted value that is not one well-formed scalar (unterminated, or
followed by more text) stays verbatim with a warning.

Any key parses, not just the semantic ones, so a typo of a known key
(``descrption:``) degrades to metadata instead of failing the file. Only a
structural violation raises :class:`FrontmatterError`, and callers log and skip
it so one bad SKILL.md cannot take down the registry build.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional


__all__ = ["FrontmatterError", "KNOWN_KEYS", "parse"]


#: The keys :class:`~noeta.builtins.skills.impl.indexer.SkillIndexer` reads as
#: semantics rather than filing under opaque ``metadata``.
#: ``disable-model-invocation`` is Claude Code's own key and gates whether the
#: skill enters the model's menu at all, so leaving it in metadata made a skill
#: that declared it callable anyway — a semantic violation, not a gap.
KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "version",
        "priority",
        "disable-model-invocation",
    }
)

_LINE_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)[ \t]*:[ \t]*(.*?)[ \t]*$")

#: A YAML comment after a plain value: ``#`` preceded by whitespace.
_TRAILING_COMMENT = re.compile(r"[ \t]+#.*$")

#: What may follow a quoted scalar's closing quote: nothing, or a comment.
_AFTER_QUOTED = re.compile(r"^(?:[ \t]+#.*)?[ \t]*$", re.DOTALL)

#: The single-character escapes of a YAML double-quoted scalar.
_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}

#: The hex escapes of a YAML double-quoted scalar and their digit counts.
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}


class FrontmatterError(ValueError):
    """Raised when SKILL.md frontmatter is structurally unparseable."""


def parse(text: str) -> tuple[dict[str, str], str, list[str]]:
    """Parse one SKILL.md text blob into ``(fields, body, warnings)``.

    ``fields`` carries **every** parsed key, semantic and non-semantic alike;
    splitting the semantic keys from the opaque metadata is ``SkillIndexer``'s
    job. ``body`` has its line endings normalised to LF. A structural violation
    raises :class:`FrontmatterError` naming the rule it broke, so the caller
    can log something useful; warnings are non-fatal.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        raise FrontmatterError(
            "missing leading '---' delimiter at byte 0"
        )

    lines = [_strip_cr(line) for line in text.split("\n")]

    end_idx = _find_terminator(lines)
    if end_idx is None:
        raise FrontmatterError("missing terminating '---' delimiter")

    fields, warnings = _parse_lines(lines[1:end_idx])

    body = "\n".join(lines[end_idx + 1:])
    return fields, body, warnings


def _strip_cr(line: str) -> str:
    """Strip one trailing ``\\r`` so CRLF input collapses to LF on re-join."""
    return line[:-1] if line.endswith("\r") else line


def _find_terminator(lines: list[str]) -> int | None:
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return i
    return None


def _parse_lines(
    lines: Iterable[str],
) -> tuple[dict[str, str], list[str]]:
    materialized = list(lines)
    fields: dict[str, str] = {}
    warnings: list[str] = []
    i = 0
    while i < len(materialized):
        raw_line = materialized[i]
        if raw_line.strip() == "" or raw_line.startswith("#"):
            i += 1
            continue
        if _is_indented(raw_line):
            raise FrontmatterError(
                f"invalid frontmatter line: {raw_line!r}"
            )
        match = _LINE_PATTERN.match(raw_line)
        if match is None:
            raise FrontmatterError(
                f"invalid frontmatter line: {raw_line!r}"
            )
        key = match.group(1)
        value = match.group(2)
        i += 1

        continuation: list[str] = []
        while i < len(materialized):
            candidate = materialized[i]
            if _is_indented(candidate):
                continuation.append(candidate)
                i += 1
                continue
            if candidate.strip() == "" and continuation:
                continuation.append(candidate)
                i += 1
                continue
            break

        if value.startswith(('"', "'")):
            unquoted = _unquote(value, continuation)
            if unquoted is None:
                warnings.append(
                    f"frontmatter key {key!r}: malformed quoted value; "
                    "kept verbatim"
                )
                value = _normalise_value(value, continuation)
            else:
                value = unquoted
        else:
            value = _normalise_value(_TRAILING_COMMENT.sub("", value), continuation)
        if key in fields:
            warnings.append(
                f"duplicate frontmatter key {key!r}: using last value"
            )
        fields[key] = value
    return fields, warnings


def _unquote(first: str, continuation: list[str]) -> Optional[str]:
    """The content of a value written as one YAML quoted scalar, or ``None``
    when it is not one (unterminated, an unknown escape, or text after the
    closing quote other than a comment).

    A scalar that spans lines folds as YAML flow scalars do: each line is
    trimmed, a line break between two lines becomes a space, and every blank
    line becomes a newline. A double-quoted line ending in ``\\`` joins the
    next line with no space.
    """
    quote = first[0]
    text = _fold_flow_lines([first, *continuation], escapable=quote == '"')
    out: list[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        if quote == "'" and ch == "'":
            if text.startswith("''", i):
                out.append("'")
                i += 2
                continue
            return "".join(out) if _AFTER_QUOTED.match(text[i + 1 :]) else None
        if quote == '"' and ch == '"':
            return "".join(out) if _AFTER_QUOTED.match(text[i + 1 :]) else None
        if quote == '"' and ch == "\\":
            escape = text[i + 1 : i + 2]
            if escape in _ESCAPES:
                out.append(_ESCAPES[escape])
                i += 2
                continue
            width = _HEX_ESCAPES.get(escape)
            digits = text[i + 2 : i + 2 + width] if width else ""
            if not width or len(digits) != width or not all(
                c in "0123456789abcdefABCDEF" for c in digits
            ):
                return None
            code = int(digits, 16)
            # A lone surrogate or a code point past Unicode is no character:
            # it would fail the first UTF-8 encode downstream.
            if 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
                return None
            out.append(chr(code))
            i += 2 + width
            continue
        out.append(ch)
        i += 1
    return None


def _fold_flow_lines(lines: list[str], *, escapable: bool) -> str:
    """Join the physical lines of a quoted scalar the way YAML folds them."""
    text = lines[0].rstrip(" \t")
    blanks = 0
    for line in lines[1:]:
        stripped = line.strip(" \t")
        if stripped == "":
            blanks += 1
            continue
        if blanks:
            text += "\n" * blanks
        elif escapable and _ends_with_escape(text):
            text = text[:-1]
        else:
            text += " "
        text += stripped
        blanks = 0
    return text


def _ends_with_escape(text: str) -> bool:
    """Whether ``text`` ends in an unescaped backslash."""
    run = len(text) - len(text.rstrip("\\"))
    return run % 2 == 1


def _is_indented(line: str) -> bool:
    return line.startswith((" ", "\t"))


def _normalise_value(value: str, continuation: list[str]) -> str:
    if not continuation:
        return value

    dedented = _dedent_block(continuation)
    if value.startswith(">"):
        return _fold_block(dedented)
    if value.startswith("|"):
        return "\n".join(dedented)
    if value == "":
        return "\n".join(dedented)
    return "\n".join([value, *dedented])


def _dedent_block(lines: list[str]) -> list[str]:
    trimmed = list(lines)
    while trimmed and trimmed[-1].strip() == "":
        trimmed.pop()
    while trimmed and trimmed[0].strip() == "":
        trimmed.pop(0)
    if not trimmed:
        return []

    min_indent = min(
        _indent_width(line) for line in trimmed if line.strip() != ""
    )
    return [
        line[min_indent:] if line.strip() != "" else ""
        for line in trimmed
    ]


def _indent_width(line: str) -> int:
    i = 0
    while i < len(line) and line[i] in (" ", "\t"):
        i += 1
    return i


def _fold_block(lines: list[str]) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                paragraphs.append(" ".join(current))
                current = []
            paragraphs.append("")
            continue
        current.append(line.strip())
    if current:
        paragraphs.append(" ".join(current))

    while paragraphs and paragraphs[-1] == "":
        paragraphs.pop()
    return "\n".join(paragraphs)
