"""Wire serialization for the SDK envelope stream.

An app that bridges the envelope stream to a browser sends **EventEnvelope
verbatim**. Serializing an envelope to a
JSON-friendly dict is a protocol concern, so it lives on the SDK surface
(re-exported by ``noeta.sdk``) rather than being re-implemented per app.

The payload is canonicalised (``noeta.protocols.canonical.to_canonical``):
event payload dataclasses become plain field dicts, and the *tagged* value
types nested inside them (``ContentRef`` and friends) become structural dicts
carrying their ``__canonical_tag__`` — e.g. a ``ContentRef`` renders as
``{"__canonical_tag__": "content_ref", "hash": …, "size": …, "media_type": …}``,
so a frontend can dereference it by hash. This is exactly the shape of the
durable bytes, so a folding frontend and a server-side fold agree.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from noeta.protocols.canonical import to_canonical
from noeta.protocols.event_log import EventEnvelope


__all__ = ["envelope_to_dict"]


def envelope_to_dict(env: EventEnvelope) -> dict[str, Any]:
    """Render one :class:`EventEnvelope` as a JSON-serializable dict.

    Driven by ``dataclasses.fields`` so a field added to the envelope is
    carried automatically — this module's whole contract is "a folding
    frontend sees every field the durable record holds", and a hand-written
    field list would silently drop a new one. ``payload`` is the one field
    that needs rendering (canonicalisation); every other field is a JSON
    scalar already.
    """
    return {
        f.name: (
            to_canonical(getattr(env, f.name)) if f.name == "payload" else getattr(env, f.name)
        )
        for f in dataclasses.fields(env)
    }
