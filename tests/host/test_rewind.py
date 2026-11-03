"""Rewind with file checkpoints: Edit and Write save a file's content
before they change it (once per prompt, blobs by hash), and a rewind forks
the conversation before a prompt and puts the files back."""

from __future__ import annotations

import base64
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
from noeta.plugins.checkpoints import RESTORE_SIGNAL, Checkpoints
from noeta.plugins.fs import FileTools
from noeta.plugins.subtasks import SubagentSpec, Subtasks
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
    # A rewind fires no SessionStart (Claude Code's rewind fires none).
    from noeta.core.client import CONTINUED_AS_KEY
    created = (await client.events(done.task_id))[0].event
    assert created.config[CONTINUED_AS_KEY] == "rewind"  # type: ignore[attr-defined]
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


async def test_a_restore_puts_back_only_what_this_task_snapshotted(
        store: SqliteStore, tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    a, other, outside = work / "a.txt", work / "other.txt", tmp_path / "outside.txt"
    a.write_text("one\n")
    other.write_text("keep\n")
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(a)})),
        reply("", ("Edit", {"file_path": str(a), "old_string": "one", "new_string": "two"})),
        reply("done"),
    ])
    rt = _runtime(store, provider, work)
    client = Client(store)
    tid = await client.create("a", "change a", config={"model": "m"})
    await rt.run_until_idle()

    def b64(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    await client.signal(tid, RESTORE_SIGNAL, {"files": [
        {"path": str(outside), "data": b64("planted\n")},   # outside the workspace
        {"path": str(other), "data": b64("planted\n")},     # never snapshotted
        {"path": str(other), "data": None},
        {"path": str(a), "data": b64("planted\n")},         # not a snapshot of it
        {"path": str(a), "data": None},                     # it existed then
    ]})
    await rt.run_until_idle()
    assert not outside.exists() and other.read_text() == "keep\n"
    assert a.read_text() == "two\n"
    [restored] = [r.event.data for r in await client.events(tid)
                  if isinstance(r.event, ExtensionRecorded) and r.event.kind == "restored"]
    assert restored["files"] == [] and len(restored["errors"]) == 3
    await client.signal(tid, RESTORE_SIGNAL, {"files": [{"path": str(a), "data": b64("one\n")}]})
    await rt.run_until_idle()
    assert a.read_text() == "one\n"


async def test_a_rewind_restores_what_a_sub_agent_changed(
        store: SqliteStore, tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    a = work / "a.txt"
    a.write_text("one\n")
    provider = ScriptedProvider([
        reply("", ("Task", {"description": "edit a", "prompt": "edit a",
                            "subagent_type": "child"})),                       # parent
        reply("launched"),
        reply("", ("Read", {"file_path": str(a)})),                            # child
        reply("", ("Edit", {"file_path": str(a), "old_string": "one", "new_string": "two"})),
        reply("edited"),
        reply("done"),                                                         # parent
    ])
    parent = Agent(name="a", model="m", extensions=[
        FileTools(), Checkpoints(), Subtasks([SubagentSpec(name="child", description="c")])])
    child = Agent(name="child", model="m", one_shot=True,
                  extensions=[FileTools(), Checkpoints()])
    rt = Runtime(store=store, agents={"a": parent, "child": child},
                 provider_for=lambda _m: provider,  # type: ignore[arg-type,return-value]
                 env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "delegate", config={"model": "m"})
    await rt.run_until_idle()
    assert a.read_text() == "two\n"
    assert _snapshots(await client.events(tid)) == []  # the sub-agent took the snapshot

    [point] = await rewind_points(client, tid)
    assert point.files == (str(a),)
    done = await rewind(client, tid, point)
    await rt.run_until_idle()
    assert a.read_text() == "one\n"
    # The notice reaches the task the front end goes on with.
    notices = [r.event.data for r in await client.events(done.task_id)
               if isinstance(r.event, ExtensionRecorded) and r.event.kind == "notice"]
    assert [n["text"] for n in notices] == ["restored 1 file(s)"]
