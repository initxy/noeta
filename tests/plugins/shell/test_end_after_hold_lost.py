"""A sub-agent that is about to end when ``hold_lost`` reaches it, after the
root already took its own copy while the sub-agent was live: the end must
not land over the unread notice, so the sub-agent itself calls its job lost
and the model hears it once."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

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
from noeta.core.store import Appended, Claim, NewTask, Patch, Post, hold_lost
from noeta.core.types import user
from noeta.plugins.shell import Registry, Shell
from noeta.testing import ScriptedProvider, reply

DEAD = "dead-host:1:x"
JOB = "<tool-use-id>toolu_9</tool-use-id>"


class LostBeforeEnd(SqliteStore):
    """Right before ``child`` appends its end, its tree's hold is reaped
    (``hold_lost`` to the root and the child) and the root runs and takes
    its copy while the child is still live."""

    def __init__(self, path: str, child: str, root: str, run_root: object) -> None:
        super().__init__(path)
        self.child, self.root, self.run_root = child, root, run_root
        self.armed = True

    async def append(self, claim: Claim, events: Sequence[Event], *, expected_seq: int,
                     patch: Patch = Patch(), posts: Sequence[Post] = (),
                     creates: Sequence[NewTask] = ()) -> Appended:
        if self.armed and claim.task_id == self.child and any(
                isinstance(e, TaskEnded) for e in events):
            self.armed = False
            await self.post(hold_lost(self.child, DEAD, 7))
            await self.post(hold_lost(self.root, DEAD, 7))
            await self.run_root()  # type: ignore[operator]
        return await super().append(claim, events, expected_seq=expected_seq, patch=patch,
                                    posts=posts, creates=creates)


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, provider: ScriptedProvider) -> Runtime:
    agent = Agent(name="a", model="m", extensions=[Shell(registry=Registry())])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                   env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]


def _told(log: Sequence[object]) -> list[str]:
    return [i.body.message.text for r in log if isinstance(r.event, InputTaken)  # type: ignore[attr-defined]
            for i in r.event.items  # type: ignore[attr-defined]
            if isinstance(i.body, Say) and JOB in i.body.message.text]


async def test_a_sub_agent_does_not_end_over_an_unread_hold_lost(tmp_path: Path) -> None:
    path = str(tmp_path / "t.db")
    provider = ScriptedProvider([reply("hello")])
    holder: dict[str, LostBeforeEnd] = {}

    async def run_root() -> None:
        await _runtime(holder["store"], provider).run_until_idle()

    plain = SqliteStore(path)
    root = await Client(plain).create("a", "hi")
    await _runtime(plain, provider).run_until_idle()
    child = f"{root}.c"
    await plain.create(NewTask(task_id=child, created=TaskCreated(
        agent="a", parent_id=root, root_id=root), inbox=[Say(message=user("crunch"))]))
    claim = await plain.claim("w", ["default"], 30.0)
    assert claim is not None and claim.task_id == child
    await plain.append(claim, [ExtensionRecorded(extension="shell", kind="started", data={
        "job_id": "bg-9", "worker": DEAD, "hold": 7, "tool_use_id": "toolu_9",
        "description": "crunch", "output_file": "/logs/bg-9.output"})], expected_seq=1)
    await plain.release(claim)
    await plain.close()

    store = holder["store"] = LostBeforeEnd(path, child, root, run_root)
    provider.add(reply("done"), reply("noted"), reply("ok"), reply("ok"))
    await _runtime(store, provider).run_until_idle()
    assert not store.armed

    child_log, root_log = await store.read(child), await store.read(root)
    told = _told(child_log) + _told(root_log)
    assert len(told) == 1 and "<status>failed</status>" in told[0]
    assert any(JOB in m.text for r in provider.requests for m in r.messages)
    assert any(isinstance(r.event, ExtensionRecorded) and r.event.kind == "lost"
               for r in child_log)
    assert isinstance(child_log[-1].event, TaskEnded)
    await store.close()
