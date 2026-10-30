"""The skill listing: the roster the model may activate, fitted to a budget.

The ``skill`` tool's schema carries no roster (it is the same bytes for every
task and turn); names and summaries reach the model as a listing recorded
once per task (``roster``); a skill that joins later, or whose description
changed since it was listed (its digest), is listed again under the same
header, as Claude Code does. Removals are not announced.

Budget: every line is ``- name: summary``; each summary is capped at 1,536
characters (Claude Code's cap); over the total budget every skill first gets a short form
(its first sentence, at most 24 tokens), then, budget permitting, its full
summary — in keep order (rank, then tier, then name). A name
is never dropped.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Collection, Mapping, Sequence

from noeta.plugins.skills.registry import (
    SkillDescription,
    SkillRegistry,
    strip_argument_placeholder,
)
from noeta.toolkit.tokens import estimate_tokens, prefix_within

__all__ = [
    "MENU_DESCRIPTION_MAX_CHARS",
    "MENU_SHORT_SUMMARY_MAX_TOKENS",
    "ROSTER_HEADER",
    "fit_menu_to_budget",
    "listing_lines",
    "listing_summary",
    "menu_keep_order",
    "render_listing",
    "roster_line",
    "short_summary",
    "skill_digest",
]

_log = logging.getLogger(__name__)

#: Per-skill cap on a summary, in characters (Claude Code's), the last one
#: the marker.
MENU_DESCRIPTION_MAX_CHARS = 1536
_TRUNCATION_MARKER = "…"

#: The short form an over-budget roster gives every skill first.
MENU_SHORT_SUMMARY_MAX_TOKENS = 24
_SHORT_MARKER = "…"

#: A sentence ends at ``.`` / ``!`` / ``?`` followed by whitespace, or right
#: after a full-width sentence mark (CJK prose puts no space after them).
_SENTENCE_END = re.compile(r"(?<=[.!?])\s|(?<=[。！？；])")  # noqa: RUF001

ROSTER_HEADER = "The following skills are available via the `Skill` tool:"


def _clip_to_tokens(text: str, max_tokens: int, marker: str) -> str:
    """The longest prefix of ``text`` that, with ``marker``, fits."""
    return text[:prefix_within(text, max_tokens, marker)].rstrip() + marker


def short_summary(text: str) -> str:
    """A summary within 24 tokens, else its first sentence, clipped."""
    if estimate_tokens(text) <= MENU_SHORT_SUMMARY_MAX_TOKENS:
        return text
    first = _SENTENCE_END.split(text.strip(), maxsplit=1)[0].strip()
    if estimate_tokens(first) <= MENU_SHORT_SUMMARY_MAX_TOKENS:
        return first
    return _clip_to_tokens(first, MENU_SHORT_SUMMARY_MAX_TOKENS, _SHORT_MARKER)


def roster_line(name: str, summary: str) -> str:
    return "- " + name + (": " + summary if summary else "")


def skill_digest(desc: SkillDescription) -> str:
    """What a listing remembers of a skill to see that it changed: a short
    hash of its name and listed text (Claude Code's digest)."""
    raw = desc.name + "\0" + listing_summary(desc)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def listing_summary(desc: SkillDescription) -> str:
    """What the listing says of a skill: its description, then its
    ``when_to_use`` after a dash (Claude Code's listing line)."""
    if desc.when_to_use:
        return f"{desc.description} - {desc.when_to_use}"
    return desc.description


def _menu_description(text: str) -> str:
    text = strip_argument_placeholder(text)
    if len(text) <= MENU_DESCRIPTION_MAX_CHARS:
        return text
    return text[:MENU_DESCRIPTION_MAX_CHARS - 1] + _TRUNCATION_MARKER


def menu_keep_order(
    registry: SkillRegistry, names: Sequence[str], rank: Mapping[str, float] | None = None
) -> tuple[str, ...]:
    """Highest first: rank score, merge tier, then name."""
    scores = rank or {}

    def key(name: str) -> tuple[float, int, str]:
        return (-float(scores.get(name, 0.0)), -registry.tier_of(name), name)

    return tuple(sorted(names, key=key))


def _entry_chars(name: str, description: str) -> int:
    return len(roster_line(name, description)) + 1


def fit_menu_to_budget(
    entries: Sequence[tuple[str, str]], keep_order: Sequence[str], budget_chars: int
) -> dict[str, str]:
    """The summary each name shows: full, short, or ``""`` (name only).

    Under budget every summary stays full. Over it, a breadth pass gives
    each skill its short form while the increment fits (the rest go
    name-only); only when nobody went name-only, a depth pass upgrades to
    full summaries while they fit."""
    by_name = dict(entries)
    if len(by_name) != len(entries):
        raise ValueError("fit_menu_to_budget: duplicate names in entries")
    if sum(_entry_chars(n, d) for n, d in by_name.items()) <= budget_chars:
        return by_name
    bare = {n: _entry_chars(n, "") for n in by_name}
    remaining = budget_chars - sum(bare.values())
    ordered: list[str] = []
    for name in (*keep_order, *by_name):
        if name in by_name and name not in ordered:
            ordered.append(name)
    fitted = {n: "" for n in by_name}
    name_only = False
    for name in ordered:
        description = by_name[name]
        if not description:
            continue
        short = short_summary(description)
        increment = _entry_chars(name, short) - bare[name]
        if increment <= remaining:
            remaining -= increment
            fitted[name] = short
        else:
            name_only = True
    if name_only:
        return fitted
    for name in ordered:
        description = by_name[name]
        if fitted[name] == description:
            continue
        increment = _entry_chars(name, description) - _entry_chars(name, fitted[name])
        if increment <= remaining:
            remaining -= increment
            fitted[name] = description
    return fitted


def listing_lines(
    registry: SkillRegistry, *, budget_chars: int, rank: Mapping[str, float] | None = None,
    unlocked: Collection[str] = (),
) -> tuple[tuple[str, str], ...]:
    """The roster as ``(name, line)`` pairs fitted to the budget, the
    narrowest scope first (the workspace's, then the user's, then the rest,
    as Claude Code lists them), by name within one. Skills with
    ``disable-model-invocation`` are left out, and so are skills with
    ``paths`` not in ``unlocked``."""
    entries: list[tuple[str, str]] = []
    for name in sorted(registry.model_invocable(unlocked),
                       key=lambda n: (-registry.tier_of(n), n)):
        desc = registry.get(name)
        if desc is not None:
            entries.append((name, _menu_description(listing_summary(desc))))
    if not entries:
        return ()
    names = tuple(n for n, _ in entries)
    fitted = fit_menu_to_budget(entries, menu_keep_order(registry, names, rank), budget_chars)
    shortened = sum(1 for n, d in entries if fitted[n] and fitted[n] != d)
    name_only = sum(1 for n, d in entries if d and not fitted[n])
    if shortened or name_only:
        _log.warning(
            "skill menu over budget: of %d skills, %d shortened, %d listed by name only "
            "(full roster %d characters, budget %d); trim the skill directories or raise "
            "Skills(budget_chars=...)",
            len(entries), shortened, name_only,
            sum(_entry_chars(n, d) for n, d in entries), budget_chars)
    return tuple((n, roster_line(n, fitted[n])) for n in names)


def render_listing(lines: Sequence[str]) -> str:
    """The text of one recorded listing, the first or a later one: a header,
    a blank line, then one line per skill."""
    return "\n".join((ROSTER_HEADER, "", *lines))
