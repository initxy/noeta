"""Postgres test-backend support for the storage contract suites.

The storage-backend-neutral contract suites (event log / dispatcher /
content store) parametrise over every adapter. The Postgres adapters
need a live server, so the ``postgres`` param only runs when the
``NOETA_TEST_POSTGRES_DSN`` env var carries a DSN (e.g.
``postgresql://user:pass@host:5432/dbname``) — locally against a dev
server, or in CI against a service container — and is skipped otherwise.

Isolation on a shared server: each test gets a fresh schema
(``noeta_t_<uuid>``) and a DSN whose ``search_path`` points at it, so
the adapters' unqualified DDL/DML lands there; the schema is dropped at
teardown. Advisory locks are database-wide, so parallel tests
over-serialise slightly but never interfere.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Iterator

import pytest


POSTGRES_DSN_ENV = "NOETA_TEST_POSTGRES_DSN"


def postgres_param() -> object:
    """The ``postgres`` fixture param, skipped unless a DSN is configured."""
    return pytest.param(
        "postgres",
        marks=pytest.mark.skipif(
            not os.environ.get(POSTGRES_DSN_ENV),
            reason=f"{POSTGRES_DSN_ENV} not set",
        ),
    )


@contextmanager
def isolated_schema_dsn() -> Iterator[str]:
    """Yield a DSN scoped to a fresh schema; drop the schema on exit.

    The yielded DSN stays in ``postgresql://`` URL form (a query-string
    ``options`` parameter, percent-encoded) so the storage-URL dispatch
    under test (``is_postgres_url``) sees the same shape a user would
    configure.
    """
    import psycopg

    admin_dsn = os.environ[POSTGRES_DSN_ENV]
    schema = f"noeta_t_{uuid.uuid4().hex[:12]}"
    sep = "&" if "?" in admin_dsn else "?"
    schema_dsn = f"{admin_dsn}{sep}options=-csearch_path%3D{schema}"
    admin = psycopg.connect(admin_dsn, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            yield schema_dsn
        finally:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        admin.close()
