"""Memory match primitives — tokenisation and the two-tier matcher.

Pure functions over the ``(name, summary, type, keywords)`` entries
snapshot; no disk, no context-channel imports. Split out of
:mod:`~noeta.builtins.memory.impl.index` so the store's write-time
near-duplicate check can reuse the exact recall vocabulary without
dragging the content channel into the store module.
"""

from __future__ import annotations

import re


__all__ = [
    "DEFAULT_RECALL_MAX_HITS",
    "MemoryEntries",
    "SUMMARY_MIN_OVERLAP",
    "match_memories",
    "match_memories_tiered",
    "match_tokens",
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
#: because a tier-1 hit spends a whole memory body on ONE shared token, and
#: two-letter fragments (``db``, ``ci``, the tail of a hyphenated slug) share
#: far too easily with ordinary prose; a two-letter term is still reachable
#: through ``memory_search``.
_MIN_TOKEN_LEN = 3

#: Word tokens too common to be evidence of anything. Same reasoning as the
#: length floor and the same blast radius: without it a memory named
#: ``user-preferences`` fires tier-1 — a whole body inline — on any message
#: containing "user". Deliberately small and closed: a stopword list is a
#: precision knob, not a language model, and every entry here is a word no
#: author would choose as the distinguishing half of a memory slug. The CJK
#: path never consults it.
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


def match_tokens(value: str) -> set[str]:
    """Match tokens, by script.

    A space-separated run is one token per word, **filtered**: a word shorter
    than :data:`_MIN_TOKEN_LEN` or listed in :data:`_STOPWORDS` is not
    evidence, so it never reaches either tier. The filter is what keeps
    tier-1's threshold of one honest — one shared token buys a whole memory
    body, so that token has to mean something.

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
    lowered = value.lower()
    tokens = {
        t
        for t in _TOKEN_RE.findall(lowered)
        if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
    }
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


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


def match_memories_tiered(
    entries: MemoryEntries,
    text: str,
    *,
    max_hits: int = DEFAULT_RECALL_MAX_HITS,
) -> tuple[tuple[str, bool], ...]:
    """Two-tier recall matching, pure and deterministic — with the tier.

    Returns ``(name, by_name)`` pairs where ``by_name`` marks a tier-1 hit.
    The tier is not bookkeeping — it is the confidence signal the injector
    spends on, so it has to survive the call (see ``format_recall_text``
    for what the difference buys).

    Tier 1: a memory hits when any token of its NAME appears in the user
    text — names are author-chosen slugs, so one *filtered* shared token is
    high-signal (:func:`match_tokens` has already dropped stopwords and
    words under :data:`_MIN_TOKEN_LEN`, which is what stops a slug like
    ``deploy-the-thing`` from firing on "the").

    Tier 2: an entry not already hit by name hits when its SUMMARY shares
    at least :data:`SUMMARY_MIN_OVERLAP` distinct tokens (prose needs more
    evidence than a slug), OR when any KEYWORDS item occurs in the text
    as a phrase (:func:`_keywords_hit` — word-prefix for ASCII items,
    substring for CJK). Keywords are curator-authored retrieval aliases —
    synonyms and cross-language equivalents — so one occurring phrase
    carries name-grade signal; the hit still rides tier-2 because the
    user did not *name* the memory, and a guess is worth a pointer, not a
    body. This is the deterministic answer to cross-lingual recall: a
    Chinese query meets an English-named memory through its Chinese
    keywords, with zero services involved. The ``type`` field never
    participates.

    Order is tier-1 hits in index order, then tier-2 hits in index order,
    capped at ``max_hits`` overall. Vector / semantic retrieval is out of
    scope: its backing service would arrive behind an adapter, swapping this
    function whole.
    """
    text_tokens = match_tokens(text)
    if not text_tokens:
        return ()
    lowered = text.lower()
    name_hits: list[tuple[str, bool]] = []
    summary_hits: list[tuple[str, bool]] = []
    for name, summary, _type, keywords in entries:
        if match_tokens(name) & text_tokens:
            name_hits.append((name, True))
        elif len(
            match_tokens(summary) & text_tokens
        ) >= SUMMARY_MIN_OVERLAP or (
            keywords and _keywords_hit(keywords, lowered)
        ):
            summary_hits.append((name, False))
    return tuple((name_hits + summary_hits)[:max_hits])


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
