"""Memory index — the content channel's SECOND resident.

This module is the proof case for the issue-02/03 generic abstraction:
adding the memory kind is exactly one :class:`ContentKindSpec` (a render
rule + a hash resolver + a drift policy) registered next to the skill
kind. No composer change, no runtime change.

Two deliberate contrasts with skills:

* **Drift policy is ``evolving``** — a memory edit is daily business, so
  the recording carries the ``evolving`` policy: the index ``content_hash``
  is recorded as provenance but is free to move (the ``pinned`` policy a
  skill carries instead would mark a moved hash as drift). This is why
  memories are NOT disguised as dynamically-generated skills.
* **The index is the only resident body** — full memory texts stay on
  disk; the model pulls them through the ordinary ``memory_read`` tool
  (``noeta.tools.memory``) or receives them via host recall
  (``noeta.execution.memory``).

Red line: every function here is pure over the ``(name, summary, type)``
entries snapshot taken at wiring time — the renderer closes over
preloaded state and never touches the disk at compose time, so the same
ledger always composes to the same bytes. Naive token matching
(:func:`match_memories`) is also pure; the impure half (reading the
store) lives in the host-side glue, before anything enters the ledger.
"""

from __future__ import annotations

import hashlib
import re

from noeta.context.composer import RenderedSkills
from noeta.context.content_channel import ContentKindSpec, ContentRenderer
from noeta.protocols.messages import Message, TextBlock


__all__ = [
    "DEFAULT_RECALL_MAX_HITS",
    "MEMORY_DRIFT_POLICY",
    "MEMORY_INDEX_NAME",
    "MEMORY_INDEX_VERSION",
    "MEMORY_KIND",
    "MemoryEntries",
    "build_memory_renderer",
    "format_recall_text",
    "match_memories",
    "memory_content_kind",
    "memory_index_hash",
    "render_memory_index_text",
]


#: The content channel kind key — matches ``TaskState.active_content``
#: and ``ContextContentRecorded.kind``.
MEMORY_KIND = "memory"
#: The index resident's name. v1 has exactly one resident per store; a
#: future sharded index would add names, not mechanisms.
MEMORY_INDEX_NAME = "index"
#: Declared version of the index *shape* (not its content — content is
#: free to evolve under the ``evolving`` policy).
MEMORY_INDEX_VERSION = "1"
#: The drift policy memory recordings carry: hash recorded, drift allowed.
MEMORY_DRIFT_POLICY = "evolving"
#: Recall injection cap — keeps a chatty match from flooding the turn.
DEFAULT_RECALL_MAX_HITS = 5

#: The index source shape: ``(name, summary, type)`` triples, sorted by
#: name (``MemoryStore.entries()`` produces exactly this). ``summary`` is
#: the frontmatter description or the first non-empty body line; ``type``
#: is the validated frontmatter type or ``""``.
MemoryEntries = tuple[tuple[str, str, str], ...]

_TOKEN_RE = re.compile(r"[a-z0-9]+")
#: Name tokens shorter than this never match (single letters are noise);
#: two-letter slugs like ``ci`` / ``db`` stay recallable.
_MIN_TOKEN_LEN = 2
#: Tier-2 (summary) matching needs this many distinct overlapping tokens
#: — a single shared prose word is too noisy to recall on.
_SUMMARY_MIN_OVERLAP = 2


def render_memory_index_text(entries: MemoryEntries) -> str:
    """Deterministic index text — the resident's rendered body.

    An entry with a type renders ``- name (type): summary``; without one
    the v1 ``- name: summary`` / ``- name`` forms are kept byte-for-byte,
    so a store whose files carry no frontmatter hashes exactly as before.
    """
    lines = [
        "Long-term memory index. Each entry is one stored memory; call",
        "the 'memory_read' tool with a memory's name for its full text.",
        "",
    ]
    for name, summary, mem_type in entries:
        label = f"{name} ({mem_type})" if mem_type else name
        lines.append(f"- {label}: {summary}" if summary else f"- {label}")
    return "\n".join(lines)


def memory_index_hash(entries: MemoryEntries) -> str:
    """``sha256`` over the rendered index text — the ``content_hash`` recorded
    on ``ContextContentRecorded`` and resolved through the generic
    ``ContentHashesFn`` seam. Hashing the *rendered* bytes keeps one
    source of truth: the recorded ``content_hash`` IS what the model saw."""
    return hashlib.sha256(
        render_memory_index_text(entries).encode("utf-8")
    ).hexdigest()


def build_memory_renderer(entries: MemoryEntries) -> ContentRenderer:
    """Bind an entries snapshot to the channel's renderer shape.

    Pure over the snapshot: renders one ``role="user"`` message holding
    the index when the ``index`` resident is active AND the snapshot is
    non-empty; anything else renders nothing (an unconfigured memory
    host leaves the ``semi_stable`` bytes untouched — zero footprint).
    ``selected_skills`` stays empty: that ``RenderedSkills`` field is
    the skill kind's plan extra, not the channel contract (renamed at
    the issue-07 generation switch).
    """

    index_text = render_memory_index_text(entries) if entries else ""

    def _render(names: list[str]) -> RenderedSkills:
        if MEMORY_INDEX_NAME not in names or not entries:
            return RenderedSkills(messages=[], selected_skills=[])
        return RenderedSkills(
            messages=[
                Message(role="user", content=[TextBlock(text=index_text)])
            ],
            selected_skills=[],
        )

    return _render


def memory_content_kind(entries: MemoryEntries) -> ContentKindSpec:
    """The memory kind's registry item — the WHOLE integration surface.

    Register this next to ``skill_content_kind`` in a
    ``ContentChannelRegistry`` and the index lives in the semi-stable
    segment (so compaction's dynamic-suffix summarisation never washes
    it out), with its ``content_hash`` recorded through the generic
    ``(kind, name)`` seam under the ``evolving`` policy the recordings
    carry.
    """
    index_hash = memory_index_hash(entries) if entries else None

    def _hashes(name: str) -> tuple[str, str] | None:
        if name != MEMORY_INDEX_NAME or index_hash is None:
            return None
        return (MEMORY_INDEX_VERSION, index_hash)

    return ContentKindSpec(
        kind=MEMORY_KIND,
        renderer=build_memory_renderer(entries),
        hashes=_hashes,
        policy=MEMORY_DRIFT_POLICY,
    )


def _tokens(value: str) -> set[str]:
    return {
        t for t in _TOKEN_RE.findall(value.lower()) if len(t) >= _MIN_TOKEN_LEN
    }


def match_memories(
    entries: MemoryEntries,
    text: str,
    *,
    max_hits: int = DEFAULT_RECALL_MAX_HITS,
) -> tuple[str, ...]:
    """Two-tier recall matching, pure and deterministic.

    Tier 1 (the v1 rule): a memory hits when any token of its NAME
    appears as a word in the user text (case-insensitive) — names are
    author-chosen slugs, so one shared token is high-signal. Tier 2: an
    entry NOT already hit by name hits when its SUMMARY shares at least
    ``_SUMMARY_MIN_OVERLAP`` distinct tokens with the text (prose needs
    more evidence than a slug). The ``type`` field never participates.

    Output order: all tier-1 hits in index (name-sorted) order, then all
    tier-2 hits in index order, capped at ``max_hits`` overall. Vector /
    semantic retrieval is out of scope (its backing service would arrive
    behind a D1-style adapter, swapping this function)."""
    text_tokens = _tokens(text)
    if not text_tokens:
        return ()
    name_hits: list[str] = []
    summary_hits: list[str] = []
    for name, summary, _type in entries:
        if _tokens(name) & text_tokens:
            name_hits.append(name)
        elif len(_tokens(summary) & text_tokens) >= _SUMMARY_MIN_OVERLAP:
            summary_hits.append(name)
    return tuple((name_hits + summary_hits)[:max_hits])


def format_recall_text(hits: tuple[tuple[str, str], ...]) -> str:
    """Render recalled ``(name, body)`` pairs into the single injected
    turn (ledgered with ``origin="memory"`` — attribution lives in the
    ledger; wire-format wrapping is the adapter's job).
    """
    parts = [
        "Recalled memories relevant to the latest user message:",
    ]
    for name, body in hits:
        parts.append("")
        parts.append(f"## {name}")
        parts.append(body)
    return "\n".join(parts)
