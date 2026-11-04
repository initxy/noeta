"""A call a crash left unfinished: a ``retry_safe`` tool runs again, a tool
with ``recover`` reports what it finds, any other is answered "outcome
unknown"."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished, ToolStarted
from noeta.core.runner import INTERRUPTED_CALL_ERROR, Runtime
from noeta.core.tool import MAX_ATTEMPTS, ToolContext, ToolFlags, ToolOutput, text_output
from noeta.testing import ScriptedProvider, reply


class Crash(BaseException):
    """Stands in for the worker dying while the tool runs."""


class Probe:
    """Crashes its first ``crashes`` runs; keeps every run's key."""

    name = "Probe"
    description = "Look something up."
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = True
    clearable = False

    def __init__(self, crashes: int, *, retry_safe: bool = False,
                 found: ToolOutput | None = None) -> None:
        self.crashes = crashes
        self.flags = ToolFlags(retry_safe=retry_safe)
        self.found = found
        self.keys: list[str] = []
        self.recovered: list[str] = []

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.keys.append(ctx.idempotency_key)
        if len(self.keys) <= self.crashes:
            raise Crash()
        return text_output("found it")


class Recovering(Probe):
    async def recover(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput | None:
        self.recovered.append(ctx.idempotency_key)
        return self.found


async def _no_sleep(_: float) -> None:
    return None


async def _expire(store: SqliteStore, task_id: str) -> None:
    def tx(conn: Any) -> None:
        conn.execute("UPDATE tasks SET lease_until = 0 WHERE task_id = ? AND status = 'running'",
                     (task_id,))

    await store._run(tx)


async def _run(tmp_path: Path, tool: Probe) -> tuple[list[Any], str]:
    """Run one task calling ``tool`` once, restarting after every crash."""
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("", ("Probe", {}))] + [reply("done")] * 5)
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", tools=[tool])},
                 provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep)
    tid = await Client(store).create("a", "go")
    for _ in range(MAX_ATTEMPTS + 2):
        try:
            await rt.run_until_idle()
            break
        except Crash:
            await _expire(store, tid)
    events = [r.event for r in await store.read(tid)]
    await store.close()
    finished = [e.result.text for e in events if isinstance(e, ToolFinished)]
    assert len(finished) == 1
    return events, finished[0]


def _starts(events: list[Any]) -> int:
    return sum(isinstance(e, ToolStarted) for e in events)


async def test_a_retry_safe_call_runs_again_after_a_crash(tmp_path: Path) -> None:
    tool = Probe(crashes=1, retry_safe=True)
    events, result = await _run(tmp_path, tool)
    assert result == "found it"
    assert _starts(events) == 2
    assert len(tool.keys) == 2 and tool.keys[0] == tool.keys[1]
    assert tool.keys[0].count(":call_") == 1


async def test_any_other_call_is_answered_outcome_unknown(tmp_path: Path) -> None:
    tool = Probe(crashes=1)
    events, result = await _run(tmp_path, tool)
    assert result == INTERRUPTED_CALL_ERROR
    assert _starts(events) == 1 and len(tool.keys) == 1


async def test_a_call_that_keeps_crashing_stops_being_retried(tmp_path: Path) -> None:
    tool = Probe(crashes=99, retry_safe=True)
    events, result = await _run(tmp_path, tool)
    assert result == INTERRUPTED_CALL_ERROR
    assert _starts(events) == MAX_ATTEMPTS == len(tool.keys)


async def test_recover_reports_the_outcome_it_finds(tmp_path: Path) -> None:
    tool = Recovering(crashes=1, found=text_output("it went through"))
    events, result = await _run(tmp_path, tool)
    assert result == "it went through"
    assert _starts(events) == 1 and len(tool.keys) == 1
    assert tool.recovered == tool.keys


async def test_recover_that_cannot_tell_leaves_outcome_unknown(tmp_path: Path) -> None:
    tool = Recovering(crashes=1)
    _events, result = await _run(tmp_path, tool)
    assert result == INTERRUPTED_CALL_ERROR
    assert len(tool.recovered) == 1


async def test_recover_comes_before_running_again(tmp_path: Path) -> None:
    tool = Recovering(crashes=1, retry_safe=True)
    events, result = await _run(tmp_path, tool)
    assert result == "found it" and _starts(events) == 2
    assert len(tool.recovered) == 1
