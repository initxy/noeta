"""Images the person pastes are also saved as files in the exec env and named
to the model (Claude Code's ``[Image source: <path>]``), once per message:
private files, never through a symlink, written again when gone, removed
with their root task."""

from __future__ import annotations

import os
import shutil
import stat
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


class RemoteEnv(LocalExecEnv):
    """This machine, standing in for an env elsewhere (a container)."""

    local = False


class Harness:
    def __init__(self, tmp_path: Path, *, env: type[LocalExecEnv] = LocalExecEnv,
                 one_shot: bool = False) -> None:
        self.work = tmp_path / "w"
        self.work.mkdir()
        self.store = SqliteStore(str(tmp_path / "t.db"))
        self.provider = ScriptedProvider([reply("ok") for _ in range(4)])
        agent = Agent(name="a", model="m", extensions=[FileTools()], one_shot=one_shot)
        self.rt = Runtime(store=self.store, agents={"a": agent},
                          provider_for=lambda _m: self.provider,
                          env_for=lambda _s: env(str(self.work)), sleep=_no_sleep)
        self.client = Client(self.store)

    async def paste(self, *images: bytes) -> str:
        tid = await self.client.create("a", Message(role="user", origin="human", content=[
            *(Image(media_type="image/png", data=d) for d in images),
            Text(text="put this in assets/")]))
        await self.rt.run_until_idle()
        return tid

    async def again(self, tid: str) -> None:
        await self.client.send(tid, "and now?")
        await self.rt.run_until_idle()

    def notes(self, n: int) -> list[str]:
        return [m.text for m in self.provider.requests[n].messages
                if m.origin == "system" and ("[Image source:" in m.text
                                             or "no longer available" in m.text)]


def _paths(note: str) -> list[str]:
    return [line[len("[Image source: "):-1] for line in note.splitlines()]


async def test_a_pasted_image_is_saved_private_and_named_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    data = png(3, 3)
    tid = await h.paste(data)
    [note] = h.notes(0)
    [path] = _paths(note)
    assert path.startswith(os.environ["NOETA_HOME"] + "/image-cache/") and path.endswith(".png")
    assert Path(path).read_bytes() == data
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(Path(path).parent).st_mode) == 0o700
    await h.again(tid)
    assert h.notes(1) == [note]
    await h.store.close()


async def test_several_images_are_one_line_each(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.paste(png(2, 2), png(3, 3))
    [note] = h.notes(0)
    first, second = _paths(note)
    assert first.endswith("-1.png") and second.endswith("-2.png")
    assert Path(second).read_bytes() == png(3, 3)
    await h.store.close()


async def test_an_env_elsewhere_saves_under_a_per_user_temp_directory(tmp_path: Path) -> None:
    h = Harness(tmp_path, env=RemoteEnv)
    await h.paste(png(2, 2))
    [path] = _paths(h.notes(0)[0])
    assert f"/noeta-images-{os.getuid()}/" in path and Path(path).is_file()
    assert not path.startswith(os.environ["NOETA_HOME"])
    shutil.rmtree(Path(path).parent)
    await h.store.close()


async def test_a_message_without_images_gets_no_note(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.client.create("a", "hello there")
    await h.rt.run_until_idle()
    assert h.notes(0) == []
    await h.store.close()


async def test_an_image_that_cannot_be_written_is_not_claimed(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (Path(os.environ["NOETA_HOME"]) / "image-cache").write_text("not a directory")
    h = Harness(tmp_path)
    await h.paste(png(2, 2))
    assert h.notes(0) == []
    assert any("pasted image not saved" in r.message for r in caplog.records)
    await h.store.close()


async def test_a_symlinked_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, Path(os.environ["NOETA_HOME"]) / "image-cache")
    h = Harness(tmp_path)
    await h.paste(png(2, 2))
    assert h.notes(0) == [] and list(elsewhere.iterdir()) == []
    await h.store.close()


async def test_a_file_gone_by_a_later_turn_is_written_again(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    tid = await h.paste(png(2, 2))
    [path] = _paths(h.notes(0)[0])
    os.remove(path)
    await h.again(tid)
    assert Path(path).read_bytes() == png(2, 2)
    assert h.notes(1) == h.notes(0)  # the same place: nothing new to say
    await h.store.close()


async def test_a_file_that_cannot_come_back_is_said_to_be_gone(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    tid = await h.paste(png(2, 2))
    [path] = _paths(h.notes(0)[0])
    cache = Path(os.environ["NOETA_HOME"]) / "image-cache"
    os.remove(path)
    os.rmdir(Path(path).parent)
    os.rmdir(cache)
    cache.write_text("in the way")
    await h.again(tid)
    assert any("no longer available" in n and path in n for n in h.notes(1))
    await h.store.close()


async def test_an_ended_root_task_takes_its_images_with_it(tmp_path: Path) -> None:
    h = Harness(tmp_path, one_shot=True)
    await h.paste(png(2, 2))
    [path] = _paths(h.notes(0)[0])
    assert not Path(path).exists() and not Path(path).parent.exists()
    await h.store.close()
