"""Review fixes: resident content comes back inside the compaction (right
after the summary, so the request sent equals the one rebuilt from the log),
and the todo reminder keeps working once a compaction pointer trails the
request."""

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
from noeta.plugins.compaction.summary import SUMMARY_FRAME
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
    return bool(messages) and messages[0].text.startswith(SUMMARY_FRAME[:30])


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
            "compaction": Compaction(auto_compact_window=45_000, clearable_tools=())})
    model = Model([*(call() for _ in range(40)), answer("done")])
    _client, tid = await run(store, main, model, env_for=lambda _s: LocalExecEnv(str(work)))
    assert len(model.summarize_requests) >= 3
    after = [r for r in model.requests if _summarized(r.messages)]
    assert len(after) > 3
    for request in after:
        texts = [m.text for m in request.messages]
        assert texts[1].startswith("Below are instructions from the codebase"), texts[:3]
        assert texts[2].startswith("# Environment")
        assert sum("(project instructions, part of" in t for t in texts) == 1
    pairs = await rebuilt(store, main, tid)
    assert pairs and all(live == again for live, again in pairs), pairs


class Small(Probe):
    name = "Small"


async def test_todo_reminder_survives_the_compaction_pointer(store: SqliteStore) -> None:
    a = Agent(name="a", model="m", tools=[Probe(size=2000), Small(size=10)],
              extensions=(Compaction(auto_compact_window=SMALL, clearable_tools=()), Todo()))
    model = Model([*(call() for _ in range(8)), *(call("Small") for _ in range(14)),
                   answer("done")])
    _client, tid = await run(store, a, model)
    assert model.summarize_requests
    reminded = [r for r in model.requests
                if any(m.origin == "system" and "TodoWrite" in m.text for m in r.messages)]
    assert reminded, "no todo reminder after the compaction"
    # The unrecorded pointer stays last, the reminder right before it.
    first = reminded[0].messages
    assert first[-1].text.startswith("The note at the head")
    assert "TodoWrite" in first[-2].text
    pairs = await rebuilt(store, a, tid)
    assert pairs and all(live == again for live, again in pairs), pairs


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

    kept, records = await restore_files([str(big), str(small)], ReadTool(), context,
                                        tail_budget=0)
    assert kept[0].text.startswith(f"Note: {big} was read before")
    assert [data["path"] for _ext, _kind, data in records] == [str(small)]
