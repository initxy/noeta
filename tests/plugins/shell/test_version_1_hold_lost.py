"""Upgrading a version 1 store posts ``hold_lost`` with epoch 0 for its old
holds. Jobs a version 1 worker recorded carry no hold epoch (and their
worker is the process, not the hold's worker): epoch 0 covers exactly those
running jobs, so the model hears they are lost, and leaves jobs with an
epoch alone."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import (
    Event,
    ExtensionRecorded,
    InputTaken,
    Say,
    TaskCreated,
    TaskEnded,
)
from noeta.core.runner import Runtime
from noeta.core.store import NewTask, hold_lost, patch_for
from noeta.plugins.shell import Registry, Shell
from noeta.testing import ScriptedProvider, reply

OLD_HOLDER = "w-old"


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, provider: ScriptedProvider) -> Runtime:
    agent = Agent(name="a", model="m", extensions=[Shell(registry=Registry())])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                   env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]


def _started(n: int, **holder: Any) -> ExtensionRecorded:
    return ExtensionRecorded(extension="shell", kind="started", data={
        "job_id": f"bg-{n}", "tool_use_id": f"toolu_{n}", "description": "crunch",
        "output_file": f"/logs/bg-{n}.output", **holder})


def _told(log: Sequence[Any], n: int) -> list[str]:
    return [i.body.message.text for r in log if isinstance(r.event, InputTaken)
            for i in r.event.items if isinstance(i.body, Say)
            and f"<tool-use-id>toolu_{n}</tool-use-id>" in i.body.message.text]


async def test_epoch_0_marks_a_version_1_job_lost_and_no_other(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("hello")])
    root = await Client(store).create("a", "hi")
    await _runtime(store, provider).run_until_idle()
    child = f"{root}.c"
    await store.create(NewTask(task_id=child, created=TaskCreated(
        agent="a", parent_id=root, root_id=root)))
    claim = await store.claim("w", ["default"], 30.0)
    assert claim is not None and claim.task_id == child
    await store.append(claim, [
        _started(1, worker="old-host:42"),                # version 1: no epoch
        _started(2, worker=OLD_HOLDER, hold=3)], expected_seq=1)
    await store.release(claim)

    await store.post(hold_lost(child, OLD_HOLDER, 0))
    provider.add(reply("noted"), reply("ok"), reply("ok"))
    await _runtime(store, provider).run_until_idle()

    log = await store.read(child)
    lost = [r.event.data["job_id"] for r in log if isinstance(r.event, ExtensionRecorded)
            and r.event.kind == "lost"]
    assert lost == ["bg-1"]
    told = _told(log, 1)
    assert len(told) == 1 and "<status>failed</status>" in told[0]
    assert _told(log, 2) == []
    await store.close()


async def test_epoch_0_tells_the_root_of_an_ended_sub_agents_version_1_job(
        tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("hello")])
    root = await Client(store).create("a", "hi")
    await _runtime(store, provider).run_until_idle()
    child = f"{root}.c"
    await store.create(NewTask(task_id=child, created=TaskCreated(
        agent="a", parent_id=root, root_id=root)))
    claim = await store.claim("w", ["default"], 30.0)
    assert claim is not None and claim.task_id == child
    events: list[Event] = [_started(9, worker="old-host:42"),
                           _started(8, worker=OLD_HOLDER, hold=3),
                           TaskEnded(status="done", answer="done")]
    await store.append(claim, events, expected_seq=1, patch=patch_for(events, 0))

    await store.post(hold_lost(root, OLD_HOLDER, 0))
    provider.add(reply("noted"))
    await _runtime(store, provider).run_until_idle()

    log = await store.read(root)
    told = _told(log, 9)
    assert len(told) == 1 and "<status>failed</status>" in told[0]
    assert _told(log, 8) == []
    await store.close()
