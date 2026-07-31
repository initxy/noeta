"""Wire serialization for the SDK envelope stream.

A host that bridges envelopes to a browser sends them verbatim, so the JSON
shape is a protocol concern and belongs on the SDK surface rather than being
re-implemented per host. Payloads are canonicalised — tagged values such as
``ContentRef`` keep their ``__canonical_tag__`` so a frontend can dereference
them by hash, and a folding frontend agrees with a server-side fold because
both see the shape of the durable bytes.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from noeta.protocols.canonical import to_canonical
from noeta.protocols.event_log import EventEnvelope


__all__ = ["envelope_to_dict"]


def envelope_to_dict(env: EventEnvelope) -> dict[str, Any]:
    """Render one :class:`EventEnvelope` as a JSON-serializable dict.

    Driven by ``dataclasses.fields`` because the contract is "a folding
    frontend sees every field the durable record holds"; a hand-written field
    list would silently drop one. ``payload`` is the only field needing
    canonicalisation — the rest are JSON scalars.
    """
    return {
        f.name: (
            to_canonical(getattr(env, f.name)) if f.name == "payload" else getattr(env, f.name)
        )
        for f in dataclasses.fields(env)
    }
