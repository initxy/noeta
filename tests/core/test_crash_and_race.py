"""Phase-1 gates: a crash at every write boundary recovers without re-running
a finished tool, and racing runners never double-write."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import StoreUnavailable
from noeta.core.events import Event, TaskCreated, ToolFinished, ToolStarted
from noeta.core.runner import INTERRUPTED_CALL_ERROR, Runtime
from noeta.core.store import Appended, Claim, NewTask, Patch, Post
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolResult, ToolUse, Usage


class Crash(BaseException):
    """Stands in for the process dying."""


class CrashingStore(SqliteStore):
    """Dies at the ``crash_at``-th append, before or after it commits."""

    def __init__(self, path: str, crash_at: int, after_commit: bool) -> None:
        super().__init__(path)
        self.crash_at = crash_at
        self.after_commit = after_commit
        self.appends = 0

    async def append(self, claim: Claim, events: Sequence[Event], *, expected_seq: int,
                     patch: Patch = Patch(), posts: Sequence[Post] = (),
                     creates: Sequence[NewTask] = ()) -> Appended:
        self.appends += 1
        if self.appends == self.crash_at and not self.after_commit:
            raise Crash()
        done = await super().append(claim, events, expected_seq=expected_seq, patch=patch,
                                   posts=posts, creates=creates)
        if self.appends == self.crash_at:
            raise Crash()
        return done


class Journal:
    """A side effect we can count: every run of the tool is written down."""

    def __init__(self) -> None:
        self.runs: list[str] = []


class Effect:
    name = "Effect"
    description = "Do a side effect."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.journal.runs.append(input["key"])
        return text_output(f"did {input['key']}")


class ByProgress:
    """Answers from the request alone, so a re-sent request after a crash
    gets the same answer: call Effect a, then b, then finish."""

    family = "scripted"

    async def complete(self, request: ModelRequest, on_delta: Any | None = None) -> ModelResponse:
        done = sum(1 for m in request.messages for b in m.content if isinstance(b, ToolResult))
        if done < 2:
            key = "ab"[done]
            msg = Message(role="assistant", content=[ToolUse(id=f"c{key}", name="Effect",
                                                             input={"key": key})])
            return ModelResponse(message=msg, stop_reason="tool_use", usage=Usage(output=1))
        return ModelResponse(message=Message(role="assistant", content=[Text(text="finished")]),
                             stop_reason="end_turn", usage=Usage(output=1))


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, journal: Journal, lease: float = 30.0) -> Runtime:
    agent = Agent(name="a", model="m", tools=[Effect(journal)])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: ByProgress(),
                   env_for=lambda _s: None, lease_seconds=lease, sleep=_no_sleep)  # type: ignore[arg-type,return-value]


async def _expire(store: SqliteStore, task_id: str) -> None:
    def tx(conn: Any) -> None:
        conn.execute("UPDATE tasks SET lease_until = 0 WHERE task_id = ? AND status = 'running'",
                     (task_id,))

    await store._run(tx)


#: Appends in a clean run: the prompt overrides and plugins, InputTaken, then
#: (ModelCalled, ModelReplied, ToolStarted, ToolFinished) x2, ModelCalled,
#: ModelReplied, Waiting, and the append that hands the task back.
CLEAN_APPENDS = 14


@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("crash_at", range(1, CLEAN_APPENDS + 2))
async def test_crash_at_every_append_boundary(tmp_path: Path, crash_at: int,
                                              after_commit: bool) -> None:
    path = str(tmp_path / "t.db")
    journal = Journal()
    crashing = CrashingStore(path, crash_at, after_commit)
    tid = await Client(crashing).create("a", "go")
    with pytest.raises(Crash) if crash_at <= CLEAN_APPENDS else _nothing():
        await _runtime(crashing, journal).run_until_idle()
    await crashing.close()

    store = SqliteStore(path)
    await _expire(store, tid)
    await _runtime(store, journal).run_until_idle()

    assert journal.runs.count("a") <= 1 and journal.runs.count("b") <= 1
    events = [r.event for r in await store.read(tid)]
    started = [e.call.id for e in events if isinstance(e, ToolStarted)]
    finished = [e.result.call_id for e in events if isinstance(e, ToolFinished)]
    assert sorted(started) == sorted(set(started))
    assert set(started) <= set(finished)
    for e in events:
        if isinstance(e, ToolFinished) and e.result.text == INTERRUPTED_CALL_ERROR:
            assert e.result.call_id in started
    row = await store.get(tid)
    assert row is not None and row.status == "waiting" and row.inbox_read_seq == row.inbox_seq
    await store.close()


class _nothing:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: Any) -> bool:
        return False


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_racing_runtimes_never_double_run(tmp_path: Path, backend: str,
                                                request: pytest.FixtureRequest) -> None:
    journal = Journal()
    if backend == "postgres":
        pg = request.getfixturevalue("pg")
        stores = [pg.store() for _ in range(4)]
    else:
        path = str(tmp_path / "t.db")
        stores = [SqliteStore(path) for _ in range(4)]
    client = Client(stores[0])
    ids = [await client.create("a", f"task {i}") for i in range(12)]
    stop = asyncio.Event()
    serving = [asyncio.create_task(_runtime(s, journal, lease=0.3).serve(stop)) for s in stores]

    async def all_waiting() -> None:
        while True:
            rows = [await client.get(t) for t in ids]
            if all(r.status == "waiting" for r in rows):
                return
            await asyncio.sleep(0.05)

    await asyncio.wait_for(all_waiting(), 30)
    stop.set()
    await asyncio.gather(*serving)
    assert len(journal.runs) == 24  # a and b once per task, never twice
    for t in ids:
        events = [r.event for r in await client.events(t)]
        assert sum(isinstance(e, ToolStarted) for e in events) == 2
    for s in stores:
        await s.close()


class RenewFails(SqliteStore):
    async def renew(self, claim: Claim, lease_seconds: float) -> Claim | None:
        raise StoreUnavailable("the store is unreachable")


async def test_a_lease_that_cannot_be_renewed_is_given_up_by_the_local_clock(
        tmp_path: Path) -> None:
    """``lease_until`` is the store's clock: a store clock running ahead must
    not keep a runner going past its lease when it cannot renew it."""
    from noeta.core.runner.task import TaskRun

    store = RenewFails(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={}, provider_for=lambda _m: None,  # type: ignore[arg-type,return-value]
                 lease_seconds=0.3)
    ahead = time.time() + 3600
    run = TaskRun(rt, Claim(task_id="t", worker=rt.worker, epoch=1, lease_until=ahead))
    try:
        await asyncio.wait_for(run._keep_claim(), 2.0)
        assert run._lost
    finally:
        await store.close()


class SlowClaim(SqliteStore):
    async def claim(self, *args: Any, **kw: Any) -> Claim | None:
        await asyncio.sleep(0.2)
        return await super().claim(*args, **kw)


async def test_a_claims_local_deadline_counts_from_before_it_was_asked_for(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store starts the lease when it takes the claim; the runner's own
    clock must not start it later, when the answer came back."""
    from noeta.core.runner.task import TaskRun

    deadlines: list[float] = []

    async def run(self: TaskRun) -> None:
        deadlines.append(self._lease_deadline)

    monkeypatch.setattr(TaskRun, "run", run)
    store = SlowClaim(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={}, provider_for=lambda _m: None,  # type: ignore[arg-type,return-value]
                 lease_seconds=10)
    try:
        await store.create(NewTask(task_id="t", created=TaskCreated(agent="a")))
        before = time.monotonic()
        assert await rt.run_once()
        assert deadlines and deadlines[0] < before + 10 + 0.1
    finally:
        await store.close()


class ClaimCommitUnknown(SqliteStore):
    """The first claim commits, then its outcome is reported unknown (the
    connection dropped during ``COMMIT``)."""

    fail = 1

    async def claim(self, *args: Any, **kw: Any) -> Claim | None:
        claim = await super().claim(*args, **kw)
        if self.fail:
            self.fail -= 1
            from noeta.core.errors import CommitUnknown

            raise CommitUnknown("connection lost during COMMIT")
        return claim


async def test_a_claim_whose_commit_landed_unseen_is_adopted(tmp_path: Path) -> None:
    """The task is this worker's in the store: it runs now, not after its
    lease ran out. A task this worker already runs is not adopted twice."""
    from noeta.testing import ScriptedProvider, reply

    store = ClaimCommitUnknown(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("done")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                 provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 lease_seconds=60)
    try:
        client = Client(store)
        tid = await client.create("a", "go")
        assert await rt.run_once()
        row = await client.get(tid)
        assert row.status == "waiting" and provider.requests
        rt._running.add("busy")
        await store.create(NewTask(task_id="busy", created=TaskCreated(agent="a")))
        claim = await SqliteStore.claim(store, rt.worker, ["default"], 60)
        assert claim is not None and claim.task_id == "busy"
        store.fail = 1
        assert await rt._claim_once() is None
    finally:
        await store.close()


async def test_a_task_whose_log_cannot_be_read_ends_failed_after_a_few_claims(
        tmp_path: Path) -> None:
    """A log no worker can read or fold is not claimed forever: after a few
    claims in a row it ends failed with the reason, its parent hears it and
    its children are cancelled."""
    import sqlite3

    from noeta.core.events import Cancel, ChildEnded
    from noeta.core.runner.task import LOAD_ATTEMPTS

    path = str(tmp_path / "t.db")
    store = SqliteStore(path)
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                 provider_for=lambda _m: None, lease_seconds=0.1)  # type: ignore[arg-type,return-value]
    try:
        await store.create(NewTask(task_id="P", created=TaskCreated(agent="a"), queue="other"))
        await store.create(NewTask(task_id="C", created=TaskCreated(
            agent="a", parent_id="P", root_id="P")))
        await store.create(NewTask(task_id="G", created=TaskCreated(
            agent="a", parent_id="C", root_id="P"), queue="other"))
        conn = sqlite3.connect(path)
        conn.execute("UPDATE events SET data = x'7b' WHERE task_id = 'C'")
        conn.commit()
        conn.close()
        for attempt in range(LOAD_ATTEMPTS):
            row = await store.get("C")
            assert row is not None and row.status != "ended", attempt
            await asyncio.sleep(0.15)  # the last claim's lease runs out
            assert await rt.run_once()
        row = await store.get("C")
        assert row is not None and row.status == "ended" and row.end_status == "failed"
        [ended] = [i.body for i in await store.inbox("P") if isinstance(i.body, ChildEnded)]
        assert ended.status == "failed" and ended.error.startswith("unreadable_log")
        assert any(isinstance(i.body, Cancel) for i in await store.inbox("G"))
    finally:
        await store.close()


async def test_a_workers_count_of_unreadable_logs_is_bounded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A task whose log failed to load here once and then ended on another
    worker is never loaded here again: its count must not stay forever."""
    import sqlite3

    from noeta.core.runner import task

    monkeypatch.setattr(task, "UNREADABLE_KEEP", 2)
    path = str(tmp_path / "t.db")
    store = SqliteStore(path)
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                 provider_for=lambda _m: None, lease_seconds=30)  # type: ignore[arg-type,return-value]
    try:
        for tid in ("t1", "t2", "t3"):
            await store.create(NewTask(task_id=tid, created=TaskCreated(agent="a")))
        conn = sqlite3.connect(path)
        conn.execute("UPDATE events SET data = x'7b'")
        conn.commit()
        conn.close()
        for _ in range(3):
            assert await rt.run_once()
        assert len(rt._unreadable) == 2 and "t1" not in rt._unreadable
    finally:
        await store.close()


@pytest.mark.parametrize("data", [
    b'{"type":"FutureEvent"}',
    b'{"type":"ModelReplied","response":{"message":{"role":"assistant",'
    b'"content":[{"type":"future_block"}]},"usage":{}}}',
    b'{"type":"ExtensionRecorded","extension":"core","kind":"future_record","data":{}}',
])
async def test_a_log_written_by_a_newer_version_is_left_to_it(tmp_path: Path,
                                                             data: bytes) -> None:
    """In a rolling upgrade an older worker claims a task a newer one wrote
    an event or block it does not know: it is not its to fail. It lets the
    lease run out (a newer worker claims it) however often that happens."""
    import sqlite3

    from noeta.core.runner.task import LOAD_ATTEMPTS

    path = str(tmp_path / "t.db")
    store = SqliteStore(path)
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                 provider_for=lambda _m: None, lease_seconds=0.1)  # type: ignore[arg-type,return-value]
    try:
        await store.create(NewTask(task_id="t", created=TaskCreated(agent="a")))
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO events (task_id, seq, type, at, data) VALUES ('t', 2, 'x', 0, ?)",
                     (data,))
        conn.execute("UPDATE tasks SET last_seq = 2 WHERE task_id = 't'")
        conn.commit()
        conn.close()
        for attempt in range(LOAD_ATTEMPTS + 1):
            await asyncio.sleep(0.15)  # the last claim's lease runs out
            assert await rt.run_once(), attempt
        row = await store.get("t")
        assert row is not None and row.status == "running"
        assert "t" not in rt._unreadable
    finally:
        await store.close()


async def test_a_failure_after_the_log_loaded_is_not_an_unreadable_log(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failing the turn can itself fail (a store hiccup, a bug in an end
    hook); the log read fine, so the task is not ended ``unreadable_log``
    however often that happens."""
    from noeta.core.runner import closing
    from noeta.core.runner.task import LOAD_ATTEMPTS, TaskRun

    async def broken_step(self: TaskRun) -> bool:
        raise RuntimeError("a bug in a step")

    async def broken_fail_turn(run: TaskRun, reason: str, detail: str = "") -> bool:
        raise RuntimeError("failing the turn broke too")

    monkeypatch.setattr(TaskRun, "_step_once", broken_step)
    monkeypatch.setattr(closing, "fail_turn", broken_fail_turn)
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                 provider_for=lambda _m: None, lease_seconds=0.1)  # type: ignore[arg-type,return-value]
    try:
        await store.create(NewTask(task_id="t", created=TaskCreated(agent="a")))
        for attempt in range(LOAD_ATTEMPTS + 1):
            await asyncio.sleep(0.15)  # the last claim's lease runs out
            assert await rt.run_once(), attempt
        row = await store.get("t")
        assert row is not None and row.status != "ended"
        assert "t" not in rt._unreadable
    finally:
        await store.close()


@pytest.fixture(params=["sqlite", "postgres"])
def store_kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.mark.parametrize(("data", "newer"), [
    (b'{"kind":"steer","text":"x"}', True),
    (b'{"kind":"message"', False),
])
async def test_an_inbox_item_this_version_cannot_read(
        tmp_path: Path, request: pytest.FixtureRequest, store_kind: str, data: bytes,
        newer: bool) -> None:
    """D43 for the inbox: an item a newer version posted is left to a newer
    worker, nothing written, however often the task is claimed; a malformed
    one ends the task failed after ``LOAD_ATTEMPTS`` claims. Neither fails a
    turn on every lease."""
    from noeta.core.events import Say, TaskEnded
    from noeta.core.runner.task import LOAD_ATTEMPTS
    from noeta.core.types import user

    path = str(tmp_path / "t.db")
    pg = request.getfixturevalue("pg") if store_kind == "postgres" else None
    store: Any = pg.store() if pg is not None else SqliteStore(path)
    calls: list[ModelRequest] = []

    class Model:
        async def complete(self, req: ModelRequest, on_delta: Any = None) -> ModelResponse:
            calls.append(req)
            return ModelResponse(message=Message(role="assistant", content=[Text(text="hi")]),
                                 stop_reason="end_turn", usage=Usage(), model="m")

    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                 provider_for=lambda _m: Model(), lease_seconds=0.1)
    try:
        await store.create(NewTask(task_id="t", created=TaskCreated(agent="a"),
                                   inbox=[Say(message=user("hello"))]))
        await rt.run_until_idle()
        await store.post(Post(task_id="t", body=Say(message=user("x"))))
        if pg is not None:
            import psycopg

            from tests.conftest import PG_DSN

            with psycopg.connect(PG_DSN, autocommit=True) as pconn:
                pconn.execute(f'UPDATE "{pg.schema}".inbox SET data = %s '
                              "WHERE task_id = 't' AND seq = 2", (data,))
        else:
            import sqlite3

            conn = sqlite3.connect(path)
            conn.execute("UPDATE inbox SET data = ? WHERE task_id = 't' AND seq = 2", (data,))
            conn.commit()
            conn.close()
        before = (await store.get("t")).last_seq
        for attempt in range(LOAD_ATTEMPTS + 1):
            await asyncio.sleep(0.15)  # the last claim's lease runs out
            await rt.run_once()
            row = await store.get("t")
            if row.status == "ended":
                break
            assert row.last_seq == before, attempt  # nothing written
        assert len(calls) == 1
        events = [r.event for r in await store.read("t")]
        if newer:
            assert row.status != "ended" and "t" not in rt._unreadable
        else:
            assert row.end_status == "failed"
            assert isinstance(events[-1], TaskEnded) and "unreadable_log" in str(events[-1].error)
            assert len(events) == before + 1
    finally:
        await store.close()


async def test_a_failing_read_only_call_cancels_its_siblings(tmp_path: Path) -> None:
    """Concurrent read-only calls: when one's result cannot be written (the
    claim is lost), the others are stopped instead of running on."""
    from noeta.core.errors import Conflict
    from noeta.testing import ScriptedProvider, reply

    cancelled: list[str] = []

    class Look:
        description = "Look."
        input_schema = {"type": "object"}
        read_only = True
        clearable = False

        def __init__(self, name: str, wait: float) -> None:
            self.name, self.wait = name, wait

        async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
            try:
                await asyncio.sleep(self.wait)
            except asyncio.CancelledError:
                cancelled.append(self.name)
                raise
            return text_output("seen")

    class LosesFast(SqliteStore):
        async def append(self, claim: Claim, events: Sequence[Event], **kw: Any) -> Appended:
            if any(isinstance(e, ToolFinished) and e.result.call_id == "fast" for e in events):
                raise Conflict("the claim was lost")
            return await super().append(claim, events, **kw)

    store = LosesFast(str(tmp_path / "t.db"))
    provider = ScriptedProvider([ModelResponse(message=Message(role="assistant", content=[
        ToolUse(id="fast", name="Fast", input={}), ToolUse(id="slow", name="Slow", input={})]),
        stop_reason="tool_use", usage=Usage(output=1)), reply("done")])
    agent = Agent(name="a", model="m", tools=[Look("Fast", 0), Look("Slow", 30)])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    try:
        await Client(store).create("a", "go")
        await asyncio.wait_for(rt.run_once(), 5)
        assert cancelled == ["Slow"]
    finally:
        await store.close()
