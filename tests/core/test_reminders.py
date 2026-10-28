"""``Extension.reminders``: recorded before the model call, placed in that
request after the recorded messages, and rebuilt identically from the log."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.loop.request import cache_breakpoints
from noeta.core.runner import Runtime
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


class EverySecondCall(Extension):
    """Reminds before the second model call of the task only."""

    name = "nudge"

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        replies = sum(1 for m in view.state.messages if m.role == "assistant")
        if replies != 1:
            return ()
        return (Reminder(text="Remember the tests.", kind="nudged", data={"n": 1}),)


async def test_a_reminder_is_recorded_and_sent_where_the_log_puts_it(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("first"), reply("second")])
    agent = Agent(name="a", model="m", extensions=[EverySecondCall()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "one")
    await rt.run_until_idle()
    await client.send(tid, "two")
    await rt.run_until_idle()

    first, second = provider.requests
    assert all(m.origin != "system" for m in first.messages)
    assert [m.text for m in second.messages] == ["one", "first", "two", "Remember the tests."]
    assert second.messages[-1].origin == "system"
    [rec] = [r.event for r in await client.events(tid)
             if isinstance(r.event, ExtensionRecorded) and r.event.extension == "nudge"]
    assert rec.kind == "nudged" and rec.data == {"n": 1}

    # The breakpoints are the ones a fresh fold of the log gives.
    from noeta.core.loop.state import fold

    records = await client.events(tid)
    upto = next(i for i, r in enumerate(records) if r.event == rec)
    state = fold(records[: upto + 1], {}, {})
    assert second.cache_after == cache_breakpoints(state)
    await store.close()
