"""A sub-agent's job that finishes right after the sub-agent's end is
saved, before its worker marks the sub-agent ended: the job's end is posted
to the ended sub-agent, and the store hands it to the root, which tells the
model once."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import Event, InputTaken, Say, TaskCreated, TaskEnded
from noeta.core.runner import Runtime
from noeta.core.store import Appended, Claim, NewTask, Patch, Post
from noeta.core.types import user
from noeta.plugins.shell import Registry, Shell
from noeta.testing import ScriptedProvider, reply


class JobEndsRightAfter(SqliteStore):
    """Once ``child``'s end is committed, waits for its jobs to finish (and
    post their ends) before the runner hears of the end."""

    def __init__(self, path: str, child: str, registry: Registry) -> None:
        super().__init__(path)
        self.child, self.registry = child, registry
        self.armed = True

    async def append(self, claim: Claim, events: Sequence[Event], *, expected_seq: int,
                     patch: Patch = Patch(), posts: Sequence[Post] = (),
                     creates: Sequence[NewTask] = ()) -> Appended:
        done = await super().append(claim, events, expected_seq=expected_seq, patch=patch,
                                    posts=posts, creates=creates)
        if self.armed and claim.task_id == self.child and any(
                isinstance(e, TaskEnded) for e in events):
            self.armed = False
            for jobs in list(self.registry.jobs.values()):
                for job in list(jobs.values()):
                    await asyncio.wait_for(job.done.wait(), 10)
        return done


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, provider: ScriptedProvider, registry: Registry) -> Runtime:
    agent = Agent(name="a", model="m", extensions=[Shell(registry=registry)])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                   sleep=_no_sleep)


async def test_a_job_ending_just_after_its_sub_agent_reaches_the_root(tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"))
    provider = ScriptedProvider([reply("hello")])
    path = str(tmp_path / "t.db")
    plain = SqliteStore(path)
    root = await Client(plain).create("a", "hi", config={"cwd": str(tmp_path)})
    await _runtime(plain, provider, registry).run_until_idle()
    await plain.close()

    child = f"{root}.c"
    store = JobEndsRightAfter(path, child, registry)
    await store.create(NewTask(task_id=child, created=TaskCreated(
        agent="a", parent_id=root, root_id=root, config={"cwd": str(tmp_path)}),
        inbox=[Say(message=user("crunch"))]))
    provider.add(reply("", ("Bash", {"command": "sleep 0.3", "run_in_background": True})),
                 reply("done"), *[reply("ok") for _ in range(4)])
    await _runtime(store, provider, registry).run_until_idle()
    assert not store.armed
    await _runtime(store, provider, registry).run_until_idle()

    [call_id] = [u.id for r in provider.requests for m in r.messages if m.role == "assistant"
                 for u in m.tool_uses]
    mark = f"<tool-use-id>{call_id}</tool-use-id>"
    told = [i.body.message.text for t in (root, child) for r in await store.read(t)
            if isinstance(r.event, InputTaken) for i in r.event.items
            if isinstance(i.body, Say) and mark in i.body.message.text]
    assert len(told) == 1 and "completed" in told[0]
    assert any(mark in m.text for m in provider.requests[-1].messages)
    await store.close()
