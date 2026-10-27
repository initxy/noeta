"""``noeta.connect``: one entry point for a store URL or a server URL."""

from __future__ import annotations

from pathlib import Path

import pytest

import noeta
from noeta.client import Client
from noeta.remote import RemoteClient


async def test_connect_sqlite_url(tmp_path: Path) -> None:
    client = noeta.connect(f"sqlite:///{tmp_path / 'tasks.db'}")
    assert isinstance(client, Client)
    tid = await client.create("main", "hi")
    assert (await client.get(tid)).status == "ready"
    await client.store.close()


async def test_connect_http_url() -> None:
    client = noeta.connect("http://127.0.0.1:1", token="t")
    assert isinstance(client, RemoteClient)
    assert client.http.headers["authorization"] == "Bearer t"
    await client.close()


def test_connect_unknown_scheme() -> None:
    with pytest.raises(ValueError):
        noeta.connect("ftp://x")
