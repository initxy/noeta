"""Scheduled prompts: cron parsing and offsets, ScheduleWakeup's clamp and
stop, cron jobs that fire and re-arm, ``/loop`` expansion at schedule time,
and the one fallback wake-up of a loop iteration that forgot to re-arm."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import Event, ExtensionRecorded, InputTaken, TimerFired, ToolFinished
from noeta.core.runner import Runtime
from noeta.core.store import Appended, Claim, NewTask, Patch, Post
from noeta.plugins.schedule import JOB_TIMER, WAKEUP_TIMER, Schedule, cron, texts
from noeta.testing import ScriptedProvider, reply

# ------------------------------------------------------------ cron


def test_parse_accepts_lists_ranges_steps_and_sunday_as_seven() -> None:
    c = cron.parse("*/15 9-17 * * 1-5")
    assert c is not None and c.minute == frozenset({0, 15, 30, 45})
    assert c.hour == frozenset(range(9, 18)) and c.weekday == frozenset(range(1, 6))
    sunday = cron.parse("0 9 * * 7")
    assert sunday is not None and sunday.weekday == frozenset({0})
    for bad in ("* * * *", "60 * * * *", "* * 0 * *", "5-1 * * * *", "*/0 * * * *", "a * * * *"):
        assert cron.parse(bad) is None, bad


def test_next_match_is_the_next_local_minute_that_matches() -> None:
    start = datetime(2026, 10, 28, 9, 7, 30).timestamp()
    nxt = cron.next_match("30 14 * * *", start)
    assert nxt is not None and datetime.fromtimestamp(nxt) == datetime(2026, 10, 28, 14, 30)
    nxt = cron.next_match("0 9 * * 1", start)  # 2026-10-28 is a Wednesday
    assert nxt is not None and datetime.fromtimestamp(nxt) == datetime(2026, 11, 2, 9, 0)
    assert cron.next_match("0 0 31 2 *", start) is None  # Feb 31 never comes


def test_offsets_late_for_recurring_early_for_one_shot_on_the_half_hour() -> None:
    start = datetime(2026, 10, 28, 9, 7, 30).timestamp()
    hourly = cron.next_fire("7 * * * *", start, "80000000", recurring=True)
    # half the job's fraction (0.5) of the one-hour period: 15 minutes late
    assert hourly == datetime(2026, 10, 28, 10, 7).timestamp() + 900
    capped = cron.next_fire("7 9 * * *", start, "ffffffff", recurring=True)
    assert capped is not None and capped - datetime(2026, 10, 29, 9, 7).timestamp() == 1800
    once = cron.next_fire("30 14 28 10 *", start, "80000000", recurring=False)
    assert once == datetime(2026, 10, 28, 14, 30).timestamp() - 45
    off = cron.next_fire("31 14 28 10 *", start, "80000000", recurring=False)
    assert off == datetime(2026, 10, 28, 14, 31).timestamp()
    five = cron.next_fire("*/5 * * * *", start, "00000000", recurring=True)
    assert five == start + 300 - cron.CACHE_LEAD_SECONDS


def test_describe_reads_the_common_shapes() -> None:
    assert cron.describe("*/5 * * * *") == "Every 5 minutes"
    assert cron.describe("7 * * * *") == "Every hour at :07"
    assert cron.describe("0 */2 * * *") == "Every 2 hours"
    assert cron.describe("57 8 * * *") == "Every day at 8:57 AM"
    assert cron.describe("0 9 * * 1-5") == "Weekdays at 9:00 AM"
    assert cron.describe("0 9 * * 0") == "Every Sunday at 9:00 AM"
    assert cron.describe("0 9 1 * *") == "0 9 1 * *"


# ------------------------------------------------------------ in a task


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def runtime(store: SqliteStore, provider: ScriptedProvider, ext: Schedule) -> Runtime:
    agent = Agent(name="a", model="m", extensions=[ext])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                   env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]


def _results(events: list[object]) -> list[str]:
    return [e.result.text for e in events if isinstance(e, ToolFinished)]


async def test_wakeup_clamps_expands_the_loop_and_fires_when_due(store: SqliteStore) -> None:
    past = time.time() - 10_000  # every wake-up computed from it is already due
    provider = ScriptedProvider([
        reply("", (texts.WAKEUP_TOOL, {"delaySeconds": 5, "reason": "watching CI",
                                       "prompt": "/loop check the deploy", "noop": True})),
        reply("waiting"),
        reply("", (texts.WAKEUP_TOOL, {"stop": True})),
        reply("stopped"),
    ])
    rt = runtime(store, provider, Schedule(clock=lambda: past))
    client = Client(store)
    tid = await client.create("a", "/loop check the deploy")
    await rt.run_until_idle()
    events = [r.event for r in await client.events(tid)]
    first = _results(events)[0]
    assert "(in " in first and "clamped to 60s" in first
    # The fired prompt is the /loop command's text, expanded when scheduled.
    fired = provider.requests[2].messages[-1].text
    assert fired.startswith("# /loop") and fired.endswith("check the deploy")
    stopped = _results(events)[-1]
    assert stopped.startswith("Loop stopped: no wake-up was pending")
    assert not any(isinstance(e, ExtensionRecorded) and e.kind == "timer"
                   and e.data["name"] == WAKEUP_TIMER and e.data["at"] > time.time()
                   for e in events)


async def test_wakeup_validates_the_fields_it_needs(store: SqliteStore) -> None:
    provider = ScriptedProvider([
        reply("", (texts.WAKEUP_TOOL, {"reason": "x", "prompt": "p", "noop": True}),
              (texts.WAKEUP_TOOL, {"delaySeconds": 60, "reason": "x", "noop": True}),
              (texts.WAKEUP_TOOL, {"delaySeconds": 60, "reason": "x", "prompt": "p"})),
        reply("done"),
    ])
    rt = runtime(store, provider, Schedule())
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert _results([r.event for r in await client.events(tid)]) == [
        texts.WAKEUP_NEEDS_DELAY, texts.WAKEUP_NEEDS_PROMPT, texts.WAKEUP_NEEDS_NOOP]


async def test_an_autonomous_tick_that_forgets_to_rearm_gets_one_fallback(
        store: SqliteStore) -> None:
    past = time.time() - 10_000
    provider = ScriptedProvider([
        reply("", (texts.WAKEUP_TOOL, {"delaySeconds": 600, "reason": "idle",
                                       "prompt": texts.DYNAMIC_SENTINEL, "noop": True})),
        reply("armed"),
        reply("tick without re-arming"),   # the wake-up fires; no ScheduleWakeup
        reply("fallback tick"),            # the keepalive fires once
    ])
    rt = runtime(store, provider, Schedule(clock=lambda: past))
    client = Client(store)
    tid = await client.create("a", "/loop")
    await rt.run_until_idle()
    assert provider.requests[2].messages[-1].text == texts.DYNAMIC_TICK
    assert provider.requests[3].messages[-1].text == texts.DYNAMIC_TICK
    assert not provider.steps  # and no third tick: the fallback is armed once
    row = await client.get(tid)
    assert row is not None and row.ready_at is None


async def test_cron_jobs_create_list_fire_rearm_and_delete(store: SqliteStore) -> None:
    provider = ScriptedProvider([
        reply("", ("ToolSearch", {"query": "select:CronCreate,CronList,CronDelete"})),
        reply("", (texts.CRON_CREATE_TOOL, {"cron": "*/5 * * * *", "prompt": "check CI"}),
              (texts.CRON_CREATE_TOOL, {"cron": "31 14 28 2 *", "prompt": "once",
                                        "recurring": False}),
              (texts.CRON_CREATE_TOOL, {"cron": "bad", "prompt": "x"})),
        reply("", (texts.CRON_LIST_TOOL, {})),
        reply("scheduled"),
    ])
    ext = Schedule()
    rt = runtime(store, provider, ext)
    client = Client(store)
    tid = await client.create("a", "set it up")
    await rt.run_until_idle()
    events = [r.event for r in await client.events(tid)]
    results = _results(events)
    assert results[1].startswith("Scheduled recurring job ")
    assert results[2].startswith("Scheduled one-shot job ") and "deletes itself" in results[2]
    assert results[3] == texts.CRON_INVALID.format(cron="bad")
    listing = results[4].splitlines()
    assert len(listing) == 2 and "Every 5 minutes (recurring)" in listing[0]
    assert "(one-shot)" in listing[1] and listing[1].endswith(": once")
    row = await client.get(tid)
    assert row is not None and row.ready_at is not None and row.ready_at > time.time()
    jobs = (await _state(client, tid, ext))["jobs"]
    recurring = next(j for j in jobs.values() if j["recurring"])
    # Firing (as the runner would on the due timer) re-arms a recurring job.
    provider.add(reply("checked"), reply("", (texts.CRON_DELETE_TOOL, {"id": recurring["id"]}),
                                         (texts.CRON_DELETE_TOOL, {"id": "nope"})),
                 reply("gone"))
    await store.post(Post(task_id=tid, body=TimerFired(name=JOB_TIMER + recurring["id"])))
    await rt.run_until_idle()
    assert provider.requests[4].messages[-1].text == "check CI"
    after = (await _state(client, tid, ext))["jobs"][recurring["id"]]
    assert after["next_at"] > recurring["next_at"] - 1
    await client.send(tid, "cancel it")
    await rt.run_until_idle()
    results = _results([r.event for r in await client.events(tid)])
    assert results[-2:] == [texts.CRON_DELETED.format(id=recurring["id"]),
                            texts.CRON_UNKNOWN.format(id="nope")]
    assert recurring["id"] not in (await _state(client, tid, ext))["jobs"]


async def test_a_continued_task_does_not_inherit_its_sources_jobs(store: SqliteStore) -> None:
    provider = ScriptedProvider([
        reply("", ("ToolSearch", {"query": "select:CronCreate,CronList"})),
        reply("", (texts.CRON_CREATE_TOOL, {"cron": "*/5 * * * *", "prompt": "check CI"})),
        reply("scheduled"),
        reply("", (texts.CRON_LIST_TOOL, {})),
        reply("listed"),
    ])
    ext = Schedule()
    rt = runtime(store, provider, ext)
    client = Client(store)
    src = await client.create("a", "set it up")
    await rt.run_until_idle()
    assert (await _state(client, src, ext))["jobs"]
    fork = await client.create("a", "what is scheduled here?", history_from=src)
    await rt.run_until_idle()
    assert _results([r.event for r in await client.events(fork)]) == [texts.CRON_NONE]


async def _state(client: Client, tid: str, ext: Schedule) -> dict[str, object]:
    st: object = ext.initial_state()
    for r in await client.events(tid):
        e = r.event
        if isinstance(e, ExtensionRecorded) and e.extension == "schedule":
            st = ext.reduce(st, e.kind, e.data)
    assert isinstance(st, dict)
    return st


async def test_loop_is_a_command_front_ends_list() -> None:
    from noeta.core.commands import expand_command, list_commands
    from noeta.core.extension import CommandContext

    agent = Agent(name="a", model="m", extensions=[Schedule()])
    ctx = CommandContext(cwd="/tmp")
    assert [c.name for c in await list_commands(agent, ctx)] == ["loop"]
    text = await expand_command(agent, ctx, "/loop 5m /babysit")
    assert text is not None and text.startswith("# /loop") and text.endswith("5m /babysit")


# ------------------------------------------------------------ a crash mid-firing


class Crash(BaseException):
    """Stands in for the process dying."""


class CrashAfterFiring(SqliteStore):
    """Dies right after committing the first append that takes a fired
    timer (once ``armed``): the firing is in the log, its follow-up is not."""

    armed = False

    async def append(self, claim: Claim, events: Sequence[Event], *, expected_seq: int,
                     patch: Patch = Patch(), posts: Sequence[Post] = (),
                     creates: Sequence[NewTask] = ()) -> Appended:
        done = await super().append(claim, events, expected_seq=expected_seq, patch=patch,
                                    posts=posts, creates=creates)
        if self.armed and any(isinstance(e, InputTaken) and any(
                isinstance(i.body, TimerFired) for i in e.items) for e in events):
            self.armed = False
            raise Crash()
        return done


async def _crash_then_recover(path: str, crashing: CrashAfterFiring, tid: str,
                              make: Callable[[SqliteStore], Runtime]) -> SqliteStore:
    """Run until the armed crash, then pick the task up in a new process."""
    with pytest.raises(Crash):
        await make(crashing).run_until_idle()
    await crashing.close()
    store = SqliteStore(path)

    def tx(conn: Any) -> None:
        conn.execute("UPDATE tasks SET lease_until = 0 WHERE task_id = ?", (tid,))

    await store._run(tx)
    await make(store).run_until_idle()
    return store


async def test_a_crash_after_a_cron_job_fires_does_not_drop_it(tmp_path: Path) -> None:
    path = str(tmp_path / "t.db")
    crashing = CrashAfterFiring(path)
    provider = ScriptedProvider([
        reply("", ("ToolSearch", {"query": "select:CronCreate"})),
        reply("", (texts.CRON_CREATE_TOOL, {"cron": "*/5 * * * *", "prompt": "check CI"}),
              (texts.CRON_CREATE_TOOL, {"cron": "31 14 28 2 *", "prompt": "once",
                                        "recurring": False})),
        reply("scheduled"),
        reply("checked"),
    ])
    ext = Schedule()
    client = Client(crashing)
    tid = await client.create("a", "set it up")
    await runtime(crashing, provider, ext).run_until_idle()
    jobs = (await _state(client, tid, ext))["jobs"]
    assert isinstance(jobs, dict) and len(jobs) == 2
    crashing.armed = True
    for job_id in jobs:
        await crashing.post(Post(task_id=tid, body=TimerFired(name=JOB_TIMER + job_id)))
    store = await _crash_then_recover(path, crashing, tid, lambda s: runtime(s, provider, ext))
    after = (await _state(Client(store), tid, ext))["jobs"]
    assert isinstance(after, dict)
    recurring = next(j for j in jobs.values() if j["recurring"])
    assert list(after) == [recurring["id"]]  # the one-shot job is done
    row = await store.get(tid)
    assert row is not None and row.ready_at is not None and row.ready_at > time.time()
    await store.close()


async def test_a_crash_after_a_loop_wakeup_fires_keeps_its_fallback(tmp_path: Path) -> None:
    path = str(tmp_path / "t.db")
    crashing = CrashAfterFiring(path)
    crashing.armed = True
    past = time.time() - 10_000
    provider = ScriptedProvider([
        reply("", (texts.WAKEUP_TOOL, {"delaySeconds": 600, "reason": "idle",
                                       "prompt": texts.DYNAMIC_SENTINEL, "noop": True})),
        reply("armed"),
        reply("tick without re-arming"),   # the wake-up fired before the crash
        reply("fallback tick"),            # the keepalive still fires once
    ])
    ext = Schedule(clock=lambda: past)
    tid = await Client(crashing).create("a", "/loop")
    store = await _crash_then_recover(path, crashing, tid, lambda s: runtime(s, provider, ext))
    assert provider.requests[3].messages[-1].text == texts.DYNAMIC_TICK
    assert not provider.steps
    await store.close()
