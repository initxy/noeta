"""A client gets the text of an error noeta raised for it, never that of
one a library or the system raised (it may name the server's paths)."""

from __future__ import annotations

import pathlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from noeta.host.stream_json import HeadlessOptions, StreamJson

SECRET = "/srv/secret/place"


def _library_error() -> None:
    pathlib.PurePosixPath(SECRET).relative_to("/elsewhere")


async def test_a_control_request_hides_a_library_errors_text() -> None:
    async def emit(_: dict[str, Any]) -> None:
        return None

    conv = StreamJson(SimpleNamespace(client=None), HeadlessOptions(), emit)  # type: ignore[arg-type]

    async def boom(_: dict[str, Any]) -> dict[str, Any]:
        _library_error()
        return {}

    conv._on_boom = boom  # type: ignore[attr-defined]
    await conv._control("r1", {"subtype": "boom"})
    await conv._control("r2", {"subtype": "nonsense"})
    errors = [conv._out.get_nowait()["response"]["error"] for _ in range(2)]  # type: ignore[index]
    assert SECRET not in errors[0] and "log" in errors[0]
    assert "nonsense" in errors[1]


async def test_an_http_request_hides_a_library_errors_text(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch
                                                           ) -> None:
    pytest.importorskip("starlette")
    httpx = pytest.importorskip("httpx")
    from noeta.backends.stores.sqlite import SqliteStore
    from noeta.core.client import Client
    from noeta.host.http import create_app

    async def tasks(*_: Any, **__: Any) -> Any:
        _library_error()

    monkeypatch.setattr(Client, "tasks", tasks)
    store = SqliteStore(str(tmp_path / "h.db"))
    api = create_app(Client(store), token="tok", cwd=str(tmp_path))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                     base_url="http://localhost") as http:
            auth = {"authorization": "Bearer tok"}
            got = await http.get("/tasks", headers=auth)
            bad = await http.get("/tasks?limit=x", headers=auth)
    finally:
        await store.close()
    assert got.status_code == 400 and SECRET not in got.text
    assert bad.status_code == 400 and "'x'" in bad.text
