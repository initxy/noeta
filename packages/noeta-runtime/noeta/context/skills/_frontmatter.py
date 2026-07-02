"""Minimal strict frontmatter parser for SKILL.md (private to skills/).

Issue 21. SKILL.md uses a deliberately narrow frontmatter dialect so
the parser stays in stdlib (no pyyaml) and so parsing the same disk
state always produces the same SkillDescription instances (a stable
rendering keeps the ``semi_stable`` segment cache-friendly).

Format (strict subset):

* File must begin with ``---\\n`` or ``---\\r\\n``.
* Terminator is a line containing exactly ``---``.
* Inside frontmatter every top-level non-blank line is ``key: value``.
* ``key`` matches ``^[A-Za-z][A-Za-z0-9_-]*$`` (a letter, then letters +
  digits + underscore + hyphen). The hyphen was added in 4.5-I5 so real
  public skills carrying keys like ``argument-hint`` / ``allowed-tools`` /
  ``disable-model-invocation`` parse instead of being skipped. Uppercase
  starts are accepted too so a capitalized key (``Name:`` / ``Description:``)
  no longer fails the whole file — it is a non-semantic key (``KNOWN_KEYS``
  is lowercase, so ``Name`` ≠ ``name``) and routes to ``metadata``, matching
  the "unknown/typo key → metadata, never fatal" contract below.
* ``value`` is the trimmed remainder of the line, captured **verbatim
  as an opaque string**; **no** quoting, escaping, list, or
  nested-structure interpretation. An inline ``allowed-tools: [Read,
  Bash]`` is stored as the literal string ``"[Read, Bash]"`` — I5 does
  not parse it into a YAML list.
* Indented continuation lines after a top-level key are tolerated for
  compatibility with YAML frontmatter. For ordinary unknown metadata,
  the block is dedented and captured as an opaque string. For folded
  (``>``) and literal (``|``) block scalars, the block is reduced to
  the string a skill menu needs; this is intentionally still a tiny
  YAML-ish subset, not a general YAML parser.
* Recognised semantic keys (drive behavior): ``name`` / ``description``
  / ``version`` / ``priority``. **Any other key is tolerated** (4.5-I5):
  it is returned in ``fields`` like any other key; ``SkillIndexer``
  routes the non-semantic keys into ``SkillDescription.metadata`` as
  opaque captured strings. This is the deliberate trade-off for loading
  real open-source skills unchanged — a typo of a known key
  (``descrption:``) silently becomes metadata rather than erroring.
* Duplicate keys take the *last* value and emit a warning.
* Body is the post-terminator content, CRLF normalised to LF; trailing
  whitespace / blank lines preserved.

Structural violations (missing leading/terminating delimiter, malformed
``key: value`` line) still raise :class:`FrontmatterError`; callers
(``SkillIndexer``) log + skip and move on so one bad SKILL.md never
takes down the Registry build.
"""

from __future__ import annotations

import re
from typing import Iterable


__all__ = ["FrontmatterError", "KNOWN_KEYS", "parse"]


KNOWN_KEYS: frozenset[str] = frozenset(
    {"name", "description", "version", "priority"}
)

_LINE_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)[ \t]*:[ \t]*(.*?)[ \t]*$")


class FrontmatterError(ValueError):
    """Raised when SKILL.md frontmatter is unparseable.

    The whole file is considered invalid; SkillIndexer logs and skips.
    """


def parse(text: str) -> tuple[dict[str, str], str, list[str]]:
    """Parse one SKILL.md text blob.

    Returns ``(fields, body, warnings)`` where ``fields`` is the
    key→value dict produced by the frontmatter, ``body`` is the
    post-frontmatter content with line endings normalised to LF, and
    ``warnings`` lists non-fatal advisories (e.g. duplicate keys took
    the last value).

    ``fields`` carries **every** parsed key, semantic and non-semantic
    alike (4.5-I5 no longer rejects unknown keys); SkillIndexer splits
    the semantic keys from the opaque metadata.

    Raises :class:`FrontmatterError` on a structural violation —
    missing leading/terminating delimiter, malformed ``key: value``
    line, or invalid key format. The error message names the specific
    rule so SkillIndexer can log it usefully.
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
    """Strip a single trailing ``\\r`` so CRLF input collapses to LF
    once we re-join with ``\\n``."""
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
        # 4.5-I5: unknown keys are tolerated (routed to metadata by the
        # Indexer), no longer fatal — real public skills carry arbitrary
        # extra keys (allowed-tools, argument-hint, license, ...).
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
