"""An image the person sends is made to fit once, in the client every front
end goes through: an oversized one sent over HTTP or stream-json reaches the
history (and so every later request) within Claude Code's limits."""

from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.images import MAX_BASE64_BYTES, MAX_HEIGHT, MAX_WIDTH, base64_size
from noeta.core.types import Image, Message, Text
from noeta.host.local import LocalApp
from noeta.host.stream_json import HeadlessOptions, StreamJson, drive
from noeta.testing import ScriptedProvider, reply
from tests.images import png

PIL = pytest.importorskip("PIL.Image")


def noisy_png(width: int, height: int) -> bytes:
    """A PNG that does not compress: over the 5 MB base64 limit at this size."""
    buf = io.BytesIO()
    PIL.frombytes("RGB", (width, height), os.urandom(width * height * 3)).save(buf, "PNG")
    return buf.getvalue()


def prompt_with(image: bytes) -> Message:
    return Message(role="user", content=[Text(text="what is this?"),
                                         Image(media_type="image/png", data=image)],
                   origin="human")


def sent_images(provider: ScriptedProvider) -> list[Image]:
    return [b for m in provider.requests[-1].messages for b in m.content
            if isinstance(b, Image)]


def assert_fits(image: Image) -> None:
    assert base64_size(len(image.data)) <= MAX_BASE64_BYTES
    with PIL.open(io.BytesIO(image.data)) as shown:
        assert shown.width <= MAX_WIDTH and shown.height <= MAX_HEIGHT


def local(tmp_path: Path, provider: ScriptedProvider) -> LocalApp:
    return LocalApp(SqliteStore(str(tmp_path / "t.db")), [Agent(name="main", model="m")],
                    provider, env_for=lambda _s: None, own_tasks_only=True)


async def test_an_oversized_image_over_http_is_fitted(tmp_path: Path) -> None:
    pytest.importorskip("starlette")
    uvicorn = pytest.importorskip("uvicorn")
    from noeta.host.http import create_app
    from noeta.host.remote import RemoteClient

    big = noisy_png(2600, 1100)
    assert base64_size(len(big)) > MAX_BASE64_BYTES
    provider = ScriptedProvider([reply("a picture"), reply("again")])
    app = local(tmp_path, provider)
    await app.start()
    server = uvicorn.Server(uvicorn.Config(create_app(app.client, token="t"),
                                           host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    client = RemoteClient(f"http://127.0.0.1:{port}", token="t")
    try:
        tid = await client.create("main", prompt_with(big))
        await client.wait(tid, timeout=20)
        await client.send(tid, "and now?")
        for _ in range(400):
            if len(provider.requests) == 2:
                break
            await asyncio.sleep(0.02)
        [image] = sent_images(provider)  # the second request still carries it, fitted
        assert_fits(image)
    finally:
        await client.close()
        server.should_exit = True
        await serving
        await app.stop()


async def test_an_oversized_image_over_stream_json_is_fitted(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("a picture")])
    app = local(tmp_path, provider)
    out: list[dict[str, Any]] = []

    async def emit(message: dict[str, Any]) -> None:
        out.append(message)

    async with app:
        conv = StreamJson(app, HeadlessOptions(model="m", cwd=str(tmp_path)), emit)
        result = await asyncio.wait_for(drive(conv, prompt=prompt_with(noisy_png(4100, 900))), 20)
    assert result is not None and result["result"] == "a picture"
    [image] = sent_images(provider)
    assert_fits(image)


async def test_an_image_that_cannot_fit_becomes_a_note(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)  # no image library: nothing is shrunk
    store = SqliteStore(str(tmp_path / "t.db"))
    client = Client(store)
    small = png(10, 10)
    tid = await client.create("main", prompt_with(png(9000, 10)))
    await client.send(tid, prompt_with(small))
    [first, second] = [item.body.message for item in await store.inbox(tid)]
    assert [type(b) for b in first.content] == [Text, Text]
    assert "left out" in first.content[1].text and "9000x10 px" in first.content[1].text
    assert second.content[1] == Image(media_type="image/png", data=small)
