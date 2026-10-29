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
from noeta.core.records import CORE
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


class Notes(Extension):
    """Several reminders due at once, out of order, on the first call."""

    def __init__(self, name: str, *reminders: Reminder) -> None:
        self.name = name
        self._reminders = reminders

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        return self._reminders if len(view.state.messages) == 1 else ()


async def test_user_reminders_come_first_then_system_turns_by_order(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("ok")])
    agent = Agent(name="a", model="m", extensions=[
        Notes("late", Reminder(text="date", role="system", order=90)),
        Notes("early", Reminder(text="env", role="system", order=0),
              Reminder(text="in the user turn")),
    ])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 sleep=_no_sleep)
    await Client(store).create("a", "go")
    await rt.run_until_idle()
    [request] = provider.requests
    assert [(m.role, m.text) for m in request.messages] == [
        ("user", "go"), ("user", "in the user turn"), ("system", "env"), ("system", "date")]
    assert request.messages[1].origin == "system"
    # One breakpoint, on the newest message, as Claude Code places it.
    assert request.cache_after == [3]
    await store.close()


class ErasesThenReminds(Extension):
    """On the first call, erases the person's message (as a blocked prompt
    is) and reminds, without failing the turn."""

    name = "eraser"

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        if any(m.origin == "system" for m in view.state.messages):
            return ()
        view.record(CORE, "erased", {"seqs": list(view.state.message_seqs)})
        return (Reminder(text="Note."),)


async def test_a_reminder_round_that_erases_messages_sends_what_the_log_holds(
        tmp_path: Path) -> None:
    """The request's recorded messages are taken again from the log once the
    reminders are recorded, not counted on: an erase among them shifts the
    count."""
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("ok")])
    agent = Agent(name="a", model="m", extensions=[ErasesThenReminds()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 sleep=_no_sleep)
    await Client(store).create("a", "secret")
    await rt.run_until_idle()
    [request] = provider.requests
    assert [m.text for m in request.messages] == ["Note."]
    await store.close()
