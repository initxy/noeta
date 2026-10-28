"""Shared fixtures. Postgres tests run when ``NOETA_TEST_POSTGRES_DSN`` is
set (e.g. ``postgresql://postgres:pw@localhost:55432/postgres``) and skip
otherwise; each test gets its own schema, dropped afterwards."""

from __future__ import annotations

import os
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
