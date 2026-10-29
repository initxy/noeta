"""A runner that dies right after taking ``hold_lost`` (the take is
committed, the notices it decided are not) leaves nothing undecided: the
next pickup decides again, and the notice reaches the model once."""

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
    External,
    InputTaken,
    Say,
    TaskCreated,
    TaskEnded,
)
from noeta.core.runner import Runtime
from noeta.core.store import HOLD_LOST, Appended, Claim, NewTask, Patch, Post, hold_lost, patch_for
from noeta.plugins.shell import Registry, Shell
from noeta.testing import ScriptedProvider, reply

DEAD = "dead-host:1:x"


class Crash(BaseException):
    """Stands in for the process dying."""


class CrashAfterTake(SqliteStore):
    """Dies right after committing the first append that takes ``name``."""

    def __init__(self, path: str, name: str) -> None:
        super().__init__(path)
        self.name = name
        self.armed = False

    async def append(self, claim: Claim, events: Sequence[Event], *, expected_seq: int,
                     patch: Patch = Patch(), posts: Sequence[Post] = (),
                     creates: Sequence[NewTask] = ()) -> Appended:
        done = await super().append(claim, events, expected_seq=expected_seq, patch=patch,
                                    posts=posts, creates=creates)
        if self.armed and any(isinstance(e, InputTaken) and any(
                isinstance(i.body, External) and i.body.name == self.name for i in e.items)
                for e in events):
            self.armed = False
            raise Crash()
        return done


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, provider: ScriptedProvider) -> Runtime:
    agent = Agent(name="a", model="m", extensions=[Shell(registry=Registry())])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                   env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]


async def _expire(store: SqliteStore, task_id: str) -> None:
    def tx(conn: Any) -> None:
        conn.execute("UPDATE tasks SET lease_until = 0 WHERE task_id = ? AND status = 'running'",
                     (task_id,))

    await store._run(tx)


async def _ended_child_with_a_job(store: SqliteStore, root: str, epoch: int) -> None:
    """A sub-agent of ``root`` whose log shows a job running under hold
    ``epoch`` of a dead worker, and its end."""
    child = f"{root}.c"
    await store.create(NewTask(task_id=child, created=TaskCreated(
        agent="a", parent_id=root, root_id=root)))
    claim = await store.claim("w", ["default"], 30.0)
    assert claim is not None and claim.task_id == child
    events: list[Event] = [
        ExtensionRecorded(extension="shell", kind="started", data={
            "job_id": "bg-9", "worker": DEAD, "hold": epoch, "tool_use_id": "toolu_9",
            "description": "crunch", "output_file": "/logs/bg-9.output"}),
        TaskEnded(status="done", answer="done")]
    await store.append(claim, events, expected_seq=1, patch=patch_for(events, 0))


async def test_a_root_that_died_after_taking_hold_lost_still_tells_the_loss(
        tmp_path: Path) -> None:
    path = str(tmp_path / "t.db")
    store = CrashAfterTake(path, HOLD_LOST)
    provider = ScriptedProvider([reply("hello")])
    root = await Client(store).create("a", "hi")
    await _runtime(store, provider).run_until_idle()
    await _ended_child_with_a_job(store, root, epoch=7)

    store.armed = True
    await store.post(hold_lost(root, DEAD, 7))
    try:
        await _runtime(store, provider).run_until_idle()
    except Crash:
        pass
    else:
        raise AssertionError("the runner never took hold_lost")
    await store.close()

    again = SqliteStore(path)
    await _expire(again, root)
    provider.add(reply("noted"))
    await _runtime(again, provider).run_until_idle()
    told = [i.body.message.text for r in await again.read(root)
            if isinstance(r.event, InputTaken) for i in r.event.items
            if isinstance(i.body, Say) and "<tool-use-id>toolu_9</tool-use-id>" in
            i.body.message.text]
    assert len(told) == 1 and "<status>failed</status>" in told[0]
    assert "<tool-use-id>toolu_9</tool-use-id>" in provider.requests[-1].messages[-1].text
    # A later pickup decides nothing twice.
    await again.post(Post(task_id=root, body=Say(message=provider.requests[0].messages[0])))
    provider.add(reply("again"))
    await _runtime(again, provider).run_until_idle()
    assert sum("<tool-use-id>toolu_9</tool-use-id>" in m.text
               for m in provider.requests[-1].messages) == 1
    await again.close()
