"""The cluster test's Postgres: ``NOETA_TEST_POSTGRES_DSN`` when set, else a
throwaway ``postgres:16`` container on a random loopback port, removed at the
end of the session. Each test gets a database of its own (the store's
schema name is fixed, so a database is the unit of isolation).

The tests need the ``noeta-cluster-scripted`` test package installed
(``make cluster`` runs them with it); without it they skip, and with
``NOETA_CLUSTER_REQUIRED`` set (``make cluster``) a skip is a failure."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from importlib.metadata import entry_points
from typing import NoReturn
from urllib.parse import urlsplit, urlunsplit

import pytest

IMAGE = "postgres:16"


def _scripted_installed() -> bool:
    return any(ep.name == "cluster_scripted" for ep in entry_points(group="noeta.providers"))


def _wait_ready(dsn: str, timeout: float = 60.0) -> None:
    import psycopg

    deadline = time.monotonic() + timeout
    while True:
        try:
            with psycopg.connect(dsn, connect_timeout=2):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)


def _skip(reason: str) -> NoReturn:
    """Skip, or fail when the run requires the cluster test to run."""
    if os.environ.get("NOETA_CLUSTER_REQUIRED"):
        pytest.fail(f"cluster test required but cannot run: {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def pg_server() -> Iterator[str]:
    """A Postgres DSN (its maintenance database) for the session."""
    if not _scripted_installed():
        _skip("the cluster test needs its scripted provider: run `make cluster`")
    dsn = os.environ.get("NOETA_TEST_POSTGRES_DSN", "")
    if dsn:
        yield dsn
        return
    if shutil.which("docker") is None:
        _skip("no docker and no NOETA_TEST_POSTGRES_DSN")
    probe = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    if probe.returncode != 0:
        _skip(f"docker cannot use {IMAGE} (or the daemon is not reachable)")
    name = f"noeta-cluster-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=pw",
                    "-p", "127.0.0.1::5432", IMAGE, "-c", "max_connections=400"],
                   check=True, capture_output=True)
    try:
        port = subprocess.run(["docker", "port", name, "5432/tcp"], check=True,
                              capture_output=True, text=True).stdout.split(":")[-1].strip()
        dsn = f"postgresql://postgres:pw@127.0.0.1:{port}/postgres"
        _wait_ready(dsn)
        # The entrypoint restarts the server once after initdb; wait for that too.
        time.sleep(1.0)
        _wait_ready(dsn)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def pg_url(pg_server: str) -> Iterator[str]:
    """A fresh database on the session's server, dropped afterwards."""
    import psycopg

    db = f"cluster_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db}"')
    parts = urlsplit(pg_server)
    yield urlunsplit(parts._replace(path=f"/{db}"))
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
