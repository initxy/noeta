"""Minimal strict frontmatter parser for SKILL.md, private to ``skills``.

The dialect is deliberately narrow: stdlib only (no pyyaml) and values captured
verbatim as opaque strings — an inline ``allowed-tools: [Read, Bash]`` stays the
literal ``"[Read, Bash]"`` — so parsing the same disk state always yields the
same fields and the composer's ``semi_stable`` segment stays cache-friendly.
Any key parses, not just the semantic ones, so a typo of a known key
(``descrption:``) degrades to metadata instead of failing the file. Only a
structural violation raises :class:`FrontmatterError`, and callers log and skip
it so one bad SKILL.md cannot take down the registry build.
"""

from __future__ import annotations

import re
from typing import Iterable


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
        if raw_line.strip() == "":
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

        value = _normalise_value(value, continuation)
        if key in fields:
            warnings.append(
                f"duplicate frontmatter key {key!r}: using last value"
            )
        fields[key] = value
    return fields, warnings


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
