"""Memory index — the content channel's SECOND resident (vocabulary).

Phase 2c: this module keeps only the kind **vocabulary** — the channel
constants and the ``MemoryEntries`` snapshot type — shared by the kernel
recording seam (``noeta.execution.memory``), fold, and the ``memory``
built-in plugin. The renderer prose, hash rule, ``ContentKindSpec``
factory, and the recall matching/formatting live in
``noeta.builtins.memory.impl.index`` and reach the kernel only through
the injected :class:`noeta.execution.memory.MemoryIndexKit` (the
reminders-builtin pattern: registry mechanism kernel-side, rendered
material in the plugin).

Two deliberate contrasts with skills (unchanged):

* **Drift policy is ``evolving``** — a memory edit is daily business, so
  the recording carries the ``evolving`` policy: the index ``content_hash``
  is recorded as provenance but is free to move (the ``pinned`` policy a
  skill carries instead would mark a moved hash as drift). This is why
  memories are NOT disguised as dynamically-generated skills.
* **The index is the only resident body** — full memory texts stay on
  disk; the model pulls them through the ordinary ``memory_read`` tool
  or receives them via host recall.
"""

from __future__ import annotations


__all__ = [
    "MEMORY_DRIFT_POLICY",
    "MEMORY_INDEX_NAME",
    "MEMORY_INDEX_VERSION",
    "MEMORY_KIND",
    "MemoryEntries",
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

#: The index source shape: ``(name, summary, type)`` triples, sorted by
#: name (``MemoryStore.entries()`` produces exactly this). ``summary`` is
#: the frontmatter description or the first non-empty body line; ``type``
#: is the validated frontmatter type or ``""``.
MemoryEntries = tuple[tuple[str, str, str], ...]
