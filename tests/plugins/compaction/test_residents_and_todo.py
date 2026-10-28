"""Resident content comes back inside the compaction (so the request sent
equals the one rebuilt from the log), the todo reminder keeps working after
a compaction, and a file too large to restore is not recorded as read."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from noeta.agents import default_agents
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.types import Message
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.summary import SUMMARY_PREAMBLE
from noeta.plugins.permissions import Permissions
from noeta.plugins.todo import Todo

from ._harness import Model, Probe, answer, call, rebuilt, run

SMALL = 34_000


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def _summarized(messages: list[Message]) -> bool:
    return any(m.text.startswith(SUMMARY_PREAMBLE) for m in messages[:2])


async def test_default_agents_residents_follow_every_summary(
        store: SqliteStore, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "AGENTS.md").write_text("RULE: always say hi")
    # main's prompt and tools take ~10K tokens: a 45K cap leaves the same
    # few-thousand-token working window the other compaction tests use.
    [main, *_] = default_agents(
        "m", tools=[Probe(size=2000)], plugins={
            "permissions": Permissions(mode="bypassPermissions"),
            "compaction": Compaction(auto_compact_window=45_000)})
    model = Model([*(call() for _ in range(40)), answer("done")])
    _client, tid = await run(store, main, model, env_for=lambda _s: LocalExecEnv(str(work)))
    assert len(model.summarize_requests) >= 2
    after = [r for r in model.requests if _summarized(r.messages)]
    assert len(after) > 3
    for request in after:
        texts = [m.text for m in request.messages]
        assert texts[0].startswith('<workspace-instructions source="AGENTS.md">'), texts[:3]
        assert texts[1].startswith(SUMMARY_PREAMBLE)
        assert sum(t.startswith("<workspace-environment>") for t in texts) == 1
        assert sum("<workspace-instructions" in t for t in texts) == 1
    pairs = await rebuilt(store, main, tid)
    assert pairs and all(live == again for live, again in pairs), pairs


class Small(Probe):
    name = "Small"


async def test_todo_reminder_survives_a_compaction(store: SqliteStore) -> None:
    a = Agent(name="a", model="m", tools=[Probe(size=2000), Small(size=10)],
              extensions=(Compaction(auto_compact_window=SMALL), Todo()))
    model = Model([*(call() for _ in range(8)), *(call("Small") for _ in range(14)),
                   answer("done")])
    await run(store, a, model)
    assert model.summarize_requests
    reminded = [r for r in model.requests
                if any(m.origin == "system" and "TodoWrite" in m.text for m in r.messages)]
    assert any(_summarized(r.messages) for r in reminded), "no todo reminder after it"
    # No rebuild check here: the harness folds the step's own reminders in
    # before calling before_model, which the live step had not recorded yet,
    # so right at the threshold its decision can differ.


async def test_a_file_too_large_to_restore_is_not_recorded_as_read(tmp_path: Path) -> None:
    from noeta.core.tool import ToolContext
    from noeta.plugins.compaction.restore import restore_files
    from noeta.plugins.fs.read import ReadTool

    small, big = tmp_path / "small.txt", tmp_path / "big.txt"
    small.write_text("alpha\n")
    big.write_text("".join(f"line {i:05d} " + "x" * 20 + "\n" for i in range(1500)))
    env = LocalExecEnv(str(tmp_path))

    def context(i: int, record: object) -> ToolContext:
        return ToolContext(task_id="t", call_id=f"r{i}", env=env, state={},
                           record=record)  # type: ignore[arg-type]

    kept, records = await restore_files([str(big), str(small)], ReadTool(), context)
    assert kept[0].text.startswith(f"{big} was read before the summary, but it is too large")
    assert [data["path"] for _ext, _kind, data in records] == [str(small)]
