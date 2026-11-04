"""Root summaries read only what a task appended since they were cached,
the cache keeps a bounded number of roots, and an ``archive`` signal in a
task's inbox lists it apart."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, External, InboxItem, TaskEnded
from noeta.core.store import Claim, Patch
from noeta.host import summaries
from noeta.host.app import archive_task
from noeta.host.summaries import archived_in, root_summaries


class _Counting(Client):
    """Counts the events each read returns."""

    def __init__(self, store: SqliteStore) -> None:
        super().__init__(store)
        self.read: list[tuple[str, int]] = []

    async def events(self, task_id: str, after: int = 0) -> Any:
        found = await super().events(task_id, after)
        self.read.append((task_id, len(found)))
        return found


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _title(store: SqliteStore, task_id: str, title: str) -> None:
    claim = await store.claim("w", ["default"], 30)
    assert isinstance(claim, Claim) and claim.task_id == task_id
    row = await store.get(task_id)
    assert row is not None
    await store.append(claim, [ExtensionRecorded(extension="title", kind="title",
                                                 data={"title": title})],
                       expected_seq=row.last_seq)
    await store.release(claim)


async def test_a_moved_root_is_read_from_where_its_summary_was_made(
        store: SqliteStore) -> None:
    client = _Counting(store)
    tid = await client.create("main", "hello there")
    cache: summaries.Cache = {}
    [first] = await root_summaries(client, cache=cache)
    assert first["title"] == ""
    await _title(store, tid, "Greeting")
    client.read.clear()
    [moved] = await root_summaries(client, cache=cache)
    assert moved["title"] == "Greeting"
    assert client.read == [(tid, 1)]  # only the new record


async def test_the_cache_keeps_a_bounded_number_of_roots(
        store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summaries, "CACHE_ROOTS", 2)
    client = Client(store)
    ids = [await client.create("main", f"task {i}") for i in range(3)]
    cache: summaries.Cache = {}
    await root_summaries(client, cache=cache)
    assert len(cache) == 2 and set(cache) < set(ids)


class _CountingInbox:
    """``store.inbox`` counting the items each read returns."""

    def __init__(self, store: SqliteStore) -> None:
        self.store = store
        self.read: list[tuple[str, int]] = []

    async def __call__(self, task_id: str, after: int = 0) -> Any:
        found = await self.store.inbox(task_id, after)
        self.read.append((task_id, len(found)))
        return found


async def _end(store: SqliteStore, task_id: str) -> None:
    """End the only claimable task the way a runner does."""
    claim = await store.claim("w", ["default"], 30)
    assert isinstance(claim, Claim) and claim.task_id == task_id
    row = await store.get(task_id)
    assert row is not None
    await store.append(claim, [TaskEnded(status="cancelled")], expected_seq=row.last_seq,
                       patch=Patch(status="ended", end_status="cancelled",
                                   inbox_read_seq=row.inbox_seq))


async def test_an_archive_signal_lists_a_root_apart_even_once_it_has_ended(
        store: SqliteStore) -> None:
    client = Client(store)
    gone = await client.create("main", "archive this one")
    await _end(store, gone)
    kept = await client.create("main", "keep this one")
    await archive_task(client, gone)
    inbox = _CountingInbox(store)
    cache: summaries.Cache = {}

    async def listed(archived: bool | None) -> list[str]:
        return [s["task_id"] for s in await root_summaries(client, cache=cache, inbox=inbox,
                                                          archived=archived)]

    assert await listed(False) == [kept]
    assert await listed(True) == [gone]
    assert set(await listed(None)) == {kept, gone}
    inbox.read.clear()
    await archive_task(client, gone, archived=False)
    assert set(await listed(False)) == {kept, gone}
    assert inbox.read == [(gone, 1)]  # only the new signal
    # Without an inbox reader nothing reads as archived.
    assert not any(s["archived"] for s in await root_summaries(client))


async def test_archived_roots_do_not_count_toward_the_limit(store: SqliteStore) -> None:
    client = Client(store)
    older = await client.create("main", "older")
    newer = await client.create("main", "newer")
    await archive_task(client, newer)
    got = await root_summaries(client, limit=1, inbox=store.inbox, archived=False)
    assert [s["task_id"] for s in got] == [older]


def test_the_last_archive_signal_wins() -> None:
    def item(seq: int, data: Any) -> InboxItem:
        return InboxItem(seq=seq, at=0.0, body=External(name="archive", data=data))

    assert archived_in([item(1, {"archived": True}), item(2, {"archived": False})]) is False
    assert archived_in([item(1, None)]) is True  # no data: archive
    assert archived_in([], archived=True) is True
    assert archived_in([InboxItem(seq=1, at=0.0, body=External(name="rename"))]) is False
