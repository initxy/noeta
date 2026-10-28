"""The skill listing: the roster the model may activate, fitted to a budget.

The ``skill`` tool's schema carries no roster (it is the same bytes for every
task and turn); names and summaries reach the model as a listing recorded
once per task (``roster``), and skills that join later are announced once
(``added-<k>``). Removals and edited summaries are not announced.

Budget: every line is ``- name: summary``; each summary is capped at 384
estimated tokens; over the total budget every skill first gets a short form
(its first sentence, at most 24 tokens), then, budget permitting, its full
summary — in keep order (rank, then tier, then priority, then name). A name
is never dropped.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence

from noeta.plugins.skills.registry import SkillRegistry, strip_argument_placeholder
from noeta.toolkit.context import estimate_tokens

__all__ = [
    "ADDED_HEADER",
    "MENU_DESCRIPTION_MAX_TOKENS",
    "MENU_SHORT_SUMMARY_MAX_TOKENS",
    "ROSTER_HEADER",
    "fit_menu_to_budget",
    "listing_lines",
    "menu_keep_order",
    "render_listing",
    "roster_line",
    "short_summary",
]

_log = logging.getLogger(__name__)

#: Per-skill cap on a summary, in estimated tokens (Claude Code's 1,536
#: characters for an ASCII summary).
MENU_DESCRIPTION_MAX_TOKENS = 384
_TRUNCATION_MARKER = "… (truncated)"

#: The short form an over-budget roster gives every skill first.
MENU_SHORT_SUMMARY_MAX_TOKENS = 24
_SHORT_MARKER = "…"

#: A sentence ends at ``.`` / ``!`` / ``?`` followed by whitespace, or right
#: after a full-width sentence mark (CJK prose puts no space after them).
_SENTENCE_END = re.compile(r"(?<=[.!?])\s|(?<=[。！？；])")  # noqa: RUF001

ROSTER_HEADER = "The following skills are available via the `skill` tool:"
ADDED_HEADER = "New skills are available via the `skill` tool:"


def _clip_to_tokens(text: str, max_tokens: int, marker: str) -> str:
    """The longest prefix of ``text`` that, with ``marker``, fits."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid] + marker) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + marker


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


def _menu_description(text: str) -> str:
    text = strip_argument_placeholder(text)
    if estimate_tokens(text) <= MENU_DESCRIPTION_MAX_TOKENS:
        return text
    return _clip_to_tokens(text, MENU_DESCRIPTION_MAX_TOKENS, _TRUNCATION_MARKER)


def menu_keep_order(
    registry: SkillRegistry, names: Sequence[str], rank: Mapping[str, float] | None = None
) -> tuple[str, ...]:
    """Highest first: rank score, merge tier, then ``priority`` ascending,
    then name."""
    scores = rank or {}

    def key(name: str) -> tuple[float, int, int, str]:
        desc = registry.get(name)
        return (-float(scores.get(name, 0.0)), -registry.tier_of(name),
                desc.priority if desc is not None else 0, name)

    return tuple(sorted(names, key=key))


def _entry_tokens(name: str, description: str) -> int:
    return estimate_tokens(roster_line(name, description) + "\n")


def fit_menu_to_budget(
    entries: Sequence[tuple[str, str]], keep_order: Sequence[str], budget_tokens: int
) -> dict[str, str]:
    """The summary each name shows: full, short, or ``""`` (name only).

    Under budget every summary stays full. Over it, a breadth pass gives
    each skill its short form while the increment fits (the rest go
    name-only); only when nobody went name-only, a depth pass upgrades to
    full summaries while they fit."""
    by_name = dict(entries)
    if len(by_name) != len(entries):
        raise ValueError("fit_menu_to_budget: duplicate names in entries")
    if sum(_entry_tokens(n, d) for n, d in by_name.items()) <= budget_tokens:
        return by_name
    bare = {n: _entry_tokens(n, "") for n in by_name}
    remaining = budget_tokens - sum(bare.values())
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
        increment = _entry_tokens(name, short) - bare[name]
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
        increment = _entry_tokens(name, description) - _entry_tokens(name, fitted[name])
        if increment <= remaining:
            remaining -= increment
            fitted[name] = description
    return fitted


def listing_lines(
    registry: SkillRegistry, *, budget_tokens: int, rank: Mapping[str, float] | None = None
) -> tuple[tuple[str, str], ...]:
    """The roster as name-sorted ``(name, line)`` pairs fitted to the
    budget. Skills with ``disable-model-invocation`` are left out."""
    entries: list[tuple[str, str]] = []
    for name in registry.model_invocable():
        desc = registry.get(name)
        if desc is not None:
            entries.append((name, _menu_description(desc.description)))
    if not entries:
        return ()
    names = tuple(n for n, _ in entries)
    fitted = fit_menu_to_budget(entries, menu_keep_order(registry, names, rank), budget_tokens)
    shortened = sum(1 for n, d in entries if fitted[n] and fitted[n] != d)
    name_only = sum(1 for n, d in entries if d and not fitted[n])
    if shortened or name_only:
        _log.warning(
            "skill menu over budget: of %d skills, %d shortened, %d listed by name only "
            "(full roster ~%d tokens, budget %d); trim the skill directories or raise "
            "Skills(budget_tokens=...)",
            len(entries), shortened, name_only,
            sum(_entry_tokens(n, d) for n, d in entries), budget_tokens)
    return tuple((n, roster_line(n, fitted[n])) for n in names)


def render_listing(first: bool, lines: Sequence[str]) -> str:
    """The text of one recorded listing: a header, then one line per skill."""
    return "\n".join((ROSTER_HEADER if first else ADDED_HEADER, *lines))
