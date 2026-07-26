"""``envelope_to_dict`` — the SDK's envelope→JSON wire shape.

This is the format a product's SSE endpoint pushes to the browser
(``noeta.agent.host.service`` re-exports it through ``noeta.sdk``), so its
contract is a public one: **every** envelope field is carried, under its own
name, and the payload is canonicalised exactly like the durable bytes — a
``ContentRef`` becomes the structural ``{"hash","size","media_type"}`` dict and a
dataclass payload carries ``__canonical_tag__``. A folding frontend reads these
dicts as if they were the recording.

It had no tests: a dropped field or a payload rendered as a repr would ship
straight to every consumer of the stream.
"""

from __future__ import annotations

import dataclasses
import json

from noeta.protocols.events import EventEnvelope
from noeta.protocols.values import ContentRef
from noeta.sdk import envelope_to_dict


def _envelope(payload: object = None, **overrides: object) -> EventEnvelope:
    fields: dict[str, object] = {
        "id": "evt-1",
        "task_id": "t-1",
        "seq": 7,
        "type": "TaskCreated",
        "schema_version": 1,
        "occurred_at": 1234.5,
        "actor": "engine",
        "trace_id": "tr-1",
        "correlation_id": "t-1",
        "causation_id": None,
        "payload": payload,
        "origin": "engine",
    }
    fields.update(overrides)
    return EventEnvelope(**fields)  # type: ignore[arg-type]


def test_every_envelope_field_is_carried() -> None:
    """No field may be silently dropped: the wire dict's keys are exactly the
    envelope's own fields (``payload`` canonicalised, the rest verbatim)."""
    out = envelope_to_dict(_envelope(payload={"k": "v"}))
    assert set(out) == {f.name for f in dataclasses.fields(EventEnvelope)}
    assert out == {
        "id": "evt-1",
        "task_id": "t-1",
        "seq": 7,
        "type": "TaskCreated",
        "schema_version": 1,
        "occurred_at": 1234.5,
        "actor": "engine",
        "trace_id": "tr-1",
        "correlation_id": "t-1",
        "causation_id": None,
        "origin": "engine",
        "payload": {"k": "v"},
    }


def test_result_is_json_serializable() -> None:
    """The dict goes straight into an SSE ``data:`` line — it must survive
    ``json.dumps`` with no custom encoder."""
    ref = ContentRef(hash="abc", size=3, media_type="text/plain")
    out = envelope_to_dict(_envelope(payload={"ref": ref}))
    assert json.loads(json.dumps(out))["payload"]["ref"]["hash"] == "abc"


def test_content_ref_payload_is_structural_not_a_repr() -> None:
    """A ``ContentRef`` renders as its structural fields — a frontend
    dereferences by hash, so a repr string would break every content fetch."""
    ref = ContentRef(hash="h1", size=42, media_type="text/markdown")
    out = envelope_to_dict(_envelope(payload=ref))
    assert out["payload"] == {
        "__canonical_tag__": "content_ref",
        "hash": "h1",
        "size": 42,
        "media_type": "text/markdown",
    }


def test_event_payload_dataclass_becomes_a_plain_field_dict() -> None:
    """An event payload dataclass is UNtagged — it renders as its plain fields.
    The envelope's own ``type`` is the discriminator; only the nested tagged
    value types (``ContentRef`` etc.) carry ``__canonical_tag__``."""
    from noeta.protocols.events import TaskCreatedPayload

    payload = TaskCreatedPayload(goal="ship it", policy_name="react")
    out = envelope_to_dict(_envelope(payload=payload))
    assert "__canonical_tag__" not in out["payload"]
    assert out["payload"]["goal"] == "ship it"
    assert out["payload"]["policy_name"] == "react"


def test_none_payload_survives() -> None:
    """Some events carry no payload; ``None`` must stay ``None`` (not become
    the string ``"None"`` or vanish)."""
    out = envelope_to_dict(_envelope(payload=None))
    assert out["payload"] is None


def test_nested_content_ref_inside_a_dataclass_payload() -> None:
    """The canonicalisation is recursive: a ``ContentRef`` nested in a typed
    payload is structural too, not a repr."""
    from noeta.protocols.events import ToolResultRecordedPayload

    ref = ContentRef(hash="deep", size=9, media_type="application/json")
    payload = ToolResultRecordedPayload(
        call_id="c-1", success=True, output_ref=ref, summary="ok"
    )
    out = envelope_to_dict(_envelope(payload=payload))
    assert out["payload"]["output_ref"] == {
        "__canonical_tag__": "content_ref",
        "hash": "deep",
        "size": 9,
        "media_type": "application/json",
    }


def test_wire_payload_matches_the_durable_canonical_bytes() -> None:
    """The wire dict's payload is the SAME canonical value the event log
    records — that equality is why a browser fold and a server fold agree."""
    from noeta.protocols.canonical import to_canonical
    from noeta.protocols.events import TaskCreatedPayload

    payload = TaskCreatedPayload(goal="ship it", policy_name="react")
    assert envelope_to_dict(_envelope(payload=payload))["payload"] == to_canonical(
        payload
    )
