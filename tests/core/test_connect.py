"""``noeta.connect``: one entry point for a store URL or a server URL."""

from __future__ import annotations

from pathlib import Path

import pytest

import noeta
from noeta.core.client import Client
from noeta.host.remote import RemoteClient


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


def test_top_level_names_load_lazily() -> None:
    import subprocess
    import sys

    import noeta

    assert set(noeta.__all__) == {"connect", *noeta._EXPORTS}
    for name in noeta._EXPORTS:
        assert getattr(noeta, name) is not None
    code = "import sys, noeta; print('noeta.core.runner' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
