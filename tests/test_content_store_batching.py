"""Batch reads: a traversal fetches its bodies in one query, not N.

``fold`` and ``as_messages`` know every ref they will dereference before they
process a single event, so both collapse their reads into one ``get_many``.
Three pieces make that work and are covered here:

* ``PrefetchedContentStore`` / ``prefetched`` — the traversal-scoped view that
  lets per-event handlers keep calling ``get`` against bodies already in hand;
* the two callers' read behaviour — one batch, no per-ref fall-through, and
  (just as load-bearing) **no** prefetch of bodies the caller never reads;
* ``CachedContentStore`` — the cross-traversal LRU wrapped around durable
  backends, which is what collapses the *same* ref re-read by later folds.

Everything is asserted through the ContentStore interface: the ref scanners
themselves stay private, and a test that reached for them would pin the
implementation rather than the property (one batch, right bytes, no waste).
The backend-neutral ``get_many`` contract lives in
``test_content_store_contract``.
"""

from __future__ import annotations

from typing import Iterable

import pytest

from noeta.client.messages import as_messages
from noeta.client.storage_resolve import build_storage_stack
from noeta.core.fold import BoundedEventLog, fold
from noeta.core.prefetch import PrefetchedContentStore, prefetched
from noeta.core.snapshot import serialize_task_state, snapshot_media_type
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.content_store import ContentStore
from noeta.protocols.errors import ContentNotFound
from noeta.protocols.events import (
    AssistantThinkingRecordedPayload,
    EventEnvelope,
    MessagesAppendedPayload,
    StepAttemptAbandonedPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskRewoundPayload,
    ToolCallApprovalRequestedPayload,
    ToolCallStartedPayload,
    ToolResultRecordedPayload,
)
from noeta.protocols.messages import Message, TextBlock, ThinkingBlock
from noeta.protocols.task import Task
from noeta.protocols.values import ContentRef
from noeta.storage.cached import CachedContentStore
from noeta.storage.memory import InMemoryContentStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CountingContentStore:
    """Records what the layer above it actually asked the backend for."""

    def __init__(self, inner: ContentStore) -> None:
        self._inner = inner
        self.gets: list[str] = []
        self.batches: list[list[str]] = []

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        return self._inner.put(body, media_type=media_type)

    def get(self, ref: ContentRef) -> bytes:
        self.gets.append(ref.hash)
        return self._inner.get(ref)

    def get_many(self, refs: Iterable[ContentRef]) -> dict[str, bytes]:
        listed = list(refs)
        self.batches.append([ref.hash for ref in listed])
        return self._inner.get_many(listed)

    @property
    def requested(self) -> set[str]:
        return set(self.gets) | {h for batch in self.batches for h in batch}


def _stream(task_id: str, payloads: list[tuple[str, object]]) -> list[EventEnvelope]:
    return [
        EventEnvelope.build(task_id=task_id, type=event_type, payload=payload).with_seq(
            seq
        )
        for seq, (event_type, payload) in enumerate(payloads, start=1)
    ]


def _fold_stream(
    store: ContentStore,
) -> tuple[list[EventEnvelope], dict[str, ContentRef]]:
    """A stream carrying every body-bearing event fold can meet.

    The two re-base markers come first so the content events after them
    survive into the folded state (a re-base overwrites every slice).
    """
    refs = {
        "state": store.put(
            serialize_task_state(Task(task_id="t1")), media_type=snapshot_media_type()
        ),
        "messages": store.put(
            to_canonical_bytes([Message(role="user", content=[TextBlock(text="hi")])]),
            media_type="application/json",
        ),
        "thinking": store.put(
            to_canonical_bytes([ThinkingBlock(text="why")]),
            media_type="application/json",
        ),
        "arguments": store.put(b'{"path": "/tmp/x"}', media_type="application/json"),
        "output": store.put(b"tool output nobody folds", media_type="text/plain"),
    }
    events = _stream(
        "t1",
        [
            ("TaskCreated", TaskCreatedPayload(goal="g", policy_name="stub")),
            ("TaskRewound", TaskRewoundPayload(target_seq=1, state_ref=refs["state"])),
            (
                "StepAttemptAbandoned",
                StepAttemptAbandonedPayload(
                    abandoned_from_seq=2, state_ref=refs["state"], reason="crash"
                ),
            ),
            (
                "MessagesAppended",
                MessagesAppendedPayload(messages_ref=refs["messages"], count=1),
            ),
            (
                "AssistantThinkingRecorded",
                AssistantThinkingRecordedPayload(
                    call_id="c1", thinking_ref=refs["thinking"]
                ),
            ),
            (
                "ToolCallApprovalRequested",
                ToolCallApprovalRequestedPayload(
                    call_id="c1", tool_name="read", arguments_ref=refs["arguments"]
                ),
            ),
            (
                "ToolResultRecorded",
                ToolResultRecordedPayload(
                    call_id="c1",
                    success=True,
                    output_ref=refs["output"],
                    summary="ok",
                ),
            ),
        ],
    )
    return events, refs


def _fold(events: list[EventEnvelope], store: ContentStore) -> Task:
    # ``ignore_snapshots`` forces the from-scratch path so the re-base handlers
    # actually run; the accelerated path would start the tail after them.
    return fold(
        BoundedEventLog(events, max_seq=len(events)),
        store,
        "t1",
        ignore_snapshots=True,
    )


def _messages_stream(
    store: ContentStore,
) -> tuple[list[EventEnvelope], dict[str, ContentRef]]:
    refs = {
        "messages": store.put(
            to_canonical_bytes([Message(role="user", content=[TextBlock(text="hi")])]),
            media_type="application/json",
        ),
        "arguments": store.put(b'{"path": "/tmp/x"}', media_type="application/json"),
        "output": store.put(b"tool said so", media_type="text/plain"),
        "answer": store.put(
            to_canonical_bytes("the long answer"), media_type="application/json"
        ),
    }
    events = _stream(
        "t1",
        [
            (
                "MessagesAppended",
                MessagesAppendedPayload(messages_ref=refs["messages"], count=1),
            ),
            (
                "ToolCallStarted",
                ToolCallStartedPayload(
                    call_id="c1", tool_name="read", arguments_ref=refs["arguments"]
                ),
            ),
            (
                "ToolResultRecorded",
                ToolResultRecordedPayload(
                    call_id="c1", success=True, output_ref=refs["output"], summary="ok"
                ),
            ),
            ("TaskCompleted", TaskCompletedPayload(answer="", answer_ref=refs["answer"])),
        ],
    )
    return events, refs


# ---------------------------------------------------------------------------
# PrefetchedContentStore
# ---------------------------------------------------------------------------


def test_prefetched_serves_known_refs_without_touching_the_store() -> None:
    inner = InMemoryContentStore()
    ref = inner.put(b"body", media_type="text/plain")
    counting = _CountingContentStore(inner)

    view = prefetched(counting, [ref])
    counting.batches.clear()

    assert view.get(ref) == b"body"
    assert counting.gets == []
    assert counting.batches == []


def test_prefetched_falls_through_for_an_unpredicted_ref() -> None:
    """A ref scanner that misses an event type costs a round-trip, not a bug."""
    inner = InMemoryContentStore()
    known = inner.put(b"known", media_type="text/plain")
    unpredicted = inner.put(b"unpredicted", media_type="text/plain")
    counting = _CountingContentStore(inner)

    view = prefetched(counting, [known])

    assert view.get(unpredicted) == b"unpredicted"
    assert counting.gets == [unpredicted.hash]


def test_prefetched_preserves_content_not_found() -> None:
    inner = InMemoryContentStore()
    counting = _CountingContentStore(inner)
    absent = ContentRef(hash="0" * 64, size=0, media_type="text/plain")

    view = PrefetchedContentStore(counting, {})

    with pytest.raises(ContentNotFound):
        view.get(absent)


def test_prefetched_with_no_refs_returns_the_store_itself() -> None:
    """A traversal with no content-bearing events pays neither query nor wrapper."""
    store = InMemoryContentStore()
    assert prefetched(store, []) is store


def test_prefetched_get_many_mixes_local_bodies_with_a_fetch_for_the_rest() -> None:
    inner = InMemoryContentStore()
    local = inner.put(b"local", media_type="text/plain")
    remote = inner.put(b"remote", media_type="text/plain")
    counting = _CountingContentStore(inner)

    view = prefetched(counting, [local])
    counting.batches.clear()

    assert view.get_many([local, remote]) == {
        local.hash: b"local",
        remote.hash: b"remote",
    }
    assert counting.batches == [[remote.hash]]


# ---------------------------------------------------------------------------
# fold
# ---------------------------------------------------------------------------


def test_fold_reads_the_whole_tail_in_one_batch() -> None:
    inner = InMemoryContentStore()
    events, _refs = _fold_stream(inner)
    counting = _CountingContentStore(inner)

    _fold(events, counting)

    assert len(counting.batches) == 1
    assert counting.gets == []


def test_fold_does_not_prefetch_bodies_it_never_dereferences() -> None:
    """``ToolResultRecorded`` is a fold no-op and its body is among the largest
    in the store; batching it would trade N small reads for one big transfer
    nothing consumes."""
    inner = InMemoryContentStore()
    events, refs = _fold_stream(inner)
    counting = _CountingContentStore(inner)

    _fold(events, counting)

    assert refs["output"].hash not in counting.requested
    assert refs["messages"].hash in counting.requested


def test_fold_state_is_unchanged_by_batching() -> None:
    """The bytes each handler sees are the ones it saw before the batch read."""
    store = InMemoryContentStore()
    events, _refs = _fold_stream(store)

    task = _fold(events, store)

    assert [b.text for m in task.runtime.messages for b in m.content] == ["hi"]
    assert [b.text for b in task.context.thinking_by_call_id["c1"]] == ["why"]
    assert task.governance.pending_approvals["c1"]["arguments"] == {"path": "/tmp/x"}


# ---------------------------------------------------------------------------
# as_messages
# ---------------------------------------------------------------------------


def test_as_messages_reads_the_whole_stream_in_one_batch() -> None:
    inner = InMemoryContentStore()
    events, _refs = _messages_stream(inner)
    counting = _CountingContentStore(inner)

    as_messages(events, counting)

    assert len(counting.batches) == 1
    assert counting.gets == []


def test_as_messages_projection_is_unchanged_by_batching() -> None:
    store = InMemoryContentStore()
    events, _refs = _messages_stream(store)

    views = as_messages(events, store)

    rendered = [(type(v).__name__, getattr(v, "output", None)) for v in views]
    assert ("ToolResultView", "tool said so") in rendered
    assert [v for v in views if type(v).__name__ == "Result"][0].answer == (
        "the long answer"
    )


def test_as_messages_accepts_a_one_shot_iterator() -> None:
    """The stream is walked twice now (scan, then project); a generator input
    must not come back empty on the second pass."""
    store = InMemoryContentStore()
    events, _refs = _messages_stream(store)

    assert as_messages(iter(events), store) == as_messages(events, store)


# ---------------------------------------------------------------------------
# CachedContentStore
# ---------------------------------------------------------------------------


def test_cache_serves_a_repeated_get_without_a_second_backend_read() -> None:
    inner = InMemoryContentStore()
    ref = inner.put(b"body", media_type="text/plain")
    counting = _CountingContentStore(inner)
    cache = CachedContentStore(counting)

    assert cache.get(ref) == cache.get(ref) == b"body"
    assert counting.gets == [ref.hash]


def test_cache_asks_the_backend_only_for_batch_misses() -> None:
    inner = InMemoryContentStore()
    hot = inner.put(b"hot", media_type="text/plain")
    cold = inner.put(b"cold", media_type="text/plain")
    counting = _CountingContentStore(inner)
    cache = CachedContentStore(counting)
    cache.get(hot)
    counting.gets.clear()

    assert cache.get_many([hot, cold]) == {hot.hash: b"hot", cold.hash: b"cold"}
    assert counting.batches == [[cold.hash]]


def test_cache_admits_bodies_on_put_so_the_read_back_is_free() -> None:
    """The Engine snapshots then refolds, and every message body it appends is
    read back by the next fold of the same tail."""
    counting = _CountingContentStore(InMemoryContentStore())
    cache = CachedContentStore(counting)

    ref = cache.put(b"written", media_type="text/plain")

    assert cache.get(ref) == b"written"
    assert counting.gets == []


def test_cache_refuses_bodies_over_the_entry_ceiling() -> None:
    """One oversized body must not evict the whole working set to be held once."""
    inner = InMemoryContentStore()
    counting = _CountingContentStore(inner)
    cache = CachedContentStore(counting, max_entry_bytes=4)
    big = inner.put(b"x" * 100, media_type="text/plain")

    cache.get(big)
    cache.get(big)

    assert counting.gets == [big.hash, big.hash]


def test_cache_evicts_least_recently_used_when_the_byte_budget_is_full() -> None:
    inner = InMemoryContentStore()
    first = inner.put(b"aaa", media_type="text/plain")
    second = inner.put(b"bbb", media_type="text/plain")
    third = inner.put(b"ccc", media_type="text/plain")
    counting = _CountingContentStore(inner)
    cache = CachedContentStore(counting, max_bytes=6)

    cache.get(first)
    cache.get(second)
    cache.get(first)  # first is now the most recently used
    cache.get(third)  # admitting this evicts second
    counting.gets.clear()

    cache.get(first)
    cache.get(third)
    assert counting.gets == []

    cache.get(second)
    assert counting.gets == [second.hash]


def test_cache_does_not_remember_a_miss() -> None:
    """Content is immutable but not eternal in both directions — a hash absent
    now can be put later, and the cache must not answer for the backend."""
    inner = InMemoryContentStore()
    cache = CachedContentStore(inner)
    absent = ContentRef(hash="0" * 64, size=0, media_type="text/plain")

    with pytest.raises(ContentNotFound):
        cache.get(absent)

    real = cache.put(b"later", media_type="text/plain")
    assert cache.get(real) == b"later"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_durable_backends_come_back_cached(tmp_path) -> None:
    _log, content_store, _dispatcher = build_storage_stack(
        "sqlite", path=str(tmp_path / "s.sqlite")
    )
    assert isinstance(content_store, CachedContentStore)


def test_the_memory_backend_is_left_bare() -> None:
    """It is already an in-process dict; wrapping it would hold every body twice."""
    _log, content_store, _dispatcher = build_storage_stack("memory")
    assert isinstance(content_store, InMemoryContentStore)
