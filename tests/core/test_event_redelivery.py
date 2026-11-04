"""``on_event`` at least once for an extension that keeps a delivery cursor:
a Waiting hands the task back only after the extensions saw it (their
records land), each claim delivers the events after the cursor again, and a
wake that changes nothing writes no second Waiting."""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, Recorded, Waiting
from noeta.core.extension import Extension, TaskView
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import agent, events, runtime


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


class Seen(Extension):
    """Counts the seqs it is given; ``cursor`` (when not ``None``) is its
    ``delivered_through``; records each Waiting it sees when ``ack``."""

    def __init__(self, name: str, cursor: int | None = None, ack: bool = False) -> None:
        self.name, self.cursor, self.ack = name, cursor, ack
        self.seen: Counter[int] = Counter()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return data if kind == "acked" else state

    def delivered_through(self, view: TaskView) -> int | None:
        return self.cursor

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        self.seen[recorded.seq] += 1
        if self.ack and isinstance(recorded.event, Waiting):
            view.record(self.name, "acked", recorded.seq)


async def test_a_record_made_for_a_waiting_lands(store: SqliteStore) -> None:
    acker = Seen("acker", ack=True)
    rt = runtime(store, ScriptedProvider([reply("hi")]), agent([acker]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    evs = await events(client, tid)
    waiting = next(i for i, e in enumerate(evs, 1) if isinstance(e, Waiting))
    assert [e.data for e in evs if isinstance(e, ExtensionRecorded)
            and e.extension == "acker"] == [waiting]
    row = await client.get(tid)
    assert (row.status, row.waiting_on) == ("waiting", "input")


async def test_a_claim_delivers_the_events_after_the_cursor_again(store: SqliteStore) -> None:
    plain, behind = Seen("plain"), Seen("behind", cursor=0)
    rt = runtime(store, ScriptedProvider([reply("hi")]), agent([plain, behind]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    first = (await client.get(tid)).last_seq
    await client.signal(tid, "unrelated")
    await rt.run_until_idle()
    assert set(plain.seen.values()) == {1}  # no cursor: each event once
    assert all(behind.seen[s] == 2 for s in range(1, first + 1))


async def test_a_wake_that_changes_nothing_writes_no_second_waiting(store: SqliteStore) -> None:
    rt = runtime(store, ScriptedProvider([reply("hi")]), agent([]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await client.signal(tid, "unrelated")
    await rt.run_until_idle()
    assert len([e for e in await events(client, tid) if isinstance(e, Waiting)]) == 1
    row = await client.get(tid)
    assert (row.status, row.waiting_on) == ("waiting", "input")
