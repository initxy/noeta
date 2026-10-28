"""``--continue`` finds the newest conversation started here, however many
other tasks the store has updated since."""

from __future__ import annotations

from pathlib import Path

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.host.stream_json import find_latest_task


async def test_continue_finds_a_task_behind_many_newer_ones(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    try:
        client = Client(store)
        assert await find_latest_task(client, "/here") is None
        older = await client.create("main", None, config={"cwd": "/here"})
        mine = await client.create("main", None, config={"cwd": "/here"})
        await client.create("main", None, config={"cwd": "/elsewhere"})
        for _ in range(250):  # other agents' tasks, all updated later
            await client.create("other", None, config={"cwd": "/here"})
        assert await find_latest_task(client, "/here") == mine
        assert await find_latest_task(client, "/nowhere") is None
        assert older != mine
    finally:
        await store.close()
