"""The repetition guard: off in the built-in agents unless a host sets it
(Claude Code has none), and on its own without the permissions plugin."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.agents import default_agents
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.extension import Extension
from noeta.host.pending import pending
from noeta.plugins.permissions import Permissions
from noeta.plugins.repetition_guard import RepetitionGuard
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, events, runtime

LOOP = "tool 'Read' called with identical arguments 3 times in a row " \
       "(repetition threshold 3); suspected loop"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _run(store: SqliteStore, exts: list[Extension]) -> tuple[Client, str, list[Any]]:
    read = Fake("Read")
    provider = ScriptedProvider([reply("", ("Read", {"p": 1})) for _ in range(3)])
    rt = runtime(store, provider, agent(exts, [read]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    return client, tid, await events(client, tid)


@pytest.mark.parametrize("with_permissions", [True, False])
async def test_the_third_identical_call_is_asked_about(store: SqliteStore,
                                                       with_permissions: bool) -> None:
    exts: list[Extension] = [RepetitionGuard()]
    if with_permissions:
        exts.insert(0, Permissions(mode="bypassPermissions"))
    client, tid, _ = await _run(store, exts)
    [item] = pending(await client.events(tid), tid)
    assert item.prompt == LOOP


async def test_deny_refuses_the_call(store: SqliteStore) -> None:
    _, _, evs = await _run(store, [RepetitionGuard(action="deny")])
    texts = [e.result.text for e in evs if isinstance(e, ToolFinished)]
    assert texts[-1] == LOOP and texts[:2] == ["ran Read", "ran Read"]


def test_off_in_the_built_in_agents_unless_set() -> None:
    def guards(**kw: Any) -> list[Extension]:
        main = next(a for a in default_agents("m", **kw) if a.name == "main")
        return [e for e in main.extensions if e.name == "repetition_guard"]

    assert guards() == []
    [guard] = guards(plugins={"repetition_guard": {"threshold": 4}})
    assert isinstance(guard, RepetitionGuard) and guard.policy.threshold == 4
    with pytest.raises(ValueError, match="action"):
        RepetitionGuard(action="ask")
