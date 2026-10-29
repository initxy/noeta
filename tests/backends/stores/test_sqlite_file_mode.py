"""The sqlite store holds every conversation: a database noeta creates is
owner-only, with its ``-wal`` and ``-shm`` files; an existing one keeps its
mode."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.events import TaskCreated
from noeta.core.store import NewTask


@pytest.fixture(autouse=True)
def usual_umask() -> Iterator[None]:
    old = os.umask(0o022)
    yield
    os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_a_new_database_and_its_side_files_are_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = SqliteStore(str(path))
    await store.create(NewTask(task_id="t", created=TaskCreated(agent="a")))
    for name in ("t.db", "t.db-wal", "t.db-shm"):
        assert _mode(tmp_path / name) == 0o600, name
    await store.close()


async def test_an_existing_database_keeps_its_mode(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    path.touch(mode=0o640)
    path.chmod(0o640)
    store = SqliteStore(str(path))
    await store.create(NewTask(task_id="t", created=TaskCreated(agent="a")))
    assert _mode(path) == 0o640
    await store.close()
