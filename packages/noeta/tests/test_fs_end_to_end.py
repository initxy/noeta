"""The runner with the real file tools: Read then Edit across two replies,
and Read + Edit in one reply (the read lands before the edit runs)."""

from __future__ import annotations

from pathlib import Path

from noeta.agent import Agent
from noeta.client import Client
from noeta.events import ToolFinished
from noeta.execenv.local import LocalExecEnv
from noeta.ext.fs import FileTools
from noeta.runner import Runtime
from noeta.store.sqlite import SqliteStore
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


async def test_read_then_edit_in_one_reply(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    target = work / "a.txt"
    target.write_text("hello world\n")
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(target)}),
              ("Edit", {"file_path": str(target), "old_string": "world",
                        "new_string": "noeta"})),
        reply("done"),
    ])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", extensions=[FileTools()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: LocalExecEnv(str(work)),
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "fix it")
    await rt.run_until_idle()
    results = [r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    assert [r.is_error for r in results] == [False, False], [r.text for r in results]
    assert target.read_text() == "hello noeta\n"
    await store.close()
