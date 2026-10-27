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
        from noeta.backends.postgres import PostgresStore

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
