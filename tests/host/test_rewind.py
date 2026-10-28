"""Rewind with file checkpoints: Edit and Write save a file's content
before they change it (once per prompt, blobs by hash), and a rewind forks
the conversation before a prompt and puts the files back."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded
from noeta.core.runner import Runtime
from noeta.host.rewind import diff_stats, file_plan, rewind, rewind_points
from noeta.plugins.checkpoints import Checkpoints
from noeta.plugins.fs import FileTools
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def _runtime(store: SqliteStore, provider: ScriptedProvider, work: Path) -> Runtime:
    agent = Agent(name="a", model="m", extensions=[FileTools(), Checkpoints()])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,  # type: ignore[arg-type,return-value]
                   env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)


def _snapshots(events: list[Any]) -> list[dict[str, Any]]:
    return [r.event.data for r in events if isinstance(r.event, ExtensionRecorded)
            and r.event.extension == "checkpoints" and r.event.kind == "snapshot"]


async def test_files_and_conversation_go_back_to_before_a_prompt(
        store: SqliteStore, tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    a, b = work / "a.txt", work / "b.txt"
    a.write_text("one\n")
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(a)})),
        reply("", ("Edit", {"file_path": str(a), "old_string": "one", "new_string": "two"})),
        reply("", ("Edit", {"file_path": str(a), "old_string": "two", "new_string": "three"})),
        reply("first done"),
        reply("", ("Write", {"file_path": str(b), "content": "new\n"}),
              ("Edit", {"file_path": str(a), "old_string": "three", "new_string": "four"})),
        reply("second done"),
        reply("redo"),
    ])
    rt = _runtime(store, provider, work)
    client = Client(store)
    tid = await client.create("a", "change a", config={"model": "m"})
    await rt.run_until_idle()
    await client.send(tid, "add b")
    await rt.run_until_idle()
    assert a.read_text() == "four\n" and b.read_text() == "new\n"
    snaps = _snapshots(await client.events(tid))
    # One snapshot per file and prompt; the first prompt's blob is kept once.
    assert [(Path(s["path"]).name, s["sha"] is None) for s in snaps] == [
        ("a.txt", False), ("b.txt", True), ("a.txt", False)]
    assert all("blob" in s for s in snaps if s["sha"])

    points = await rewind_points(client, tid)
    assert [p.text for p in points] == ["change a", "add b"]
    assert [Path(f).name for f in points[1].files] == ["b.txt", "a.txt"]
    plan = await file_plan(client, tid, points[1])
    assert plan.files == {str(b): None, str(a): b"three\n"}
    assert diff_stats(plan) == (1, 2)

    done = await rewind(client, tid, points[1])
    assert done.prompt == "add b" and done.task_id != tid
    await rt.run_until_idle()
    assert a.read_text() == "three\n" and not b.exists()
    await client.send(done.task_id, "again")
    await rt.run_until_idle()
    sent = [m.text for m in provider.requests[-1].messages if m.text]
    assert sent[0] == "change a" and sent[-1] == "again" and "add b" not in sent

    # The fork's points reach back into the conversation it continues.
    back = await rewind_points(client, done.task_id)
    assert [p.text for p in back] == ["change a", "again"]
    only_files = await rewind(client, done.task_id, back[0], conversation=False)
    assert only_files.task_id == done.task_id
    await rt.run_until_idle()
    assert a.read_text() == "one\n"
