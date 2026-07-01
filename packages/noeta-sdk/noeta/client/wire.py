"""Wire serialization for the SDK envelope stream.

An app that bridges the envelope stream to a browser sends **EventEnvelope
verbatim**. Serializing an envelope to a
JSON-friendly dict is a protocol concern, so it lives on the SDK surface
(re-exported by ``noeta.sdk``) rather than being re-implemented per app.

The payload is canonicalised (``noeta.protocols.canonical.to_canonical``):
``ContentRef`` members become structural ``{"hash","size","media_type"}`` dicts
and dataclass payloads carry ``__canonical_tag__`` — the same shape the legacy
backend wired, so a folding frontend sees identical bytes.
"""

from __future__ import annotations

from typing import Any

from noeta.protocols.canonical import to_canonical
from noeta.protocols.event_log import EventEnvelope


__all__ = ["envelope_to_dict"]


def envelope_to_dict(env: EventEnvelope) -> dict[str, Any]:
    """Render one :class:`EventEnvelope` as a JSON-serializable dict."""
    return {
        "id": env.id,
        "task_id": env.task_id,
        "seq": env.seq,
        "type": env.type,
        "schema_version": env.schema_version,
        "occurred_at": env.occurred_at,
        "actor": env.actor,
        "trace_id": env.trace_id,
        "correlation_id": env.correlation_id,
        "causation_id": env.causation_id,
        "origin": env.origin,
        "payload": to_canonical(env.payload),
    }
