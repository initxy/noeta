"""Images the person pastes are also saved as files in the exec env, and the
model is told where (Claude Code's pasted-image paths), once per message."""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.types import Image, Message, Text
from noeta.plugins.fs import FileTools
from noeta.testing import ScriptedProvider, reply
from tests.images import png


async def _no_sleep(_: float) -> None:
    return None


async def test_a_pasted_image_is_saved_and_its_path_told_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    work = tmp_path / "w"
    work.mkdir()
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("ok"), reply("fine")])
    agent = Agent(name="a", model="m", extensions=[FileTools()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)
    client = Client(store)
    data = png(3, 3)
    tid = await client.create("a", Message(role="user", origin="human", content=[
        Image(media_type="image/png", data=data), Text(text="put this in assets/")]))
    await rt.run_until_idle()
    notes = [m.text for m in provider.requests[0].messages
             if m.origin == "system" and "also saved as a file" in m.text]
    assert len(notes) == 1
    path = notes[0].split('"')[1]
    assert path.startswith(str(tmp_path / "home" / "image-cache")) and path.endswith(".png")
    assert Path(path).read_bytes() == data
    await client.send(tid, "and now?")
    await rt.run_until_idle()
    again = [m.text for m in provider.requests[1].messages if "also saved as a file" in m.text]
    assert again == notes
    await store.close()


async def test_a_message_without_images_gets_no_note(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("ok")])
    agent = Agent(name="a", model="m", extensions=[FileTools()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(tmp_path)), sleep=_no_sleep)
    await Client(store).create("a", "hello there")
    await rt.run_until_idle()
    assert not any("saved as a file" in m.text for m in provider.requests[0].messages)
    await store.close()
