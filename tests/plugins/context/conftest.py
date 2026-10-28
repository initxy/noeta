"""The context plugins (instructions, memory, skills, environment): a small
harness — a sqlite store, a scripted model, a workspace directory and the
local ExecEnv on it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, Recorded, ToolFinished
from noeta.core.runner import Runtime
from noeta.core.types import Message
from noeta.testing import ScriptedProvider


async def _no_sleep(_: float) -> None:
    return None


class Harness:
    def __init__(self, store: SqliteStore, work: Path) -> None:
        self.store = store
        self.work = work
        self.client = Client(store)
        self.provider = ScriptedProvider()

    def runtime(self, *agents: Agent) -> Runtime:
        return Runtime(store=self.store, agents={a.name: a for a in agents},
                       provider_for=lambda _m: self.provider,
                       env_for=lambda _s: LocalExecEnv(str(self.work)), sleep=_no_sleep)

    async def start(self, agent: Agent, goal: str = "go", **kw: Any) -> str:
        tid = await self.client.create(agent.name, goal, **kw)
        await self.runtime(agent).run_until_idle()
        return tid

    async def say(self, agent: Agent, tid: str, text: str) -> None:
        await self.client.send(tid, text)
        await self.runtime(agent).run_until_idle()

    async def events(self, tid: str) -> list[Recorded]:
        return await self.client.events(tid)

    async def records(self, tid: str, extension: str, kind: str | None = None
                      ) -> list[ExtensionRecorded]:
        return [r.event for r in await self.events(tid)
                if isinstance(r.event, ExtensionRecorded) and r.event.extension == extension
                and (kind is None or r.event.kind == kind)]

    async def results(self, tid: str) -> list[ToolFinished]:
        return [r.event for r in await self.events(tid) if isinstance(r.event, ToolFinished)]

    def last_messages(self) -> list[Message]:
        return list(self.provider.requests[-1].messages)


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    work = tmp_path / "work"
    work.mkdir()
    store = SqliteStore(str(tmp_path / "t.db"))
    yield Harness(store, work)
    await store.close()
