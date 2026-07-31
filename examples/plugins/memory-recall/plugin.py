"""RAG-style recall of cross-task notes, injected as the turn is recorded.

Demonstrated SDK capability: the ``reminder_provider`` surface — *recorded*
injection. At a named seam the provider sees a narrow read-only view of the
incoming turn and returns zero or more reminders, and because that output goes
through the Engine's sole origin-writer seam it lands in the ledger. This is what
lets a provider be **impure**: query a vector store, a RAG index, a memory
service. Resume and replay fold the reminder back from the ledger and never
re-invoke the provider, so a retrieval that returns different results tomorrow
cannot rewrite yesterday's task. Anything pure and deterministic belongs on the
``reminder`` surface instead.

The retriever here is a deterministic keyword-overlap stand-in for an embedding
index, so the shape is complete and the example runs offline; a real plugin
points :data:`RETRIEVER` at its own index and changes nothing else.
"""

from __future__ import annotations

import re
from collections import namedtuple
from typing import Any

from noeta.sdk import PluginBuilder


#: The seam reads ``text`` and ``origin`` by duck typing, so this example can
#: stand in for noeta's recorded-reminder type and stay clear of runtime
#: internals. A shipped plugin returns noeta's ``Reminder`` instead.
Recalled = namedtuple("Recalled", ["text", "origin"])


#: Capped low on purpose: recall competes with the turn's own content for the
#: model's attention, and a long recall block reliably wins that fight.
TOP_K = 2


class StubRetriever:
    """A keyword-overlap retriever standing in for a vector store.

    Ties break on id so the example is reproducible in tests; a real embedding
    index has no such obligation, since the provider's output is recorded and
    replay reads the ledger rather than re-querying.
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


#: A stand-in corpus of cross-task "memories" — the kind of standing constraint
#: a team would otherwise repeat in every prompt.
CORPUS: dict[str, str] = {
    "deploy": "Deploys go through the staging gate first; never push straight to prod.",
    "db": "The analytics database is read-replica only — writes must target the primary.",
    "style": "Public API errors use the CodedError base with a stable string code.",
}

#: The one seam a real deployment repoints at its own index.
RETRIEVER = StubRetriever(CORPUS)


def rag_recall(view: Any) -> list:
    """Recall relevant notes for the incoming turn.

    Tagged ``origin="memory"`` because the seam is the single writer of that
    author tag — recalled material must be distinguishable from what the user
    actually said.

    Nothing is caught here only because the stub cannot fail. A provider over a
    real index chooses: let the exception fail the turn loudly, or catch and
    degrade to no recall. Silence is the safer default for retrieval.
    """
    hits = RETRIEVER.query(getattr(view, "text", "") or "")
    if not hits:
        return []
    lines = [f"- {body}" for _id, body, _score in hits]
    text = "Relevant notes recalled from memory:\n" + "\n".join(lines)
    return [Recalled(text=text, origin="memory")]


#: The builder *is* this plugin's manifest, and its name is the plugin identity
#: — the activation key, not the filename. ``python -m noeta.sdk.plugin_check``
#: derives TOML from it and verifies the shipped ``noeta-plugin.toml`` matches.
#:
#: ``seams`` names where the provider runs. ``turn_intake`` is the moment a user
#: message is being recorded — the only point at which injected material can
#: enter the ledger alongside the turn it belongs to.
plugin = PluginBuilder("memory-recall", requires_noeta=">=0.4")
plugin.reminder_provider(rag_recall, name="rag-recall", seams=["turn_intake"])
