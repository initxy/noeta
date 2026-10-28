"""A file the model read and someone else then changed: the model is told
once, at the start of its next turn; its own edits and untouched files say
nothing."""

from __future__ import annotations

import os
from pathlib import Path

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.types import ModelRequest
from noeta.plugins.fs import FileTools
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


def _notes(request: ModelRequest) -> list[str]:
    return [m.text for m in request.messages
            if m.origin == "system" and m.text.startswith("Note: ")]


def _touch(path: Path, text: str) -> None:
    path.write_text(text)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))  # a clearly newer mtime


async def test_told_once_per_change(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    a, b = work / "a.txt", work / "b.txt"
    a.write_text("one\n")
    b.write_text("bee\n")
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(a)}), ("Read", {"file_path": str(b)})),
        reply("read"),
        reply("", ("Edit", {"file_path": str(b), "old_string": "bee", "new_string": "be"})),
        reply("edited"),
        reply("ok"),
        reply("ok"),
        reply("ok"),
    ])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", extensions=[FileTools()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: LocalExecEnv(str(work)),
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "read both")
    await rt.run_until_idle()

    _touch(a, "two\n")
    await client.send(tid, "edit b")
    await rt.run_until_idle()
    turn2 = provider.requests[2]
    assert _notes(turn2) == [f"Note: {a} was modified after you last read it, either by the "
                             "user or by a linter. Don't tell the user this, since they are "
                             "already aware. This change was intentional, so make sure to take "
                             "it into account as you proceed (ie. don't revert it unless the "
                             "user asks you to). Read the file again before you edit it."]
    assert len(_notes(provider.requests[3])) == 1  # checked only at the start of a turn

    await client.send(tid, "again")  # nothing new changed; b was the model's own edit
    await rt.run_until_idle()
    assert len(_notes(provider.requests[4])) == 1  # the earlier note, not a new one

    _touch(a, "three\n")
    a.unlink()
    await client.send(tid, "and again")  # a deleted file says nothing
    await rt.run_until_idle()
    assert len(_notes(provider.requests[5])) == 1

    a.write_text("four\n")
    _touch(a, "four\n")
    await client.send(tid, "last")
    await rt.run_until_idle()
    assert len(_notes(provider.requests[6])) == 2
    await store.close()
