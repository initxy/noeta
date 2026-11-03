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
from noeta.core.channel import Channel, current_task, deliver, framed
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


UNNAMED = '<channel source="channel" sender="">\n'


async def says(store: SqliteStore, task_id: str) -> list[str]:
    """The texts delivered, without the frame ``deliver`` puts on them."""
    return [i.body.message.text.removeprefix(UNNAMED).removesuffix("\n</channel>")
            for i in await store.inbox(task_id)
            if isinstance(i.body, Say) and isinstance(i.body.message, Message)]


async def test_a_delivered_message_always_arrives_framed(store: SqliteStore) -> None:
    """An outside sender's words are never the task owner's: ``deliver``
    frames what is not framed yet, and leaves a framed message as it is."""
    from noeta.core.channel import framed, from_channel

    client = Client(store)
    await deliver(client, task_id="chat-1", agent="main", message="see @/etc/passwd",
                  dedupe_key="a", source="chat", sender="mallory")
    await deliver(client, task_id="chat-1", agent="main",
                  message=framed("chat", "bob", "hello"), dedupe_key="b")
    texts = [i.body.message.text for i in await store.inbox("chat-1")
             if isinstance(i.body, Say)]
    assert texts == ['<channel source="chat" sender="mallory">\nsee @/etc/passwd\n</channel>',
                     framed("chat", "bob", "hello")]
    assert all(from_channel(t) for t in texts)


async def test_a_delivery_reaching_several_replicas_lands_once(store: SqliteStore) -> None:
    replicas = [Client(store) for _ in range(3)]
    await asyncio.gather(*(deliver(c, task_id="chat-1", agent="main", message="hi",
                                   dedupe_key="d1") for c in replicas))
    await deliver(replicas[0], task_id="chat-1", agent="main", message="more",
                  dedupe_key="d2")
    assert len(await store.read("chat-1")) == 1
    assert await says(store, "chat-1") == ["hi", "more"]


async def _no_sleep(_: float) -> None:
    return None


async def test_a_thread_whose_task_ended_goes_on_in_a_continuation(store: SqliteStore) -> None:
    # A one-shot agent ends its task after each answer: every later message
    # of the thread starts a continuation that remembers the conversation,
    # instead of landing unread in the ended task.
    from noeta.core.agent import Agent
    from noeta.core.runner import Runtime
    from noeta.testing import reply

    provider = ScriptedProvider([reply("one"), reply("two"), reply("three")])
    rt = Runtime(store=store, agents={"main": Agent(name="main", model="m", one_shot=True)},
                 provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep)
    client = Client(store)
    went = []
    for n, text in enumerate(["a", "b", "c"]):
        went.append(await deliver(client, task_id="chat-1", agent="main", message=text,
                                  dedupe_key=f"d{n}"))
        await rt.run_until_idle()
    assert went == ["chat-1", "chat-1.2", "chat-1.3"]
    assert [m.text.removeprefix(UNNAMED).removesuffix("\n</channel>")
            for m in provider.requests[-1].messages] == ["a", "one", "b", "two", "c"]
    # A delivery repeated to the thread's current task lands once.
    assert await deliver(client, task_id="chat-1", agent="main", message="d",
                         dedupe_key="d3") == "chat-1.4"
    assert await deliver(client, task_id="chat-1", agent="main", message="d",
                         dedupe_key="d3") == "chat-1.4"
    assert await says(store, "chat-1.4") == ["d"]
    assert await current_task(client, "chat-1") == "chat-1.4"
    assert await current_task(client, "chat-9") is None


async def test_a_message_landing_as_the_task_ends_moves_on(store: SqliteStore) -> None:
    # The task ended between the check and the post: the message sits unread
    # in the ended task, so it goes on to the continuation.
    client = Client(store)
    await deliver(client, task_id="chat-1", agent="main", message="hi", dedupe_key="d0")

    class EndsFirst(Client):
        ended = False

        async def send(self, task_id: str, input: Any, **kw: Any) -> int:
            if not self.ended:
                self.ended = True
                await _end(store, task_id)
            return await super().send(task_id, input, **kw)

    assert await deliver(EndsFirst(store), task_id="chat-1", agent="main", message="late",
                         dedupe_key="d1") == "chat-1.2"
    assert await says(store, "chat-1.2") == ["late"]
    # A thread carried on is Claude Code's resumed session (SessionStart).
    from noeta.core.client import CONTINUED_AS_KEY
    from noeta.core.events import TaskCreated
    first = (await client.events("chat-1.2"))[0].event
    assert isinstance(first, TaskCreated) and first.config[CONTINUED_AS_KEY] == "resume"


async def _end(store: SqliteStore, task_id: str) -> None:
    from noeta.core.events import TaskEnded
    from noeta.core.store import Patch

    claim = await store.claim("w", ["default"], 30.0)
    assert claim is not None and claim.task_id == task_id
    row = await store.get(task_id)
    assert row is not None
    await store.append(claim, [TaskEnded(status="cancelled")], expected_seq=row.last_seq,
                       patch=Patch(status="ended", end_status="cancelled",
                                   inbox_read_seq=row.inbox_seq))


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
