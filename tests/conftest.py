"""Shared fixtures. Postgres tests run when ``NOETA_TEST_POSTGRES_DSN`` is
set (e.g. ``postgresql://postgres:pw@localhost:55432/postgres``) and skip
otherwise; each test gets its own schema, dropped afterwards."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

PG_DSN = os.environ.get("NOETA_TEST_POSTGRES_DSN", "")


class Pg:
    def __init__(self, dsn: str, schema: str) -> None:
        self.dsn = dsn
        self.schema = schema

    def store(self, **kw: Any) -> Any:
        from noeta.backends.stores.postgres import PostgresStore

        return PostgresStore(self.dsn, schema=self.schema, **kw)


@pytest.fixture
def pg() -> Iterator[Pg]:
    if not PG_DSN:
        pytest.skip("set NOETA_TEST_POSTGRES_DSN to run the postgres tests")
    import psycopg

    schema = f"t_{uuid.uuid4().hex[:12]}"
    yield Pg(PG_DSN, schema)
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


#: The real stderr, kept before output capture takes fd 2 over.
_STDERR_FD: list[int] = []


def pytest_configure(config: pytest.Config) -> None:
    _STDERR_FD.append(os.dup(2))


def _dump_pending_tasks(nodeid: str) -> None:
    """Every unfinished asyncio task with its stack: faulthandler's thread
    stacks show a stuck loop, not the coroutine it waits on."""
    import asyncio.tasks

    lines = [f"\n--- asyncio tasks still pending in {nodeid} ---\n"]
    for _ in range(3):  # read from another thread: the set may change under us
        try:
            pending = [t for t in list(asyncio.tasks._scheduled_tasks) if not t.done()]
            break
        except RuntimeError:
            pending = []
    for task in pending:
        lines.append(f"{task!r}\n")
        awaited: Any = task.get_coro()
        while awaited is not None:  # down the await chain, outermost first
            frame = getattr(awaited, "cr_frame", None) or getattr(awaited, "gi_frame", None)
            if frame is None:
                lines.append(f"  awaiting {awaited!r}\n")
                break
            lines.append(f"  {frame.f_code.co_filename}:{frame.f_lineno} in "
                         f"{frame.f_code.co_name}\n")
            awaited = getattr(awaited, "cr_await", None) or getattr(awaited, "gi_yieldfrom", None)
    os.write(_STDERR_FD[0], "".join(lines).encode())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Any:
    """With ``faulthandler_timeout`` set, a test still running then also
    has its pending asyncio tasks printed."""
    after = float(item.config.getini("faulthandler_timeout") or 0)
    if after <= 0:
        return (yield)
    timer = threading.Timer(after, _dump_pending_tasks, args=(item.nodeid,))
    timer.daemon = True
    timer.start()
    try:
        return (yield)
    finally:
        timer.cancel()


@pytest.fixture(autouse=True)
def noeta_home_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
                      ) -> None:
    """noeta's home is a fresh directory for each test, so what a task keeps
    there (plans, pasted images, transcripts) never lands in the user's."""
    monkeypatch.setenv("NOETA_HOME", str(tmp_path_factory.mktemp("noeta-home")))


@pytest.fixture(autouse=True)
def no_model_listings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads a provider's model listing over the network (a front
    end built from settings would, for a model the catalog does not know);
    a test of listings passes its own transport or listing."""
    from noeta.backends.providers import listing

    monkeypatch.setattr(listing, "fetch_listing", lambda *a, **kw: [])


@pytest.fixture(autouse=True)
def fold_snapshots_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every task log any test loads is folded both ways and compared: from
    the start, and from a snapshot taken at several seqs plus the events
    after it (``tests/snapshot_check.py``). Opt out with
    ``NOETA_TEST_NO_SNAPSHOT_CHECK=1``."""
    if os.environ.get("NOETA_TEST_NO_SNAPSHOT_CHECK"):
        return
    from noeta.core.runner.task import TaskRun
    from tests.snapshot_check import check_both_ways

    original = TaskRun.load

    async def load(self: TaskRun) -> None:
        await original(self)
        await check_both_ways(self)

    monkeypatch.setattr(TaskRun, "load", load)
