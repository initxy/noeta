"""Root summaries read only what a task appended since they were cached,
and the cache keeps a bounded number of roots."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded
from noeta.core.store import Claim
from noeta.host import summaries
from noeta.host.summaries import root_summaries


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
