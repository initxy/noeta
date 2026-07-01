"""Compaction event payloads + fold (③ D-3, unified compaction contract).

Two new event types carry the unified compaction step:

* ``CompactionRequested`` — observability anchor for *why* a compaction
  step ran (``reason`` ∈ {"overflow", "proactive"}); fold is a no-op
  (it changes no state slice).
* ``Compacted`` — the durable result: history up to ``boundary_count``
  messages is replaced by one summary message whose body lives behind
  ``summary_ref``. Fold writes the summary slice onto
  ``ContextState`` (single writer), which the Composer then
  reads to swap the covered prefix for the summary.

Both restore byte-safe on old streams (the types are simply absent →
fold's unknown-type tolerance). New typed payloads must register in
``_PAYLOAD_RESTORERS`` (the contract suite enforces it).
"""

from __future__ import annotations

from noeta.core.fold import _HANDLERS
from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.events import CompactedPayload, CompactionRequestedPayload
from noeta.protocols.task import Task
from noeta.protocols.values import ContentRef
from noeta.storage.memory import InMemoryContentStore


def _ref(h: str) -> ContentRef:
    return ContentRef(hash=h, size=1, media_type="application/json")


def test_compaction_requested_payload_restorer_round_trip() -> None:
    """Outer payloads are reconstructed via ``_PAYLOAD_RESTORERS`` (they
    carry no canonical tag); the canonical body is a plain dict and the
    sqlite restorer rebuilds the typed payload from it."""
    from noeta.storage.sqlite.eventlog import _PAYLOAD_RESTORERS

    p = CompactionRequestedPayload(reason="overflow", estimated_tokens=900_000)
    body = from_canonical_bytes(to_canonical_bytes(p))
    restored = _PAYLOAD_RESTORERS["CompactionRequested"](body)
    assert restored == p


def test_compacted_payload_restorer_round_trip() -> None:
    from noeta.storage.sqlite.eventlog import _PAYLOAD_RESTORERS

    p = CompactedPayload(
        summary_ref=_ref("sha256:abc"),
        boundary_count=12,
        replaced_count=12,
        composer_version="three_segment.v3",
    )
    body = from_canonical_bytes(to_canonical_bytes(p))
    restored = _PAYLOAD_RESTORERS["Compacted"](body)
    assert restored == p


def test_payload_restorers_registered_for_both() -> None:
    from noeta.storage.sqlite.eventlog import _PAYLOAD_RESTORERS

    assert "CompactionRequested" in _PAYLOAD_RESTORERS
    assert "Compacted" in _PAYLOAD_RESTORERS


def test_fold_handlers_registered() -> None:
    assert "CompactionRequested" in _HANDLERS
    assert "Compacted" in _HANDLERS


def _envelope(payload: object, type_: str) -> object:
    from noeta.protocols.events import EventEnvelope

    return EventEnvelope.build(task_id="t-1", type=type_, payload=payload)


def test_fold_compaction_requested_is_noop() -> None:
    task = Task(task_id="t-1")
    before = task.context.summary_ref
    env = _envelope(
        CompactionRequestedPayload(reason="overflow", estimated_tokens=1),
        "CompactionRequested",
    )
    _HANDLERS["CompactionRequested"](task, env, InMemoryContentStore())
    assert task.context.summary_ref == before  # unchanged


def test_fold_compacted_writes_summary_slice() -> None:
    task = Task(task_id="t-1")
    ref = _ref("sha256:deef")
    env = _envelope(
        CompactedPayload(
            summary_ref=ref,
            boundary_count=7,
            replaced_count=7,
            composer_version="three_segment.v3",
        ),
        "Compacted",
    )
    _HANDLERS["Compacted"](task, env, InMemoryContentStore())
    assert task.context.summary_ref == ref
    assert task.context.summary_boundary == 7
