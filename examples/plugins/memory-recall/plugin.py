"""First-party example manifest plugin — ``memory-recall``: a RAG-style
(track A) ``reminder_provider`` with a stub retriever.

Demonstrated SDK capability
---------------------------
The new ``reminder_provider`` surface — **track A** of the SDK-extensibility
redesign (``docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md``,
D7): *recorded injection*. At a named recording seam the provider receives a
narrow read-only ``RecallView`` (task id, incoming message, a ``TaskState``
projection, workspace path) and returns zero or more reminders. Because the
output is **recorded** through the Engine's sole origin-writer seam, the provider
**may be impure** — query a vector DB, an external RAG index, a memory store —
and resume/replay folds the reminder back **from the ledger, never re-invoking
the provider**. This is the seam that opens RAG-backed memory plugins; noeta's
own memory auto-recall is the built-in tenant of the same surface.

This example wires a **stub retriever** (:class:`StubRetriever`) — a tiny
in-process keyword-overlap "vector store" over a fixed corpus — in place of a
real embedding index, so the shape is complete and offline-runnable. Swap
:data:`RETRIEVER` for a real client and the provider is production-shaped.

Seams and ordering
------------------
The provider is bound to the ``turn_intake`` seam (a user message being
recorded). Multiple providers on one seam run in ``(plugin, name)`` order; a
provider raise fails the turn loudly (a provider that prefers degradation
catches internally — this one does).

Public-surface note
-------------------
A shipped plugin returns noeta's ``Reminder(text, origin)``. To keep this example
on the ``noeta.sdk`` public surface only (the recorded-reminder type is a runtime
internal), it returns a **structural** stand-in — a ``(text, origin)`` pair the
recording seam reads by duck typing. ``origin`` is ``"memory"`` (recalled
cross-task material); the seam is the single writer of that author tag.
"""

from __future__ import annotations

import re
from collections import namedtuple
from typing import Any

from noeta.sdk import PluginBuilder


#: A structural stand-in for noeta's recorded-reminder contract (``text`` +
#: ``origin``). The ``turn_intake`` recording seam reads these two fields by duck
#: typing; a shipped plugin returns ``noeta``'s ``Reminder`` instead.
Recalled = namedtuple("Recalled", ["text", "origin"])


#: How many recalled notes to surface at most per turn.
TOP_K = 2


class StubRetriever:
    """A tiny deterministic keyword-overlap retriever standing in for a vector DB.

    ``query`` scores each corpus note by the size of its token overlap with the
    incoming text and returns the top matches (score-desc, id-asc for stable
    ties). Deterministic and offline — a real plugin swaps this for an embedding
    index while keeping the same ``query(text) -> [(id, text, score)]`` shape.
    """

    def __init__(self, corpus: dict[str, str]) -> None:
        self._corpus = dict(corpus)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}

    def query(self, text: str, *, k: int = TOP_K) -> list[tuple[str, str, int]]:
        query_tokens = self._tokens(text)
        scored = [
            (doc_id, body, len(query_tokens & self._tokens(body)))
            for doc_id, body in self._corpus.items()
        ]
        hits = [row for row in scored if row[2] > 0]
        hits.sort(key=lambda row: (-row[2], row[0]))
        return hits[:k]


#: The stub corpus of cross-task "memories" the retriever searches. A real plugin
#: points :data:`RETRIEVER` at its own index.
CORPUS: dict[str, str] = {
    "deploy": "Deploys go through the staging gate first; never push straight to prod.",
    "db": "The analytics database is read-replica only — writes must target the primary.",
    "style": "Public API errors use the CodedError base with a stable string code.",
}

#: The live retriever the provider queries. Swap for a real client in production.
RETRIEVER = StubRetriever(CORPUS)


def rag_recall(view: Any) -> list:
    """Recall relevant notes for the incoming turn (impure; output is recorded).

    ``view`` is the seam's ``RecallView``; ``view.text`` is the recall key (the
    incoming message's concatenated text). Returns zero or more structural
    reminders tagged ``origin="memory"``. Impure by contract (it may query an
    external index); it catches nothing here because the stub cannot fail, but a
    provider that prefers degradation over a loud turn-failure catches internally.
    """
    hits = RETRIEVER.query(getattr(view, "text", "") or "")
    if not hits:
        return []
    lines = [f"- {body}" for _id, body, _score in hits]
    text = "Relevant notes recalled from memory:\n" + "\n".join(lines)
    return [Recalled(text=text, origin="memory")]


#: The single-file manifest (decorator sugar *is* the manifest, spec D1). The
#: contributed provider is cached for single-file resolution; a distributed
#: install exposes it at ``memory_recall:rag_recall``. ``seams`` names the
#: recording seam(s) it binds to. ``python -m noeta.sdk.plugin_check`` derives the
#: TOML from this builder.
plugin = PluginBuilder("memory-recall", requires_noeta=">=0.4")
plugin.reminder_provider(rag_recall, name="rag-recall", seams=["turn_intake"])
