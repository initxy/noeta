"""Channels: listeners a host runs, whose inbound posts are idempotent so
several replicas may run the same one."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.cli import home_dir
from noeta.cli.config import from_settings, load_settings, make_channels
from noeta.core import registry
from noeta.core.channel import Channel, deliver, framed
from noeta.core.client import Client, ClientAPI
from noeta.core.events import Say
from noeta.core.types import Message
from noeta.host.channels import run_channels
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider


@pytest.fixture
async def store(tmp_path: Path) -> Any:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def says(store: SqliteStore, task_id: str) -> list[str]:
    return [i.body.message.text for i in await store.inbox(task_id)
            if isinstance(i.body, Say) and isinstance(i.body.message, Message)]


async def test_a_delivery_reaching_several_replicas_lands_once(store: SqliteStore) -> None:
    replicas = [Client(store) for _ in range(3)]
    await asyncio.gather(*(deliver(c, task_id="chat-1", agent="main", message="hi",
                                   dedupe_key="d1") for c in replicas))
    await deliver(replicas[0], task_id="chat-1", agent="main", message="more",
                  dedupe_key="d2")
    assert len(await store.read("chat-1")) == 1
    assert await says(store, "chat-1") == ["hi", "more"]


def test_framed_text_says_who_speaks() -> None:
    assert framed("chat", 'a"b', "hello") == \
        '<channel source="chat" sender="a&quot;b">\nhello\n</channel>'


class Flaky(Channel):
    name = "flaky"

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, client: ClientAPI, stop: asyncio.Event) -> None:
        self.runs += 1
        if self.runs == 1:
            raise RuntimeError("connection dropped")
        await stop.wait()


async def test_a_failing_channel_is_started_again(store: SqliteStore) -> None:
    flaky, stop = Flaky(), asyncio.Event()
    running = asyncio.create_task(run_channels([flaky], Client(store), stop, restart_after=0))
    for _ in range(100):
        await asyncio.sleep(0)
        if flaky.runs == 2:
            break
    stop.set()
    await asyncio.wait_for(running, 1)
    assert flaky.runs == 2


class Greeter(Channel):
    name = "greeter"

    def __init__(self, text: str = "hello") -> None:
        self.text = text

    async def run(self, client: ClientAPI, stop: asyncio.Event) -> None:
        await deliver(client, task_id="greeted", agent="main", message=self.text,
                      dedupe_key="once")
        await stop.wait()


async def test_a_local_app_runs_its_channels_while_started(tmp_path: Path) -> None:
    from noeta.core.agent import Agent

    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [Agent(name="main", model="m")],
                   ScriptedProvider([]), channels=[Greeter()])
    await app.start(worker=False)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if await app.store.get("greeted") is not None:
            break
    await app.stop()
    reopened = SqliteStore(str(tmp_path / "t.db"))
    assert await says(reopened, "greeted") == ["hello"]
    await reopened.close()


@pytest.fixture
def clean_registry() -> Iterator[None]:
    saved = dict(registry._channel_files), dict(registry._files), set(registry._loaded_paths)
    yield
    registry._channel_files.clear()
    registry._channel_files.update(saved[0])
    registry._files.clear()
    registry._files.update(saved[1])
    registry._loaded_paths.clear()
    registry._loaded_paths.update(saved[2])


CHANNEL_FILE = '''
from noeta.core.channel import Channel


class Pager(Channel):
    name = "pager"

    def __init__(self, token: str, room: str = "ops") -> None:
        self.token, self.room = token, room

    async def run(self, client, stop):
        await stop.wait()
'''


async def test_config_toml_enables_a_channel_from_a_plugin_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_registry: None) -> None:
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "pager.py").write_text(CHANNEL_FILE)
    monkeypatch.setattr(home_dir, "HOME", home)
    monkeypatch.setenv("PAGER_TOKEN", "t0k")
    (home / "config.toml").write_text(
        f'model = "sonnet"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n'
        '[channels.pager]\ntoken = "{env:PAGER_TOKEN}"\n')
    app = from_settings(load_settings(), cwd=str(tmp_path), with_channels=True)
    [pager] = app.channels
    assert (pager.name, pager.token, pager.room) == ("pager", "t0k", "ops")  # type: ignore[attr-defined]
    assert registry.sources(registry.CHANNELS)["pager"] == str(home / "plugins" / "pager.py")
    front = from_settings(load_settings(), cwd=str(tmp_path))
    assert front.channels == []  # a front end runs none
    await front.store.close()
    assert make_channels({"pager": {"enabled": False, "token": "x"}}) == []
    await app.store.close()
