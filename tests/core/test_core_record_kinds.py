"""A plugin cannot write a ``core`` record of a kind the core does not know:
folding one leaves the task to a newer worker, so this one would never
run it again."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.core.records import CORE
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.testing import ScriptedProvider, reply


class BogusRecord:
    name = "Bogus"
    description = "Write a core record of an unknown kind."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ctx.record(CORE, "not_a_core_kind", {})
        return text_output("recorded")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def test_a_view_refuses_an_unknown_core_kind() -> None:
    view = TaskView(state=TaskState())
    with pytest.raises(ValueError, match="not_a_core_kind"):
        view.record(CORE, "not_a_core_kind", {})
    assert view.pending == []
    view.record("mine", "not_a_core_kind", {})  # an extension's own kinds are its own
    assert len(view.pending) == 1


async def test_a_tool_writing_an_unknown_core_kind_fails_and_the_task_goes_on(
        store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Bogus", {})), reply("done")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", tools=[BogusRecord()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [result] = [r.event.result for r in await client.events(tid)
                if isinstance(r.event, ToolFinished)]
    assert result.is_error and "not_a_core_kind" in result.text
    assert (await client.get(tid)).waiting_on == "input"


def test_continue_and_turn_done_decode_old_and_new_entries() -> None:
    from noeta.core import records
    from noeta.core.events import ExtensionRecorded
    from noeta.core.types import Usage

    def decode(kind: str, data: Any) -> Any:
        return records.decode(ExtensionRecorded(extension=CORE, kind=kind, data=data))

    # Entries written before they carried data.
    assert decode("continue", None) == records.Continue()
    assert decode("turn_done", None) == records.TurnDone()
    usage = Usage(uncached=10, output=5)
    for cls in (records.Continue, records.TurnDone):
        rec = cls(reason=records.EMPTY_REPLY, model="m", usage=usage)
        assert decode(rec.kind, rec.data()) == rec
        assert cls().data() is None
    # A malformed usage is dropped, the rest stands.
    bad = decode("turn_done", {"reason": "empty_reply", "usage": {"output": "x"}})
    assert bad.reason == "empty_reply" and bad.usage is None


def test_the_nudge_is_folded_from_the_log() -> None:
    # Whichever worker folds the log knows the model was asked already.
    from noeta.core import records
    from noeta.core.events import ModelReplied, Recorded
    from noeta.core.loop.state import apply
    from noeta.core.runner.texts import EMPTY_REPLY_NUDGE
    from noeta.core.types import user

    state = TaskState()
    nudge = records.event(records.Continue(reason=records.EMPTY_REPLY),
                          message=user(EMPTY_REPLY_NUDGE, origin="inline"))
    state.model_attempts = 2
    apply(state, Recorded(seq=1, at=0.0, event=nudge), {})
    assert state.empty_nudged and state.model_attempts == 0
    apply(state, Recorded(seq=2, at=0.0, event=ModelReplied(response=reply("ok"))), {})
    assert not state.empty_nudged
