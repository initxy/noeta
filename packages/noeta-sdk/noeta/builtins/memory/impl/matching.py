"""Memory match primitives — tokenisation and the two-tier matcher.

Pure functions over the ``(name, summary, type, keywords)`` entries
snapshot; no disk, no context-channel imports. Split out of
:mod:`~noeta.builtins.memory.impl.index` so the store's write-time
near-duplicate check can reuse the exact recall vocabulary without
dragging the content channel into the store module.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Collection


__all__ = [
    "DEFAULT_RECALL_MAX_HITS",
    "MemoryEntries",
    "NAME_MIN_OVERLAP",
    "RECALL_KEY_MAX_CHARS",
    "RankedMemory",
    "SUMMARY_MIN_OVERLAP",
    "carried_tokens",
    "common_name_tokens",
    "match_memories",
    "match_memories_tiered",
    "match_tokens",
    "name_overlap_needed",
    "rank_memories",
]


#: The index source shape: ``(name, summary, type, keywords)`` quadruples,
#: sorted by name (``MemoryStore.entries()`` produces exactly this).
#: ``summary`` is the frontmatter description or the first non-empty body
#: line; ``type`` is the validated frontmatter type or ``""``; ``keywords``
#: is the raw frontmatter ``keywords`` value (comma-separated retrieval
#: aliases, ``""`` when absent) — matcher-only material, never rendered
#: into the index.
MemoryEntries = tuple[tuple[str, str, str, str], ...]

#: Recall injection cap — keeps a chatty match from flooding the turn.
DEFAULT_RECALL_MAX_HITS = 5

#: The matcher reads at most this many leading characters of the text. The
#: recall key is the whole incoming message, and a message is routinely a
#: person's sentence followed by something pasted — a log, a stack trace, a
#: worker's report. Tokenised whole, a 40 KB paste shares a token with nearly
#: every page in the store and recall answers with five bodies of noise. The
#: ask is at the head: a message states what it wants before it pastes the
#: evidence, so the leading slice is the part that carries intent. 2 000
#: characters is ten index summaries (:data:`_SUMMARY_MAX_CHARS` is 200) and
#: comfortably the whole of any typed message including the head of a pasted
#: excerpt; past it, a page is still reachable through ``memory_search``.
RECALL_KEY_MAX_CHARS = 2000

_TOKEN_RE = re.compile(r"[a-z0-9]+")
#: Keyword list separators — liberal on purpose: a Chinese-writing model
#: reaches for ``，`` / ``、`` as naturally as ``,``, and rejecting those
#: would silently disable the aliases it wrote.
_KEYWORD_SEP_RE = re.compile(r"[,，、;；]")
#: An all-ASCII keyword item gets word-PREFIX matching; anything else
#: (CJK or mixed) gets plain substring containment.
_ASCII_ITEM_RE = re.compile(r"^[\x00-\x7f]+$")
#: Scripts written without spaces (CJK ideographs, kana, hangul). The word
#: rule finds *nothing* in them, so they are tokenised separately.
_CJK_RUN_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+"
)
#: Word tokens shorter than this never match. Applies to the WORD rule only
#: — a CJK bigram is 2 characters by construction and must stay exempt, or
#: recall goes silently dead for every space-free script. Three (not two)
#: because two-letter fragments (``db``, ``ci``, the tail of a hyphenated
#: slug) share far too easily with ordinary prose; a two-letter term is still
#: reachable through ``memory_search``.
_MIN_TOKEN_LEN = 3

#: Word tokens too common to be evidence of anything, in any store. Same
#: reasoning as the length floor. Deliberately small and closed: a stopword
#: list is a precision knob, not a language model, and every entry here is a
#: word no author would choose as the distinguishing half of a memory slug.
#: What is common in ONE store — a project prefix two dozen names share, the
#: year in a dated slug — is not this list's job: :func:`common_name_tokens`
#: counts that off the store's own names. The CJK path never consults it.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "about", "after", "again", "all", "and", "any", "are", "been",
        "before", "being", "but", "can", "could", "did", "does", "for",
        "from", "had", "has", "have", "her", "him", "his", "how", "into",
        "its", "just", "like", "may", "more", "much", "not", "now", "one",
        "only", "other", "our", "out", "over", "please", "same", "she",
        "should", "some", "such", "than", "that", "the", "their", "them",
        "then", "there", "these", "they", "this", "those", "too", "use",
        "very", "was", "were", "what", "when", "where", "which", "who",
        "why", "will", "with", "would", "you", "your",
    }
)
#: Tier-2 (summary) matching needs this many distinct overlapping tokens
#: — a single shared prose word is too noisy to recall on. In a
#: space-free script the same threshold reads as "one shared word of 3+
#: characters, or two shared 2-character words", since an n-character run
#: yields n-1 bigrams. That is deliberately a shade looser than the word
#: rule, and it is affordable because a tier-2 hit costs one index line
#: rather than a whole memory body.
SUMMARY_MIN_OVERLAP = 2
#: Tier-1 (name) matching: the floor on how many of a name's tokens the text
#: must carry before it counts as having NAMED the page. A body is uninvited
#: context the model cannot decline, and names are slugs of ordinary technical
#: words (``applog-web-report-channel``), so ONE shared word — "report" — is a
#: lead, not evidence that the text named the page. One shared token therefore
#: earns the tier-2 pointer, never silence. See :func:`name_overlap_needed`
#: for the whole rule, of which this is the floor.
NAME_MIN_OVERLAP = 2
#: A name token is *common* once more than this many names carry it, or more
#: than a tenth of the store, whichever is larger — see
#: :func:`common_name_tokens`.
_COMMON_MIN_NAMES = 3


def _script_tokens(value: str, *, filtered: bool) -> set[str]:
    """The tokenisation both token views share — see :func:`match_tokens`.

    ``filtered`` applies the word rule's length floor and stopword set. It
    never touches bigrams: every bigram is exactly 2 characters, so a shared
    floor would delete the space-free path outright.
    """
    lowered = value.lower()
    words = _TOKEN_RE.findall(lowered)
    tokens = (
        {
            t
            for t in words
            if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
        }
        if filtered
        else set(words)
    )
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def match_tokens(value: str) -> set[str]:
    """Match tokens, by script — the **evidence** view.

    A space-separated run is one token per word, **filtered**: a word shorter
    than :data:`_MIN_TOKEN_LEN` or listed in :data:`_STOPWORDS` is not
    evidence, so it never reaches either tier.

    A CJK run becomes its **character bigrams**, because the word rule finds
    nothing at all in a script written without spaces — a wholly
    Chinese/Japanese/Korean message would yield an empty token set, which
    :func:`match_memories_tiered` early-returns on, making recall silently
    dead rather than merely weak. The length floor and the stopword set are
    the word rule's alone and MUST NOT touch bigrams: every bigram is exactly
    2 characters, so a shared floor would delete the space-free path outright.

    Bigrams are the standard segmenter-free approximation ("记忆机制" →
    ``{记忆, 忆机, 机制}``, which a "记忆" query meets) and keep this module's
    red line intact: pure, deterministic, no dictionary, no service. A
    single-character run falls back to the character itself so a
    one-character term still matches something.
    """
    return _script_tokens(value, filtered=True)


def carried_tokens(value: str) -> set[str]:
    """Every token ``value`` carries — the **size** view, filters off.

    The same tokenisation as :func:`match_tokens` without the length floor and
    the stopword set, so ``ci-cd-flow`` carries three tokens rather than one
    and ``how-we-deploy`` three rather than one. Two jobs, both tier-1's:

    * it measures how big a NAME is, so a name whose other tokens the filters
      ate cannot be "named" by the one token that survived them;
    * it measures how much of that name the TEXT carries, so a message that
      writes the slug out in full — ``zz-target``, ``ci-cd-flow`` — names the
      page even though the filters make ``zz`` / ``ci`` / ``cd`` unusable as
      evidence on their own.

    Never used as evidence by itself: a hit still needs a filtered token
    (:func:`rank_memories`), so a name made of stopwords cannot be named by
    ordinary prose.
    """
    return _script_tokens(value, filtered=False)


def name_overlap_needed(size: int) -> int:
    """How many of a ``size``-token name the text must carry to have named it.

    ``min(size, max(NAME_MIN_OVERLAP, size // 2))`` — at least
    :data:`NAME_MIN_OVERLAP` tokens **and** at least half of them (rounded
    down), and never more than the name has:

    * a one-token name (``deploy``, ``部署``) needs its one token, as before;
    * a two- or three-token name needs two — ``ci-cd-flow`` can no longer be
      named by ``flow`` alone, which is what made a passing "data flow" buy
      the whole page. Before this rule the floor was taken against the
      *filtered* token count, which collapsed to 1 for exactly the names whose
      other tokens the filters ate;
    * a long name needs half of it — 3 of the 6 bigrams of ``我们的部署流程``,
      which "我们这个流程" (the pronoun plus one ordinary word) does not reach.

    Half is rounded DOWN because CJK bigrams overlap: an N-character name
    yields N-1 bigrams, so the M shared characters of a genuine naming yield
    only M-1 of them, and rounding up would put a real naming just out of
    reach (``技能同步`` covers 3 of ``工具技能同步做法``'s 7 bigrams).
    """
    return min(size, max(NAME_MIN_OVERLAP, size // 2))


def _keywords_hit(keywords: str, lowered_text: str) -> bool:
    """Does any keyword item occur in the text — as a PHRASE, not tokens.

    Keywords are curator-chosen aliases, so each comma-separated item is
    matched whole against the raw lowered text, NOT tokenised: bigram
    set-intersection made "部署流程" fire on any text containing "流程",
    and word tokens made ``deploy`` blind to "deployment". Phrase
    containment fixes both at once:

    * An all-ASCII item anchors at a word START (``\\bdeploy`` meets
      "deploy", "deploys", "deployment" — poor-man's stemming) but never
      mid-word ("art" does not meet "startup").
    * A CJK / mixed item is a plain substring ("部署流程" hits only when
      the whole phrase appears; CJK has no word boundaries to anchor on).

    Deliberately NO length floor and NO stopword filter here: an item is
    an author's explicit choice, so a two-letter term like ``ci`` — which
    the word rule floors out of names and summaries — is reachable again
    through keywords.
    """
    for raw in _KEYWORD_SEP_RE.split(keywords.lower()):
        item = raw.strip()
        if not item:
            continue
        if _ASCII_ITEM_RE.match(item):
            if re.search(r"\b" + re.escape(item), lowered_text):
                return True
        elif item in lowered_text:
            return True
    return False


@dataclass(frozen=True, slots=True)
class RankedMemory:
    """One matched memory with the evidence that matched it.

    ``by_name`` marks a tier-1 hit. ``score`` is the number of distinct
    matched tokens — name tokens for tier 1, name and summary tokens together
    for tier 2 — and ``by_keyword`` a curator alias that occurred as a phrase.
    """

    name: str
    by_name: bool
    score: int
    by_keyword: bool = False


def common_name_tokens(entries: MemoryEntries) -> frozenset[str]:
    """The name tokens too widespread in THIS store to be name evidence.

    A token carried by more than ``max(3, len(entries) // 10)`` names. A
    closed stopword list cannot know that ``schema`` is filler in one store
    and the distinguishing word in another; document frequency over the
    store's own names can, with no dictionary — and it is what catches the
    project prefix two dozen slugs share and the year in a dated one. Count
    it over the WHOLE store (residents and excluded names included), so what
    is common never depends on which pages happen to be in context.
    """
    limit = max(_COMMON_MIN_NAMES, len(entries) // 10)
    counts = Counter(
        token
        for name, _summary, _type, _keywords in entries
        for token in match_tokens(name)
    )
    return frozenset(token for token, n in counts.items() if n > limit)


def rank_memories(
    entries: MemoryEntries,
    text: str,
    *,
    exclude: Collection[str] = (),
) -> tuple[RankedMemory, ...]:
    """Every memory ``text`` matches, best first, uncapped.

    The ordered whole of which :func:`match_memories_tiered` keeps the head;
    the write tool's near-duplicate probe reads it too, so "similar" means
    exactly "recall would surface it". ``exclude`` names never match but
    still count toward :func:`common_name_tokens`. Only the leading
    :data:`RECALL_KEY_MAX_CHARS` characters of ``text`` are read.

    Tier 1 means the text **named** the page: it carries
    :func:`name_overlap_needed` of the name's tokens — at least
    :data:`NAME_MIN_OVERLAP` of them and at least half of them, counting every
    token the name carries (:func:`carried_tokens`), store-common tokens
    excluded from both sides — and at least one of those is a filtered
    evidence token, so a name cannot be named by stopwords alone. One rule for
    every script: a CJK name's bigrams count exactly as an ASCII name's words
    do. Sorted by matched-token count, descending, then index order.

    Tier 2: an entry not hit by name hits on ONE shared non-common name token
    (a lead, not evidence), OR when its SUMMARY shares at least
    :data:`SUMMARY_MIN_OVERLAP` distinct tokens (prose needs more evidence
    than a slug), OR when any KEYWORDS item occurs in the text as a phrase
    (:func:`_keywords_hit` — word-prefix for ASCII items, substring for CJK).
    Keywords are curator-authored retrieval aliases — synonyms and
    cross-language equivalents — so one occurring phrase carries name-grade
    signal and sorts ahead of the rest of the tier; the hit still rides
    tier-2 because the user did not *name* the memory, and a guess is worth a
    pointer, not a body. This is the deterministic answer to cross-lingual
    recall: a Chinese query meets an English-named memory through its Chinese
    keywords, with zero services involved. After keyword hits the tier sorts
    by distinct matched tokens across name and summary, descending, then
    index order. The ``type`` field never participates.
    """
    key = text[:RECALL_KEY_MAX_CHARS]
    text_tokens = match_tokens(key)
    if not text_tokens:
        return ()
    lowered = key.lower()
    text_carried = carried_tokens(key)
    common = common_name_tokens(entries)
    skip = frozenset(exclude)
    name_hits: list[tuple[int, RankedMemory]] = []
    summary_hits: list[tuple[int, RankedMemory]] = []
    for position, (name, summary, _type, keywords) in enumerate(entries):
        if name in skip:
            continue
        carried = carried_tokens(name) - common
        distinctive = match_tokens(name) - common
        matched = distinctive & text_tokens
        # ``matched`` is the evidence (a filtered token the text shares);
        # ``covered`` is how much of the name the text carries at all, so a
        # message that writes the slug out in full still names the page even
        # where the filters left it one usable token. ``matched`` ⊆ ``covered``
        # by construction, so the truthy check is also the "at least one
        # evidence token" guard.
        covered = carried & text_carried
        if matched and len(covered) >= name_overlap_needed(len(carried)):
            name_hits.append(
                (position, RankedMemory(name, True, len(matched)))
            )
            continue
        summary_matched = match_tokens(summary) & text_tokens
        by_keyword = bool(keywords) and _keywords_hit(keywords, lowered)
        if (
            matched
            or len(summary_matched) >= SUMMARY_MIN_OVERLAP
            or by_keyword
        ):
            summary_hits.append(
                (
                    position,
                    RankedMemory(
                        name,
                        False,
                        len(matched | summary_matched),
                        by_keyword,
                    ),
                )
            )
    name_hits.sort(key=lambda hit: (-hit[1].score, hit[0]))
    summary_hits.sort(
        key=lambda hit: (not hit[1].by_keyword, -hit[1].score, hit[0])
    )
    return tuple(ranked for _position, ranked in name_hits + summary_hits)


def match_memories_tiered(
    entries: MemoryEntries,
    text: str,
    *,
    max_hits: int = DEFAULT_RECALL_MAX_HITS,
    exclude: Collection[str] = (),
) -> tuple[tuple[str, bool], ...]:
    """Two-tier recall matching, pure and deterministic — with the tier.

    Returns ``(name, by_name)`` pairs where ``by_name`` marks a tier-1 hit.
    The tier is not bookkeeping — it is the confidence signal the injector
    spends on, so it has to survive the call (see ``format_recall_text``
    for what the difference buys).

    The head of :func:`rank_memories` (which holds the matching rules):
    tier-1 hits, then tier-2 hits, each best first, capped at ``max_hits``
    overall — so the cap keeps the strongest evidence, not the start of the
    alphabet. Vector / semantic retrieval is out of scope: its backing
    service would arrive behind an adapter, swapping this function whole.
    """
    ranked = rank_memories(entries, text, exclude=exclude)
    return tuple((hit.name, hit.by_name) for hit in ranked[:max_hits])


def match_memories(
    entries: MemoryEntries,
    text: str,
    *,
    max_hits: int = DEFAULT_RECALL_MAX_HITS,
) -> tuple[str, ...]:
    """Two-tier recall matching, pure and deterministic.

    The tier-blind view: names only. :func:`match_memories_tiered` is the
    implementation, and the one to call when the tier matters.
    """
    return tuple(
        name
        for name, _by_name in match_memories_tiered(
            entries, text, max_hits=max_hits
        )
    )
