"""Resident content comes back inside the compaction (so the request sent
equals the one rebuilt from the log), a plugin's reminder keeps working after
a compaction, and a file too large to restore is not recorded as read."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from noeta.agents import default_agents
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.types import Message
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.summary import SUMMARY_PREAMBLE
from noeta.plugins.permissions import Permissions

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
    # main's prompt and tools take ~8K tokens: a 29K cap (threshold 14,616)
    # leaves the same few-thousand-token working window the other
    # compaction tests use.
    [main, *_] = default_agents(
        "m", tools=[Probe(size=2000)], plugins={
            "permissions": Permissions(mode="bypassPermissions"),
            "compaction": Compaction(auto_compact_window=29_232)})
    model = Model([*(call() for _ in range(40)), answer("done")])
    _client, tid = await run(store, main, model, env_for=lambda _s: LocalExecEnv(str(work)))
    assert len(model.summarize_requests) >= 2
    after = [r for r in model.requests if _summarized(r.messages)]
    assert len(after) > 3
    for request in after:
        texts = [m.text for m in request.messages]
        assert texts[0].startswith("Below are instructions from the codebase"), texts[:3]
        assert texts[1].startswith(SUMMARY_PREAMBLE)
        assert sum(t.startswith("# Environment") for t in texts) == 1
        assert sum("(project instructions, part of" in t for t in texts) == 1
    pairs = await rebuilt(store, main, tid)
    assert pairs and all(live == again for live, again in pairs), pairs


class Small(Probe):
    name = "Small"


class Nudge(Extension):
    """A reminder every tenth step of a turn."""

    name = "nudge"

    async def reminders(self, view: TaskView) -> list[Reminder]:
        steps = view.state.steps_in_turn
        return [Reminder(text=f"Nudge {steps}: keep going.")] if steps and steps % 10 == 0 \
            else []


async def test_a_reminder_survives_a_compaction(store: SqliteStore) -> None:
    a = Agent(name="a", model="m", tools=[Probe(size=2000), Small(size=10)],
              extensions=(Compaction(auto_compact_window=5_232), Nudge()))
    model = Model([*(call() for _ in range(8)), *(call("Small") for _ in range(14)),
                   answer("done")])
    await run(store, a, model)
    assert model.summarize_requests
    reminded = [r for r in model.requests
                if any(m.origin == "system" and "Nudge" in m.text for m in r.messages)]
    assert any(_summarized(r.messages) for r in reminded), "no reminder after it"
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

    kept, records, _full = await restore_files([str(big), str(small)], ReadTool(), context)
    assert kept[0].text.startswith(f"{big} was read before the summary, but it is too large")
    assert [data["path"] for _ext, _kind, data in records] == [str(small)]


async def test_a_file_the_read_refuses_for_its_size_still_gets_the_note(tmp_path: Path) -> None:
    """Claude Code's ``compact_file_reference``: a file over Read's
    whole-file ceiling is named, not dropped; a missing file is dropped."""
    from noeta.core.tool import ToolContext
    from noeta.plugins.compaction.restore import restore_files
    from noeta.plugins.fs.read import ReadTool

    huge = tmp_path / "data.txt"
    huge.write_text("y" * 99 + "\n" * 1 + ("z" * 99 + "\n") * 3200)  # > 256 KB
    env = LocalExecEnv(str(tmp_path))

    def context(i: int, record: object) -> ToolContext:
        return ToolContext(task_id="t", call_id=f"r{i}", env=env, state={},
                           record=record)  # type: ignore[arg-type]

    kept, records, _full = await restore_files(
        [str(huge), str(tmp_path / "gone.txt")], ReadTool(), context)
    assert [m.text for m in kept] == [
        f"{huge} was read before the summary, but it is too large to show again here; "
        "read it with the Read tool if you need it."]
    assert records == []
