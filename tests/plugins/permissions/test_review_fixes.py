"""Review fixes: the model cannot widen its own approvals through
``.noeta/``; the project file is read once per task; "always" survives a
message taken with the answer; children start with their parent's rules."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, Waiting
from noeta.core.pending import pending, pending_tree
from noeta.plugins.permissions import Permissions
from noeta.plugins.subtasks import Subtasks
from noeta.plugins.subtasks.extension import SubagentSpec
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, events, runtime


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _start(store: SqliteStore, provider: ScriptedProvider, perms: Permissions,
                 tools: list[Any], config: dict[str, Any]) -> tuple[Client, str, Any]:
    rt = runtime(store, provider, agent([perms], tools))
    client = Client(store)
    tid = await client.create("a", "go", config=config)
    await rt.run_until_idle()
    return client, tid, rt


@pytest.mark.parametrize("mode", ["default", "acceptEdits"])
async def test_writes_into_noeta_ask_in_every_mode_but_bypass(
        store: SqliteStore, tmp_path: Path, mode: str) -> None:
    write, edit, bash = Fake("Write"), Fake("Edit"), Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Write", {"file_path": ".noeta/shell-allowlist.json"}),
        ("Edit", {"file_path": str(tmp_path / "src" / ".." / ".noeta" / "x")}),
        ("Bash", {"command": "cp a .noeta/shell-allowlist.json"}),
        ("Write", {"file_path": "notes.txt"}))])
    client, tid, _ = await _start(store, provider, Permissions(mode=mode, trusted_workspace=True),
                                  [write, edit, bash], {"cwd": str(tmp_path)})
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "approval"
    config = [d for d in last.detail.values() if ".noeta/ settings" in d["prompt"]]
    assert sorted(d["tool"] for d in config) == ["Bash", "Edit", "Write"]
    assert all(d["always"] is None for d in config)
    assert [r["file_path"] for r in write.runs] == (["notes.txt"] if mode == "acceptEdits"
                                                    else [])
    assert len(last.ids) == (3 if mode == "acceptEdits" else 4)


async def test_bypass_writes_into_noeta(store: SqliteStore, tmp_path: Path) -> None:
    write = Fake("Write")
    provider = ScriptedProvider([reply("", ("Write", {"file_path": ".noeta/x"})), reply("ok")])
    await _start(store, provider, Permissions(mode="bypassPermissions"), [write],
                 {"cwd": str(tmp_path)})
    assert len(write.runs) == 1


async def test_the_project_file_is_read_once_per_task(store: SqliteStore,
                                                      tmp_path: Path) -> None:
    bash, ask = Fake("Bash"), Fake("Ask")
    provider = ScriptedProvider([reply("", ("Ask", {})),
                                 reply("", ("Bash", {"command": "sh x.sh"})), reply("ok")])
    client, tid, rt = await _start(store, provider, Permissions(trusted_workspace=True),
                                   [bash, ask], {"cwd": str(tmp_path)})
    [item] = pending(await client.events(tid), tid)  # the task is released here
    (tmp_path / ".noeta").mkdir()
    (tmp_path / ".noeta" / "shell-allowlist.json").write_text('[{"program": "sh"}]')
    await client.answer(tid, item.request_id, "allow")
    await rt.run_until_idle()
    assert not bash.runs
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "approval"
    records = [e for e in await events(client, tid)
               if isinstance(e, ExtensionRecorded) and e.kind == "project"]
    assert len(records) == 1 and records[0].data == {"specs": []}


async def test_always_survives_a_message_taken_with_the_answer(store: SqliteStore) -> None:
    write = Fake("Write")
    provider = ScriptedProvider([reply("", ("Write", {"file_path": "a"})),
                                 reply("", ("Write", {"file_path": "b"})), reply("ok")])
    client, tid, rt = await _start(store, provider, Permissions(), [write], {})
    [item] = pending(await client.events(tid), tid)
    await client.send(tid, "also do b", when="now")
    await client.answer(tid, item.request_id, "allow", always=True)
    await rt.run_until_idle()
    assert [r["file_path"] for r in write.runs] == ["a", "b"]


async def test_a_child_starts_with_its_parents_rules(store: SqliteStore) -> None:
    write = Fake("Write")
    provider = ScriptedProvider([
        reply("", ("Write", {"file_path": "a"})),                    # parent asks
        reply("", ("Task", {"description": "do b", "prompt": "b",
                            "subagent_type": "child"})),            # parent delegates
        reply("", ("Write", {"file_path": "b"})),                    # child: no ask
        reply("wrote b"), reply("done")])
    parent = Agent(name="a", model="m", tools=[write], extensions=(
        Permissions(), Subtasks([SubagentSpec(name="child", description="c")])))
    child = Agent(name="child", model="m", tools=[write], one_shot=True,
                  extensions=(Permissions(), Subtasks()))
    rt = runtime(store, provider, parent, child)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [item] = pending(await client.events(tid), tid)
    await client.answer(tid, item.request_id, "allow", always=True)
    await rt.run_until_idle()
    assert await pending_tree(client, tid) == []
    assert [r["file_path"] for r in write.runs] == ["a", "b"]
