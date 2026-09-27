"""Content garbage collection (``noeta.storage.gc`` / ``Client.collect_garbage``).

The two retention rules and the one safety property, end to end over a real
Client: an LLM request body is never retained (and not stored by default), only
a task's latest ``TaskSnapshot`` body is, everything an event references —
directly or through a body — stays, and a reclaimed snapshot body makes fold
rebuild from the events instead of failing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import pytest

from noeta.core.fold import fold
from noeta.core.snapshot import serialize_task_state
from noeta.protocols.errors import ContentNotFound
from noeta.protocols.values import ContentRef
from noeta.sdk import (
    Client,
    HostConfig,
    LLMResponse,
    Options,
    TextBlock,
    ToolContext,
    ToolResult,
    ToolUseBlock,
    Usage,
    tool,
)
from noeta.sdk.storage import collect_garbage
from noeta.sdk.testing import FakeLLMProvider
from noeta.storage.cached import CachedContentStore
from noeta.storage.gc import live_content_hashes
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


_BIG = 20_000
_ARTIFACT_BODY = b"artifact-" + b"a" * 4_000


@tool(
    name="big_read",
    version="1",
    risk_level="low",
    description="Return a big blob and a pointer to an artifact.",
    input_schema={
        "type": "object",
        "properties": {"i": {"type": "integer"}},
        "additionalProperties": False,
    },
)
def big_read(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    # The artifact is referenced ONLY from inside the output body — no
    # ``artifacts=`` list — so keeping it exercises the transitive scan.
    art = ctx.artifact_store.put(_ARTIFACT_BODY, media_type="text/plain")
    return ToolResult(
        success=True,
        output={
            "i": arguments.get("i", 0),
            "text": "x" * _BIG,
            "artifact": {"hash": art.hash, "size": art.size, "media_type": art.media_type},
        },
    )


def _artifact_hash() -> str:
    return hashlib.sha256(_ARTIFACT_BODY).hexdigest()


def _call(call_id: str, i: int) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name="big_read", arguments={"i": i})],
        usage=Usage(uncached=1, output=1),
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _responses(turns: int, *, salt: int = 0) -> list[LLMResponse]:
    out: list[LLMResponse] = []
    for i in range(turns):
        out.append(_call(f"c{salt}-{i}", salt * 100 + i))
        out.append(_end(f"done-{salt}-{i}"))
    return out


def _client(
    workspace: Path,
    *,
    storage_path: str | None,
    responses: list[LLMResponse],
    record_llm_requests: bool = False,
) -> Client:
    return Client(
        Options(
            system_prompt="finish",
            name="main",
            allowed_tools=(big_read,),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(responses=responses),
        workspace_dir=workspace,
        multi_turn=True,
        host_config=HostConfig(
            storage_path=storage_path, record_llm_requests=record_llm_requests
        ),
    )


def _refs(client: Client, task_id: str, event_type: str, field: str) -> list[ContentRef]:
    return [
        getattr(e.payload, field)
        for e in client.events(task_id)
        if e.type == event_type
    ]


def _present(client: Client, refs: Iterable[ContentRef]) -> list[bool]:
    return [client.get_content(ref.hash) is not None for ref in refs]


def _page_bytes(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        size = conn.execute("PRAGMA page_size").fetchone()[0]
        return int(pages) * int(size)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rule 1: request bodies
# ---------------------------------------------------------------------------


def test_request_bodies_are_not_stored_by_default(tmp_path: Path) -> None:
    client = _client(tmp_path, storage_path=None, responses=_responses(1))
    try:
        t = client.start(goal="go").task_id
        request_refs = _refs(client, t, "LLMRequestStarted", "request_ref")
        assert len(request_refs) == 2
        assert _present(client, request_refs) == [False, False]
        # Every ref still carries a real identity, and responses are kept.
        assert all(len(ref.hash) == 64 and ref.size > 0 for ref in request_refs)
        response_refs = _refs(client, t, "LLMResponseRecorded", "response_ref")
        assert all(_present(client, response_refs))
    finally:
        client.shutdown()


def test_record_llm_requests_stores_bodies_and_a_sweep_reclaims_them(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        storage_path=str(tmp_path / "t.sqlite"),
        responses=_responses(1),
        record_llm_requests=True,
    )
    try:
        t = client.start(goal="go").task_id
        request_refs = _refs(client, t, "LLMRequestStarted", "request_ref")
        assert _present(client, request_refs) == [True, True]
        events_before = len(client.events(t))

        # Inside the grace nothing goes — the bodies were just written.
        kept = client.collect_garbage(grace_seconds=3600.0)
        assert kept.ok and kept.swept == 0 and kept.bytes_freed == 0
        assert _present(client, request_refs) == [True, True]

        result = client.collect_garbage(grace_seconds=0.0)
        assert result.ok and result.swept >= 2
        assert result.bytes_freed >= sum(ref.size for ref in request_refs)
        assert _present(client, request_refs) == [False, False]
        # The events themselves are untouched; the transcript still folds.
        assert len(client.events(t)) == events_before
        assert client.messages(t)
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# Rule 2: superseded snapshots — and the conversation survives the sweep
# ---------------------------------------------------------------------------


def test_sweep_keeps_latest_snapshot_and_conversation_still_resumes(
    tmp_path: Path,
) -> None:
    db = tmp_path / "t.sqlite"
    client = _client(tmp_path, storage_path=str(db), responses=_responses(3))
    try:
        t = client.start(goal="go").task_id
        client.send_goal(t, goal="again")
        snapshots = _refs(client, t, "TaskSnapshot", "state_ref")
        assert len(snapshots) >= 2
        assert all(_present(client, snapshots))
        message_refs = _refs(client, t, "MessagesAppended", "messages_ref")
        output_refs = _refs(client, t, "ToolResultRecorded", "output_ref")

        result = client.collect_garbage(grace_seconds=0.0)
        assert result.ok
        assert result.swept >= len(snapshots) - 1
        assert result.live > 0
        present = _present(client, snapshots)
        assert present[-1] is True and not any(present[:-1])
        # Everything the events still point at stays.
        assert all(_present(client, message_refs))
        assert all(_present(client, output_refs))
        # The artifact is reachable only through the tool output body.
        assert client.get_content(_artifact_hash()) is not None

        # Read models and a third turn work on the swept store.
        assert client.messages(t)
        assert [row["task_id"] for row in client.task_summaries()] == [t]
        host = client._host
        assert serialize_task_state(
            fold(host.event_log, host.content_store, t)
        ) == serialize_task_state(
            fold(host.event_log, host.content_store, t, ignore_snapshots=True)
        )
        outcome = client.send_goal(t, goal="third")
        assert outcome.status != "terminal"
    finally:
        client.shutdown()

    # A fresh Client over the same file folds the task from what is left.
    again = _client(tmp_path, storage_path=str(db), responses=_responses(1, salt=9))
    try:
        assert again.messages(t)
        status = again.task_status(t)
        assert status is not None and status.task_id == t
    finally:
        again.shutdown()


def test_fold_falls_back_to_the_events_when_the_snapshot_body_is_gone(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, storage_path=None, responses=_responses(2))
    try:
        t = client.start(goal="go").task_id
        client.send_goal(t, goal="again")
        host = client._host
        store = host.content_store
        assert isinstance(store, InMemoryContentStore)
        expected = serialize_task_state(
            fold(host.event_log, store, t, ignore_snapshots=True)
        )
        latest = _refs(client, t, "TaskSnapshot", "state_ref")[-1]
        del store._blobs[latest.hash]

        assert serialize_task_state(fold(host.event_log, store, t)) == expected
        assert client.messages(t)
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# Rule 3: everything else an event references stays; delete_task orphans go
# ---------------------------------------------------------------------------


def test_delete_task_then_sweep_reclaims_private_blobs_and_keeps_shared(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        storage_path=str(tmp_path / "t.sqlite"),
        responses=_responses(1, salt=1) + _responses(1, salt=2),
    )
    try:
        # Same goal text ⇒ the goal message body is one shared blob; the tool
        # outputs differ (``i``) ⇒ each task's output body is private.
        t1 = client.start(goal="alpha").task_id
        t2 = client.start(goal="alpha").task_id
        goal_refs_1 = _refs(client, t1, "MessagesAppended", "messages_ref")
        goal_refs_2 = _refs(client, t2, "MessagesAppended", "messages_ref")
        shared = {r.hash for r in goal_refs_1} & {r.hash for r in goal_refs_2}
        assert shared
        private_1 = _refs(client, t1, "ToolResultRecorded", "output_ref")
        assert private_1
        assert {r.hash for r in private_1}.isdisjoint(
            {r.hash for r in _refs(client, t2, "ToolResultRecorded", "output_ref")}
        )

        assert client.delete_task(t1)["ok"] is True
        result = client.collect_garbage(grace_seconds=0.0)
        assert result.ok and result.swept >= len(private_1)
        assert _present(client, private_1) == [False] * len(private_1)
        assert all(client.get_content(h) is not None for h in shared)
        assert client.messages(t2)
    finally:
        client.shutdown()


def test_live_content_hashes_applies_both_exclusions(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        storage_path=None,
        responses=_responses(2),
        record_llm_requests=True,
    )
    try:
        t = client.start(goal="go").task_id
        client.send_goal(t, goal="again")
        host = client._host
        live = live_content_hashes(host.event_log, host.content_store)

        for ref in _refs(client, t, "LLMRequestStarted", "request_ref"):
            assert ref.hash not in live
        snapshots = _refs(client, t, "TaskSnapshot", "state_ref")
        assert snapshots[-1].hash in live
        assert all(s.hash not in live for s in snapshots[:-1])
        for ref in _refs(client, t, "MessagesAppended", "messages_ref"):
            assert ref.hash in live
        for ref in _refs(client, t, "LLMResponseRecorded", "response_ref"):
            assert ref.hash in live
        assert _artifact_hash() in live

        # The memory backend sweeps too, so a test host can exercise the call.
        result = client.collect_garbage(grace_seconds=0.0)
        assert result.ok and result.swept > 0 and result.vacuumed is False
        assert client.messages(t)
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# The sweep itself: vacuum, unsupported stores, the cache, argument checks
# ---------------------------------------------------------------------------


def test_vacuum_shrinks_the_sqlite_file(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    client = _client(
        tmp_path,
        storage_path=str(db),
        responses=_responses(3),
        record_llm_requests=True,
    )
    try:
        t = client.start(goal="go").task_id
        client.send_goal(t, goal="again")
        client.send_goal(t, goal="more")
    finally:
        client.shutdown()
    before = _page_bytes(db)

    sweeper = _client(tmp_path, storage_path=str(db), responses=[])
    try:
        result = sweeper.collect_garbage(grace_seconds=0.0, vacuum=True)
        assert result.ok and result.vacuumed is True and result.bytes_freed > 0
    finally:
        sweeper.shutdown()
    after = _page_bytes(db)
    assert after < before


class _StoreWithoutSweep:
    def __init__(self) -> None:
        self._inner = InMemoryContentStore()

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        return self._inner.put(body, media_type=media_type)

    def get(self, ref: ContentRef) -> bytes:
        return self._inner.get(ref)

    def get_many(self, refs: Iterable[ContentRef]) -> dict[str, bytes]:
        return self._inner.get_many(refs)


def test_a_store_without_sweep_reports_unsupported() -> None:
    log = InMemoryEventLog()
    bare = collect_garbage(log, _StoreWithoutSweep())
    assert bare.ok is False and bare.reason == "unsupported"
    assert (bare.live, bare.swept, bare.bytes_freed, bare.vacuumed) == (0, 0, 0, False)
    # The read cache forwards the sweep and reports the same when it cannot.
    cached = collect_garbage(log, CachedContentStore(_StoreWithoutSweep()))
    assert cached.ok is False and cached.reason == "unsupported"


def test_cached_store_drops_its_cache_on_sweep() -> None:
    clock = [1_000.0]
    inner = InMemoryContentStore(clock=lambda: clock[0])
    store = CachedContentStore(inner)
    ref = store.put(b"cached body", media_type="text/plain")
    assert store.get(ref) == b"cached body"  # now resident in the cache
    clock[0] += 10.0
    outcome = store.sweep(set(), grace_seconds=1.0)
    assert (outcome.rows, outcome.bytes) == (1, len(b"cached body"))
    with pytest.raises(ContentNotFound):
        store.get(ref)


def test_collect_garbage_rejects_a_negative_grace() -> None:
    with pytest.raises(ValueError):
        collect_garbage(InMemoryEventLog(), InMemoryContentStore(), grace_seconds=-1)
