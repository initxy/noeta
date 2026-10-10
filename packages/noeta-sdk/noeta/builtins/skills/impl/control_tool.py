"""``skill`` — the model-driven skill-invocation control tool.

A control tool, not an Engine/ToolRuntime tool: a ``skill`` call activates a
named skill via a ``StatePatchDecision``, the same channel pre-loop activations
use. This module owns the tool's whole story — provider schema, skill-name
validator, response→neutral-Decision translate body, its ``skill.md``
description, and the budget fitting of the roster lines the skill listing
records (:mod:`~noeta.builtins.skills.impl.listing`). The schema carries no
roster: it is byte-identical for every task and turn, so the head of the prompt
never moves with the skill set. It builds on kernel-side neutral mechanism only
(the control-tool mount types, ``ControlTranslateContext``, shared validators),
and is reached solely through the plugin loader's ``ref`` resolution.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Mapping, Optional, Sequence

from noeta.execution.control_tool import (
    ControlToolBuildContext,
    ControlToolMount,
)
from noeta.policies.control_semantics import (
    ControlTranslateContext,
    ack_patch_decision,
    validate_required_string,
)
from noeta.protocols.decisions import (
    Decision,
    TaskStatePatch,
    ToolCall,
    ToolCallsDecision,
)
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.resources import load_markdown

from .indexer import strip_argument_placeholder


__all__ = [
    "DEFAULT_MENU_BUDGET_TOKENS",
    "MENU_DESCRIPTION_MAX_TOKENS",
    "MENU_SHORT_SUMMARY_MAX_TOKENS",
    "SKILL_TOOL",
    "SKILL_NOT_ALONE",
    "estimate_menu_tokens",
    "fit_menu_to_budget",
    "listing_lines",
    "menu_keep_order",
    "short_summary",
    "skill_tool_schema",
    "make_skill_translate",
    "make_skills_control_tool",
    "model_invocable_names",
    "roster_line",
]


_log = logging.getLogger(__name__)


#: Model-visible **control** tool name for model-driven skill menu selection.
SKILL_TOOL = "skill"


#: Per-skill budget for the roster line, in estimated tokens
#: (:func:`estimate_menu_tokens`). Every roster line is recorded into the skill
#: listing every task carries for its whole life, so an author's unbounded
#: ``description:`` is unbounded prompt on every single turn of every session
#: that indexes it. Tokens, not characters, so a Chinese summary is
#: capped at the same cost as an English one; 384 is Claude Code's 1,536
#: characters for an ASCII summary.
MENU_DESCRIPTION_MAX_TOKENS = 384

_TRUNCATION_MARKER = "… (truncated)"

#: The short form an over-budget roster gives every skill before any skill
#: keeps its full summary: the first sentence, clipped to this many estimated
#: tokens. Enough for the "what" an author puts first; small enough that a
#: roster of a few dozen skills fits the default budget.
MENU_SHORT_SUMMARY_MAX_TOKENS = 24

_SHORT_MARKER = "…"

#: A sentence ends at ``.`` / ``!`` / ``?`` followed by whitespace, or right
#: after a full-width ``。！？；`` (CJK prose puts no space after them).
_SENTENCE_END = re.compile(r"(?<=[.!?])\s|(?<=[。！？；])")

#: Total roster budget, in estimated tokens, when the host passes no
#: ``menu_budget_tokens`` — 1 % of a 200k-token window, the same fraction the
#: host derives from the bound model's catalog window. The per-skill cap
#: above bounds one entry; this bounds the whole roster, which is what a
#: workspace with a hundred skills actually pays for on every turn.
DEFAULT_MENU_BUDGET_TOKENS = 2000

#: The kernel's ``chars/4`` heuristic, applied to the non-CJK part of a
#: summary. A Han / Kana / Hangul character is counted as one token instead:
#: skill summaries are routinely written in Chinese, and ``chars/4`` would
#: under-count them three- to four-fold, making the budget a fiction exactly
#: where it matters.
_CHARS_PER_TOKEN = 4

#: One character = one token for these scripts. A compiled class rather than
#: a per-character range walk: a 200-skill Chinese roster is scanned a few
#: times per build, and the walk cost ~10× the regex.
_CJK_CHAR = re.compile(
    "["
    "\u1100-\u11ff"  # Hangul Jamo
    # CJK symbols and punctuation, Hiragana, Katakana, Bopomofo, Hangul
    # compatibility Jamo, Kanbun, CJK strokes, enclosed CJK, CJK compatibility
    "\u3000-\u33ff"
    "\u3400-\u4dbf"  # CJK unified ideographs extension A
    "\u4e00-\u9fff"  # CJK unified ideographs
    "\uac00-\ud7af"  # Hangul syllables
    "\uf900-\ufaff"  # CJK compatibility ideographs
    "\uff00-\uffef"  # halfwidth and fullwidth forms
    "\U00020000-\U0002ffff"  # CJK unified ideographs extensions B–F
    "]"
)


def _is_cjk(ch: str) -> bool:
    return _CJK_CHAR.fullmatch(ch) is not None


def estimate_menu_tokens(text: str) -> int:
    """Deterministic, CJK-aware token estimate for one piece of roster text.

    A budgeting unit, not a billed count — like the kernel's
    ``estimate_text_tokens`` it only has to be stable and monotone. CJK
    characters count one each; the rest ``ceil(n / 4)``.
    """
    cjk = len(_CJK_CHAR.findall(text))
    other = len(text) - cjk
    return cjk + (other + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _clip_to_tokens(text: str, max_tokens: int, marker: str) -> str:
    """The longest prefix of ``text`` whose estimate, ``marker`` included,
    stays within ``max_tokens`` — with ``marker`` appended.

    The estimate is monotone in the prefix length, so a binary search over it
    is exact; it costs a few regex scans instead of a per-character walk.
    """
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_menu_tokens(text[:mid] + marker) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + marker


def short_summary(text: str) -> str:
    """The short roster form of one summary.

    A summary within :data:`MENU_SHORT_SUMMARY_MAX_TOKENS` is its own short
    form. A longer one keeps its first sentence, clipped to the cap with a
    trailing ``…`` when the sentence alone is still too long.
    """
    if estimate_menu_tokens(text) <= MENU_SHORT_SUMMARY_MAX_TOKENS:
        return text
    first = _SENTENCE_END.split(text.strip(), maxsplit=1)[0].strip()
    if estimate_menu_tokens(first) <= MENU_SHORT_SUMMARY_MAX_TOKENS:
        return first
    return _clip_to_tokens(first, MENU_SHORT_SUMMARY_MAX_TOKENS, _SHORT_MARKER)


#: One roster line is ``- name: summary`` (``- name`` with no summary), one
#: per line; the cost model charges the whole line so the estimate tracks the
#: recorded listing rather than the bare summaries.
_LINE_BULLET = "- "
_LINE_COLON = ": "


def roster_line(name: str, summary: str) -> str:
    """The listing line for ``name`` showing ``summary`` (``""`` ⇒ name only)."""
    return _LINE_BULLET + name + (_LINE_COLON + summary if summary else "")


_SKILL_DESCRIPTION = load_markdown(__package__, "skill")

_SKILL_PROP_DESCRIPTION = (
    "Name of the skill to activate, from the skill listing in your context."
)


def skill_tool_schema() -> dict[str, Any]:
    """Provider-visible schema for :data:`SKILL_TOOL`.

    A control tool (never an Engine/ToolRuntime tool): a ``skill`` call
    activates a named skill via a ``StatePatchDecision`` (``activate_skills``
    patch), the same channel pre-loop activations use. Added to the Composer's
    ``control_action_schemas`` only when ``skill_invocation_enabled`` AND the
    workspace has at least one indexed skill.

    A single required ``skill`` string parameter, with no ``enum`` and no
    roster: the names and summaries reach the model through the recorded skill
    listing, and the translate validates the name. The schema is therefore the
    same bytes for every task and every turn (Claude Code's ``Skill`` tool has
    the same shape).
    """
    skill_prop = {"type": "string", "description": _SKILL_PROP_DESCRIPTION}
    return {
        "type": "function",
        "function": {
            "name": SKILL_TOOL,
            "description": _SKILL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": skill_prop,
                },
                "required": ["skill"],
            },
        },
    }


# Max length for a skill name string (generous upper bound; the real gate is
# the name set, but this catches obviously-malformed payloads before we
# format the error roster).
_SKILL_NAME_MAX_LEN = 200


#: The answer to a ``skill`` call that shared its response with ordinary tool
#: calls. Those calls ran; only the activation was refused.
SKILL_NOT_ALONE = (
    "Skill not loaded: skill must be the only tool call in its response, "
    "because its instructions arrive with the step after it. The other tool "
    "calls in this response did run - their results are here. Call skill by "
    "itself now if you still need it."
)


def _maybe_skill_decision(
    response: LLMResponse,
    assistant_message: Message,
    *,
    menu_names: frozenset[str],
    assistant_thinking: tuple[ThinkingBlock, ...] = (),
    control_tool_names: frozenset[str] = frozenset(),
) -> Decision | None:
    """Translate a ``skill`` control-tool call into a neutral
    :class:`StatePatchDecision`, or ``None`` when no ``skill`` is present.

    The ``skill`` call must be the **sole** tool call in the turn. Batched with
    ordinary tool calls, those calls still run — a :class:`ToolCallsDecision`
    carrying them, with the ``skill`` call answered in place by
    :data:`SKILL_NOT_ALONE` — and only the activation is refused, so a model
    that habitually batches loses one round trip, not its work. The skill is
    not loaded alongside them: its activation anchors where the patch lands,
    which on that path is ahead of the very assistant turn that asked for it,
    and the body would be written in front of a turn already sent. Two
    ``skill`` calls, or ``skill`` beside another control tool, stay a
    recoverable refusal of the whole response. The ``skill``
    argument is validated against the model-invocable name set: a known name becomes a
    ``StatePatchDecision(activate_skills=[name])``; an unknown name becomes an
    error ack listing the available names so the model can retry.

    Duplicate activation is not special-cased — the success ack is returned and
    ``TaskStatePatch.apply`` unions ``activate_skills`` with
    ``state.active_skills`` order-preserving, so it de-duplicates.
    """
    tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
    skill_blocks = [b for b in tool_uses if b.tool_name == SKILL_TOOL]
    if not skill_blocks:
        return None

    # Sole-call rule — exactly one `skill` block and nothing else.
    others = [b for b in tool_uses if b.tool_name != SKILL_TOOL]
    if (
        len(skill_blocks) == 1
        and others
        and not any(b.tool_name in control_tool_names for b in others)
    ):
        return ToolCallsDecision(
            calls=[
                ToolCall(
                    tool_name=b.tool_name,
                    arguments=dict(b.arguments),
                    call_id=b.call_id,
                )
                for b in others
            ],
            assistant_message=assistant_message,
            assistant_thinking=assistant_thinking,
            preacked_results=(
                ToolResultBlock(
                    call_id=skill_blocks[0].call_id,
                    output="",
                    success=False,
                    error=SKILL_NOT_ALONE,
                ),
            ),
        )
    if len(skill_blocks) != len(tool_uses) or len(skill_blocks) != 1:
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            # The same text is stamped on every tool_use of the response, so it
            # has to read correctly on a neighbour that never ran too.
            text=(
                "Nothing in this response ran: skill must be called on its "
                "own, once per response. Call skill by itself first, then "
                "re-issue the other calls in a separate response."
            ),
            valid=False,
        )

    block = skill_blocks[0]
    args = dict(block.arguments)
    ok, name_or_err = validate_required_string(
        args.get("skill"), "skill", _SKILL_NAME_MAX_LEN
    )
    if not ok:
        assert isinstance(name_or_err, str)
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=name_or_err,
            valid=False,
        )
    name = name_or_err
    assert isinstance(name, str)
    if name not in menu_names:
        available = ", ".join(sorted(menu_names)) if menu_names else "(none)"
        return ack_patch_decision(
            tool_uses,
            assistant_message,
            assistant_thinking,
            patch=None,
            text=f"unknown skill {name!r}; available: {available}",
            valid=False,
        )
    return ack_patch_decision(
        tool_uses,
        assistant_message,
        assistant_thinking,
        patch=TaskStatePatch(activate_skills=[name]),
        # "next step", not "next turn": the instructions are composed into the
        # very next request of this same turn (CONTEXT.md — one turn is wake →
        # many steps → park).
        text=f"Skill '{name}' loaded; its instructions will appear in your "
        f"context from your next step.",
        valid=True,
    )


def make_skill_translate(
    menu_names: frozenset[str],
) -> Callable[[ControlTranslateContext], Optional[Decision]]:
    """Build the ``skill`` translate closure over the model-invocable names.

    The closure captures ``menu_names`` — this build's live roster, not the
    listing the task recorded — so :func:`_maybe_skill_decision` accepts a
    skill installed since the listing and refuses one removed since, without
    the neutral :class:`ControlTranslateContext` carrying a feature-named
    field.
    """

    def translate(ctx: ControlTranslateContext) -> Optional[Decision]:
        return _maybe_skill_decision(
            ctx.response,
            ctx.assistant_message,
            menu_names=menu_names,
            assistant_thinking=ctx.assistant_thinking,
            control_tool_names=ctx.control_tool_names,
        )

    return translate


def _menu_description(text: str) -> str:
    """Fit one skill's description into the roster budget.

    Truncation is a suffix marker rather than a hard cut, so the model can tell
    a clipped summary from a terse one and knows the body holds the rest. The
    result stays within :data:`MENU_DESCRIPTION_MAX_TOKENS` estimated tokens,
    marker included. ``$ARGUMENTS`` is excised first — the listing is a
    model-visible surface and Noeta has no argument channel to substitute from
    (same rule as the activation render; see
    ``indexer.strip_argument_placeholder``).
    """
    text = strip_argument_placeholder(text)
    if estimate_menu_tokens(text) <= MENU_DESCRIPTION_MAX_TOKENS:
        return text
    return _clip_to_tokens(text, MENU_DESCRIPTION_MAX_TOKENS, _TRUNCATION_MARKER)


def _tier_of(registry: Any, name: str) -> int:
    """``registry.tier_of(name)`` when the registry keeps tiers, else ``0``.

    Duck-typed so a synthetic registry (a test double, a host's own) that
    offers only ``names()`` / ``get()`` still builds a menu."""
    tier_of = getattr(registry, "tier_of", None)
    if tier_of is None:
        return 0
    return int(tier_of(name))


def menu_keep_order(
    registry: Any,
    names: Sequence[str],
    rank: Optional[Mapping[str, float]] = None,
) -> tuple[str, ...]:
    """``names`` in the order their summaries are worth keeping.

    Highest first: the host's ``rank`` score (a per-task, per-tenant signal —
    usage-derived or hand-set — absent names score ``0``), then the merge
    tier (workspace-local above global above built-in / borrowed), then the
    frontmatter ``priority`` (ascending, the render-order convention), then
    the name. Every key is fixed at session build, so the same registry and
    rank always yield the same order — and the same roster bytes.
    """

    scores: Mapping[str, float] = rank if rank is not None else {}

    def key(name: str) -> tuple[float, int, int, str]:
        desc = registry.get(name)
        priority = getattr(desc, "priority", 0) if desc is not None else 0
        return (
            -float(scores.get(name, 0.0)),
            -_tier_of(registry, name),
            int(priority),
            name,
        )

    return tuple(sorted(names, key=key))


def _entry_tokens(name: str, description: str) -> int:
    """Estimated cost of one roster line plus its newline."""
    return estimate_menu_tokens(roster_line(name, description) + "\n")


def fit_menu_to_budget(
    entries: Sequence[tuple[str, str]],
    keep_order: Sequence[str],
    budget_tokens: int,
) -> dict[str, str]:
    """The summary each name shows in a roster fitted to ``budget_tokens``:
    its full summary, its :func:`short_summary`, or ``""`` (name only).

    The cost model is the rendered roster: every entry pays for its name (a
    name is never dropped — the listing must name every skill the model may
    activate), and a summary pays its increment over the bare name. Below the
    budget every summary stays full, so today's rosters are byte-identical.
    Over it, two greedy passes run in ``keep_order``:

    - **breadth** — each skill gets its short summary while the increment
      still fits (a later, shorter one can still fit after a longer one was
      skipped, the greedy Claude Code's listing uses); the rest go name-only;
    - **depth** — only when no skill went name-only, each skill is upgraded to
      its full summary while that increment still fits.

    A short summary is what lets the model judge a skill and a bare name
    mostly is not, so every skill gets something before any skill gets
    everything; the keep order decides who gets the full text and, when even
    the short forms overflow, who is listed by name only. A names-only roster
    that already exceeds the budget shows no summary at all: the entry count
    is the workspace's to trim.

    Every entry is charged exactly once: ``keep_order`` first (duplicates
    collapsed to their first position), then any name it left out, as the
    lowest priority — a partial keep order can never let a summary slip past
    the budget. Duplicate names in ``entries`` are a caller error (the
    baseline would under-count) and raise.
    """
    by_name = dict(entries)
    if len(by_name) != len(entries):
        raise ValueError("fit_menu_to_budget: duplicate names in entries")
    if sum(_entry_tokens(name, desc) for name, desc in by_name.items()) <= budget_tokens:
        return by_name
    bare = {name: _entry_tokens(name, "") for name in by_name}
    remaining = budget_tokens - sum(bare.values())
    ordered: list[str] = []
    seen: set[str] = set()
    for name in (*keep_order, *by_name):
        if name in by_name and name not in seen:
            seen.add(name)
            ordered.append(name)
    fitted = {name: "" for name in by_name}
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
        increment = _entry_tokens(name, description) - _entry_tokens(
            name, fitted[name]
        )
        if increment <= remaining:
            remaining -= increment
            fitted[name] = description
    return fitted


def model_invocable_names(registry: Any) -> tuple[str, ...]:
    """The sorted skill names ``registry`` lets the model invoke — what the
    ``skill`` translate accepts and the listing names, before any budget
    fitting. A skill whose
    frontmatter declares ``disable-model-invocation: true`` is left out (it
    stays loadable through the host preload channel). ``()`` for no registry."""
    if registry is None:
        return ()
    out: list[str] = []
    for name in sorted(registry.names()):
        desc = registry.get(name)
        if desc is None or not getattr(desc, "model_invocable", True):
            continue
        out.append(name)
    return tuple(out)


def listing_lines(
    registry: Any,
    *,
    budget_tokens: int = DEFAULT_MENU_BUDGET_TOKENS,
    rank: Optional[Mapping[str, float]] = None,
) -> tuple[tuple[str, str], ...]:
    """The roster as ``(name, line)`` pairs, name-sorted and fitted to
    ``budget_tokens`` — what the skill listing records.

    A skill whose frontmatter declares ``disable-model-invocation: true`` is
    left out, so the model neither sees it nor can name it. It stays in the
    Registry, which is what keeps the host preload channel (``Options.skills``,
    a seed activation) able to load it — the same split Claude Code draws
    between what the model may invoke and what the user may.

    Over the budget a skill shows a shortened summary or its name alone
    (:func:`fit_menu_to_budget` in :func:`menu_keep_order`). The lines stay
    name-sorted whatever the keep order; rank decides only how much of each
    summary survives. The listing records these lines once per task, so rank
    and budget shape a task's bytes exactly once. Going over budget is logged
    once per build — the operator's cue to trim the skill tiers or raise the
    budget.
    """
    if registry is None:
        return ()
    entries: list[tuple[str, str]] = []
    for name in model_invocable_names(registry):
        desc = registry.get(name)
        if desc is None:
            continue
        entries.append((name, _menu_description(desc.description)))
    if not entries:
        return ()
    names = tuple(name for name, _ in entries)
    fitted = fit_menu_to_budget(
        entries, menu_keep_order(registry, names, rank), budget_tokens
    )
    kept = sum(1 for name, desc in entries if desc and fitted[name] == desc)
    shortened = sum(1 for name, desc in entries if fitted[name] and fitted[name] != desc)
    name_only = sum(1 for name, desc in entries if desc and not fitted[name])
    if shortened or name_only:
        _log.warning(
            "skill menu over budget: of %d skills, %d keep a full summary, "
            "%d shortened, %d listed by name only (full roster ~%d tokens, "
            "budget %d); trim the skill tiers or raise "
            "plugin_config['skills']['menu_budget_tokens']",
            len(entries),
            kept,
            shortened,
            name_only,
            sum(_entry_tokens(name, desc) for name, desc in entries),
            budget_tokens,
        )
    return tuple((name, roster_line(name, fitted[name])) for name in names)


def make_skills_control_tool(
    registry: Any,
) -> Callable[[ControlToolBuildContext], Optional[ControlToolMount]]:
    """Build the ``skill`` control-tool mount factory over ``registry``.

    The returned factory closes over the pack's merged registry and rides
    ``PackContribution.control_tools`` into the builder's generic mount loop. It
    self-gates on the effective ``skill_invocation`` capability flag AND a
    non-empty model-invocable roster — mounting IS enablement. The schema is
    fixed (:func:`skill_tool_schema`); only the translate's name set follows
    the registry.
    """

    def factory(ctx: ControlToolBuildContext) -> Optional[ControlToolMount]:
        if not ctx.flag("skill_invocation"):
            return None
        names = frozenset(model_invocable_names(registry))
        if not names:
            return None
        return ControlToolMount(
            name=SKILL_TOOL,
            schema=skill_tool_schema(),
            translate=make_skill_translate(names),
            routing_priority=400,
            schema_priority=400,
        )

    return factory
