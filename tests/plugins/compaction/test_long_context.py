"""Claude Code's further ways of staying in the window, through the real
runner: a summary written in the background ahead of the threshold and
swapped in there, old tool results cleared once the cache is cold, and a
compaction while idle before the one-hour cache expires — each across a
restart mid-way, and each request rebuilt from the log byte for byte."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import (
    Compacted,
    Event,
    ExtensionRecorded,
    External,
    InputTaken,
    TimerFired,
)
from noeta.core.provider import OnDelta
from noeta.core.store import Appended, Claim, NewTask, Patch, Post
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolResult, ToolUse, Usage
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction import extension as compaction_ext
from noeta.plugins.compaction.clearing import PLACEHOLDER
from noeta.plugins.compaction.extension import (
    IDLE_NOTICE,
    IDLE_TIMER,
    command_text,
)
from noeta.plugins.compaction.summary import KEPT_NOTE, SUMMARIZE_PROMPT, SUMMARY_PREAMBLE

from ._harness import FORMATTED, Model, Probe, is_summarize, note_reply, rebuilt, runtime

# Unknown model "m": a 128K window and 16,384 output tokens leave an
# effective 111,616: the summary is written ahead from 89,293 tokens, the
# threshold is 98,616.
AHEAD = 90_000
OVER = 99_000


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


_n = iter(range(1, 1_000_000))


def call_at(tokens: int, name: str = "Probe") -> Callable[[ModelRequest], ModelResponse]:
    """A tool call whose reply reports a context of ``tokens``."""
    def step(request: ModelRequest) -> ModelResponse:
        use = ToolUse(id=f"k{next(_n)}", name=name, input={})
        return ModelResponse(message=Message(role="assistant", content=[use]),
                             stop_reason="tool_use", usage=Usage(uncached=tokens, output=5))
    return step


def answer_at(tokens: int, text: str = "done") -> Callable[[ModelRequest], ModelResponse]:
    def step(request: ModelRequest) -> ModelResponse:
        return ModelResponse(message=Message(role="assistant", content=[Text(text=text)]),
                             stop_reason="end_turn", usage=Usage(uncached=tokens, output=5))
    return step


class AheadModel(Model):
    """``Model``, where a summary step may be a coroutine function (one that
    waits), and every main call first waits for the summaries being written
    ahead (``settle``), so their results land at the next step."""

    def __init__(self, main: Sequence[Any] = (), summaries: Sequence[Any] = (), *,
                 settle: Compaction | None = None) -> None:
        super().__init__(main, summaries)
        self.settle = settle

    async def complete(self, request: ModelRequest,
                       on_delta: OnDelta | None = None) -> ModelResponse:
        if is_summarize(request) and self.summary_steps \
                and asyncio.iscoroutinefunction(self.summary_steps[0]):
            self.summarize_requests.append(request)
            result: ModelResponse = await self.summary_steps.pop(0)(request)
            return result
        if not is_summarize(request) and self.settle is not None:
            await settled(self.settle)
        return await super().complete(request, on_delta)


async def settled(ext: Compaction) -> None:
    await asyncio.gather(*(job for job, _ in list(ext._jobs.values())),
                         return_exceptions=True)


def agent(ext: Compaction, tools: Sequence[Any] = ()) -> Agent:
    return Agent(name="a", model="m", tools=[Probe(), *tools], extensions=(ext,))


async def records(client: Client, tid: str, kind: str) -> list[Any]:
    return [r.event.data for r in await client.events(tid)
            if isinstance(r.event, ExtensionRecorded) and r.event.kind == kind]


async def compactions(client: Client, tid: str) -> list[Compacted]:
    return [r.event for r in await client.events(tid) if isinstance(r.event, Compacted)]


async def assert_rebuilds(store: SqliteStore, a: Agent, tid: str) -> None:
    pairs = await rebuilt(store, a, tid)
    assert pairs and all(live == again for live, again in pairs), pairs


async def start(store: SqliteStore, a: Agent, model: Model, goal: str = "go",
                config: dict[str, Any] | None = None) -> tuple[Client, str]:
    client = Client(store)
    tid = await client.create(a.name, goal, config=config)
    await runtime(store, a, model).run_until_idle()
    return client, tid


# ------------------------------------------------------------ written ahead


async def test_a_summary_written_ahead_is_swapped_in_at_the_threshold(
        store: SqliteStore) -> None:
    ext = Compaction()
    a = agent(ext)
    model = AheadModel([call_at(10_000), call_at(AHEAD), call_at(93_000), call_at(96_000),
                        call_at(OVER), answer_at(20_000)], settle=ext)
    client, tid = await start(store, a, model)
    # One summarize call, made in the background from 80% of the window:
    # of what came before the newest reply then.
    (ahead,) = model.summarize_requests
    assert [m.role for m in ahead.messages] == ["user", "assistant", "user"]
    assert ahead.messages[-1].content[-1].text.startswith(SUMMARIZE_PROMPT)  # type: ignore[union-attr]
    assert ahead.tools == model.requests[0].tools
    (compacted,) = await compactions(client, tid)
    assert FORMATTED in compacted.summary and KEPT_NOTE in compacted.summary
    (done,) = await records(client, tid, "compacted")
    assert done["precomputed"] is True and done["reason"] == "auto"
    # The request after it: the summary, then every round since, verbatim.
    after = model.requests[-1]
    assert after.messages[0].text == compacted.summary
    assert [m.role for m in after.messages[1:]] == ["assistant", "user"] * 4
    # The background call's cost is the task's, recorded when it was taken.
    spent = [d for d in await records(client, tid, "spent") if d["purpose"] == "compaction"]
    assert len(spent) == 1
    await assert_rebuilds(store, a, tid)


async def test_with_precompute_off_the_threshold_summarizes_in_the_step(
        store: SqliteStore) -> None:
    ext = Compaction(auto_compact_precompute=False)
    a = agent(ext)
    model = AheadModel([call_at(10_000), call_at(AHEAD), call_at(93_000), call_at(OVER),
                        answer_at(20_000)], settle=ext)
    client, tid = await start(store, a, model)
    assert len(model.summarize_requests) == 1
    assert not await records(client, tid, "precomputed")
    (done,) = await records(client, tid, "compacted")
    assert done["precomputed"] is False
    # Today's cut: everything before the newest reply.
    assert len(model.summarize_requests[0].messages) == 7
    await assert_rebuilds(store, a, tid)


async def test_a_summary_written_ahead_survives_a_restart(store: SqliteStore) -> None:
    ext = Compaction()
    a = agent(ext)
    model = AheadModel([call_at(10_000), call_at(AHEAD), answer_at(93_000)], settle=ext)
    client, tid = await start(store, a, model)
    assert len(model.summarize_requests) == 1 and await records(client, tid, "precomputed")
    # Another process picks the task up: the summary is in the log.
    ext2 = Compaction()
    a2 = agent(ext2)
    model2 = AheadModel([call_at(OVER), answer_at(20_000)], settle=ext2)
    await client.send(tid, "more")
    await runtime(store, a2, model2).run_until_idle()
    assert model2.summarize_requests == []
    (done,) = await records(client, tid, "compacted")
    assert done["precomputed"] is True
    assert model2.requests[-1].messages[0].text.startswith(SUMMARY_PREAMBLE)
    await assert_rebuilds(store, a2, tid)


async def test_a_summary_lost_with_its_process_is_written_again(store: SqliteStore) -> None:
    never = asyncio.Event()

    async def hang(_request: ModelRequest) -> ModelResponse:
        await never.wait()
        raise AssertionError("unreachable")

    ext = Compaction()
    a = agent(ext)
    model = AheadModel([call_at(10_000), call_at(AHEAD), answer_at(93_000)], [hang])
    client, tid = await start(store, a, model)
    assert len(model.summarize_requests) == 1
    for job, _ in ext._jobs.values():  # the process dies with its call
        job.cancel()
    await asyncio.sleep(0)
    assert not await records(client, tid, "precomputed")
    ext2 = Compaction()
    a2 = agent(ext2)
    model2 = AheadModel([call_at(95_000), call_at(OVER), answer_at(20_000)], settle=ext2)
    await client.send(tid, "more")
    await runtime(store, a2, model2).run_until_idle()
    assert len(model2.summarize_requests) == 1
    (done,) = await records(client, tid, "compacted")
    assert done["precomputed"] is True
    await assert_rebuilds(store, a2, tid)


@pytest.mark.parametrize("fails", [False, True])
async def test_a_summary_that_no_longer_fits_is_dropped(store: SqliteStore,
                                                        fails: bool) -> None:
    """Written ahead, it lands after a ``/compact``: the conversation it
    summarizes is gone, so only its cost is recorded (neither the summary
    nor a failure), and the threshold summarizes as it always did."""
    release = asyncio.Event()

    async def late(_request: ModelRequest) -> ModelResponse:
        await release.wait()
        if fails:
            raise RuntimeError("the provider went away")
        return note_reply()

    ext = Compaction()
    a = agent(ext)
    model = AheadModel([call_at(10_000), call_at(AHEAD), answer_at(93_000),
                        call_at(OVER), answer_at(20_000)], [late])
    client, tid = await start(store, a, model)
    await client.send(tid, command_text(""))
    await runtime(store, a, model).run_until_idle()
    spent = len(await records(client, tid, "spent"))
    release.set()
    await settled(ext)
    await runtime(store, a, model).run_until_idle()  # the late result is taken
    assert not await records(client, tid, "precomputed")
    assert not await records(client, tid, "precompute_failed")
    assert len(await records(client, tid, "spent")) == spent + (0 if fails else 1)
    assert not ext._jobs
    await client.send(tid, "more")
    await runtime(store, a, model).run_until_idle()
    manual, auto = await records(client, tid, "compacted")
    assert manual["reason"] == "manual" and auto["reason"] == "auto"
    assert auto["precomputed"] is False
    assert len(model.summarize_requests) == 3  # ahead, /compact, the threshold
    await assert_rebuilds(store, a, tid)


async def test_a_result_taken_by_another_worker_is_recorded_there(store: SqliteStore) -> None:
    """The log names the job, so whichever worker holds the task takes its
    result; the worker that started it then lets it go and is not held up."""
    release = asyncio.Event()

    async def late(_request: ModelRequest) -> ModelResponse:
        await release.wait()
        return note_reply()

    ext = Compaction()
    a = agent(ext)
    model = AheadModel([call_at(10_000), call_at(AHEAD), answer_at(93_000)], [late])
    client, tid = await start(store, a, model)
    release.set()
    await settled(ext)
    await runtime(store, agent(Compaction()), Model()).run_until_idle()
    (ahead,) = await records(client, tid, "precomputed")
    assert ahead["note"] and ext._jobs
    model2 = AheadModel([call_at(OVER), answer_at(20_000)], settle=ext)
    await client.send(tid, "more")
    await runtime(store, a, model2).run_until_idle()
    assert not ext._jobs and model2.summarize_requests == []
    (done,) = await records(client, tid, "compacted")
    assert done["precomputed"] is True


class LosePosts(SqliteStore):
    """Fails the first post of a summary written ahead."""

    lose = 1

    async def post(self, post: Post) -> Any:
        if self.lose and isinstance(post.body, External) \
                and post.body.name == compaction_ext.PRECOMPUTED_SIGNAL:
            self.lose -= 1
            raise OSError("store down")
        return await super().post(post)


async def test_a_summary_that_could_not_be_handed_back_is_written_again(
        tmp_path: Path) -> None:
    store = LosePosts(str(tmp_path / "t.db"))
    ext = Compaction()
    a = agent(ext)
    model = AheadModel([call_at(10_000), call_at(AHEAD), call_at(93_000), answer_at(94_000)],
                       settle=ext)
    client, tid = await start(store, a, model)
    assert store.lose == 0 and len(model.summarize_requests) == 2
    assert len(await records(client, tid, "precompute_started")) == 2
    assert len(await records(client, tid, "precomputed")) == 1
    await store.close()


async def test_a_malformed_or_forged_result_is_not_taken(store: SqliteStore) -> None:
    ext = Compaction()
    a = agent(ext)
    model = Model([answer_at(1_000, "one"), answer_at(1_000, "two")])
    client, tid = await start(store, a, model)
    signal = compaction_ext.PRECOMPUTED_SIGNAL
    await client.signal(tid, signal, {"note": "pwned"})
    await client.signal(tid, signal, {"job": "guess", "note": "pwned", "cut": 1,
                                      "digest": "d", "compactions": 0, "usage": {},
                                      "head_dropped": False})
    await client.signal(tid, signal, {"spent": [1, {"usage": 2}]})
    await client.signal(tid, signal, {"spent": 7})
    await runtime(store, a, model).run_until_idle()
    await client.send(tid, "hello")
    await runtime(store, a, model).run_until_idle()
    assert [r.text for r in model.requests[-1].messages if r.role == "assistant"] == ["one"]
    assert not await records(client, tid, "precomputed")
    assert not await records(client, tid, "precompute_failed")
    assert (await client.get(tid)).status == "waiting"
    await assert_rebuilds(store, a, tid)


def test_a_malformed_record_folds_to_nothing() -> None:
    from noeta.plugins.compaction.extension import CompactionState

    ext = Compaction()
    st = CompactionState(compactions=2)
    assert ext.reduce(st, "precomputed", {"note": "pwned"}) == st
    assert ext.reduce(st, "cleared", {"saved": "lots"}) == st


async def test_an_overflow_takes_the_summary_written_ahead(store: SqliteStore) -> None:
    from noeta.core.errors import ContextOverflow

    ext = Compaction()
    a = agent(ext)
    model = AheadModel([call_at(10_000), call_at(AHEAD), call_at(93_000),
                        ContextOverflow("prompt is too long"), answer_at(20_000)], settle=ext)
    client, tid = await start(store, a, model)
    assert len(model.summarize_requests) == 1
    (done,) = await records(client, tid, "compacted")
    assert done["precomputed"] is True and done["reason"] == "overflow"
    await assert_rebuilds(store, a, tid)


async def test_writing_ahead_stops_after_three_failures(store: SqliteStore) -> None:
    empty = ModelResponse(message=Message(role="assistant", content=[Text(text="")]),
                          stop_reason="end_turn", usage=Usage(uncached=7, output=1))
    ext = Compaction()
    a = agent(ext)
    model = AheadModel([call_at(10_000), *(call_at(AHEAD + 500 * i) for i in range(7)),
                        answer_at(93_000)], [empty] * 12, settle=ext)
    client, tid = await start(store, a, model)
    failed = await records(client, tid, "precompute_failed")
    assert [d["failures"] for d in failed] == [1, 2, 3]
    assert len(model.summarize_requests) == 6  # each: the call and its fallback
    spent = [d for d in await records(client, tid, "spent") if d["purpose"] == "compaction"]
    assert len(spent) == 6
    assert not await compactions(client, tid)
    await assert_rebuilds(store, a, tid)


# ------------------------------------------------------------ clearing


class Big:
    """A clearable tool with a large result (about 6,000 tokens)."""

    name = "Big"
    description = "Look at a lot."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    async def run(self, input: dict[str, Any], ctx: Any) -> Any:
        from noeta.core.tool import text_output

        return text_output(f"{ctx.call_id}:" + "y" * 24_000)


def _results(request: ModelRequest) -> dict[str, str]:
    return {b.call_id: b.text for m in request.messages for b in m.content
            if isinstance(b, ToolResult)}


def clearing_agent(offset: list[float], **kw: Any) -> tuple[Compaction, Agent]:
    kw.setdefault("auto_compact_clear_results", True)
    ext = Compaction(auto=False, clock=lambda: time.time() + offset[0], **kw)
    return ext, agent(ext, tools=[Big()])


def turn_one() -> list[Any]:
    return [call_at(1_000, "Probe"), *(call_at(1_000, "Big") for _ in range(16)),
            answer_at(1_000, "one")]


async def test_old_results_are_cleared_after_an_idle_gap(store: SqliteStore) -> None:
    offset = [0.0]
    _, a = clearing_agent(offset)

    def back(step: Callable[[ModelRequest], ModelResponse]) -> Callable[..., ModelResponse]:
        def go(request: ModelRequest) -> ModelResponse:
            offset[0] = 0.0
            return step(request)
        return go

    model = Model([*turn_one(), back(call_at(1_000, "Big")), answer_at(1_000, "two")])
    client, tid = await start(store, a, model)
    full = _results(model.requests[-1])
    assert PLACEHOLDER not in full.values()
    offset[0] = 4_000.0  # past the hour's cache
    await client.send(tid, "again")
    await runtime(store, a, model).run_until_idle()
    (cleared,) = await records(client, tid, "cleared")
    big = [i for i, t in full.items() if t.startswith(f"{i}:y")]
    # The newest 12 of the 16 clearable results stay; the Probe result too.
    assert cleared["ids"] == big[:4] and cleared["saved"] >= 20_000
    for request in model.requests[-2:]:
        results = _results(request)
        assert [i for i, t in results.items() if t == PLACEHOLDER] == big[:4]
        assert all(results[i] == full[i] for i in full if i not in big[:4])
    assert not await compactions(client, tid)
    await assert_rebuilds(store, a, tid)


async def test_clearing_is_off_by_default(store: SqliteStore) -> None:
    offset = [4_000.0]
    _, a = clearing_agent(offset, auto_compact_clear_results=False)
    model = Model([*turn_one(), answer_at(1_000, "two")])
    client, tid = await start(store, a, model)
    await client.send(tid, "again")
    await runtime(store, a, model).run_until_idle()
    assert not await records(client, tid, "cleared")
    assert PLACEHOLDER not in _results(model.requests[-1]).values()


class Crash(BaseException):
    """Stands in for the process dying."""


class CrashAfter(SqliteStore):
    """Dies right after committing the first append ``armed`` matches."""

    armed: Callable[[Sequence[Event]], bool] | None = None

    async def append(self, claim: Claim, events: Sequence[Event], *, expected_seq: int,
                     patch: Patch = Patch(), posts: Sequence[Post] = (),
                     creates: Sequence[NewTask] = ()) -> Appended:
        done = await super().append(claim, events, expected_seq=expected_seq, patch=patch,
                                    posts=posts, creates=creates)
        if self.armed is not None and self.armed(events):
            self.armed = None
            raise Crash()
        return done


async def recover(path: str, crashing: CrashAfter, tid: str,
                  make: Callable[[SqliteStore], Any],
                  crash: Callable[[SqliteStore], Any] | None = None) -> SqliteStore:
    """Run until the armed crash (with ``crash``'s runtime, else ``make``'s),
    then pick the task up in a new process."""
    with pytest.raises(Crash):
        await (crash or make)(crashing).run_until_idle()
    await crashing.close()
    store = SqliteStore(path)

    def tx(conn: Any) -> None:
        conn.execute("UPDATE tasks SET lease_until = 0 WHERE task_id = ?", (tid,))

    await store._run(tx)
    await make(store).run_until_idle()
    return store


def _recorded(kind: str) -> Callable[[Sequence[Event]], bool]:
    return lambda events: any(isinstance(e, ExtensionRecorded) and e.kind == kind
                              for e in events)


async def test_a_clearing_survives_a_crash_before_its_request(tmp_path: Path) -> None:
    path = str(tmp_path / "t.db")
    crashing = CrashAfter(path)
    offset = [0.0]
    _, a = clearing_agent(offset)
    model = Model([*turn_one(), answer_at(1_000, "two")])
    client = Client(crashing)
    tid = await client.create(a.name, "go")
    await runtime(crashing, a, model).run_until_idle()
    sent = len(model.requests)
    offset[0] = 4_000.0
    await client.send(tid, "again")
    crashing.armed = _recorded("cleared")
    # The new process's clock shows no gap: the recorded clearing still holds.
    _, a2 = clearing_agent([0.0])
    store = await recover(path, crashing, tid, lambda s: runtime(s, a2, model),
                          crash=lambda s: runtime(s, a, model))
    assert len(model.requests) == sent + 1
    (cleared,) = await records(Client(store), tid, "cleared")
    results = _results(model.requests[-1])
    assert [i for i, t in results.items() if t == PLACEHOLDER] == cleared["ids"]
    await assert_rebuilds(store, a2, tid)
    await store.close()


def auto_clearing_agent(offset: list[float]) -> Agent:
    ext = Compaction(auto_compact_clear_results=True, auto_compact_precompute=False,
                     clock=lambda: time.time() + offset[0])
    return agent(ext, tools=[Big()])


async def test_a_clearing_that_brings_the_context_under_the_threshold_does_not_compact(
        store: SqliteStore) -> None:
    offset = [0.0]
    a = auto_clearing_agent(offset)
    model = Model([*turn_one()[:-1], answer_at(OVER, "one"), answer_at(1_000, "two")])
    client, tid = await start(store, a, model)
    offset[0] = 4_000.0
    await client.send(tid, "again")
    await runtime(store, a, model).run_until_idle()
    (cleared,) = await records(client, tid, "cleared")
    assert OVER - cleared["saved"] < 98_616  # under the threshold once cleared
    assert not await compactions(client, tid) and not model.summarize_requests
    await assert_rebuilds(store, a, tid)


async def test_a_compaction_in_the_clearing_step_summarizes_the_real_results(
        store: SqliteStore) -> None:
    offset = [0.0]
    a = auto_clearing_agent(offset)
    model = Model([*turn_one()[:-1], answer_at(130_000, "one"), answer_at(1_000, "two")])
    client, tid = await start(store, a, model)
    full = _results(model.requests[-1])
    offset[0] = 4_000.0
    await client.send(tid, "again")
    await runtime(store, a, model).run_until_idle()
    (cleared,) = await records(client, tid, "cleared")
    assert len(await compactions(client, tid)) == 1
    (summ,) = model.summarize_requests
    results = _results(summ)
    assert PLACEHOLDER not in results.values()
    assert all(results[i] == full[i] for i in cleared["ids"])
    await assert_rebuilds(store, a, tid)


def test_a_clearing_counts_off_the_usage_it_saved() -> None:
    from noeta.core.loop.state import TaskState
    from noeta.core.types import user
    from noeta.plugins.compaction.extension import CompactionState

    reply = Message(role="assistant", content=[Text(text="ok")])
    messages = [user("go"), reply, user("more")]
    request = ModelRequest(model="m", messages=messages)
    state = TaskState(messages=messages, message_seqs=[1, 5, 6],
                      last_usage=Usage(uncached=50_000))
    ext = Compaction()
    before = ext._tokens(state, request, 3)[0]
    state.ext["compaction"] = CompactionState(cleared=("x",), cleared_saved=30_000,
                                              cleared_reply=5)
    assert ext._tokens(state, request, 3)[0] == before - 30_000
    state.ext["compaction"] = CompactionState(cleared=("x",), cleared_saved=30_000,
                                              cleared_reply=4)
    assert ext._tokens(state, request, 3)[0] == before  # a newer reply measured it


# ------------------------------------------------------------ idle


BIG = 250_000


def idle_agent() -> tuple[Compaction, Agent]:
    ext = Compaction(auto_compact_idle=True, auto_compact_precompute=False)
    return ext, agent(ext)


def one_hour_cache(monkeypatch: pytest.MonkeyPatch, life: float) -> None:
    """The requests use the one-hour cache, which here lives ``life``
    seconds (the timer is due at 90% of it)."""
    monkeypatch.setattr(compaction_ext, "cache_life", lambda _state: life)


@pytest.fixture
def due_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one-hour cache, living no time: the idle timer is due at once."""
    one_hour_cache(monkeypatch, 0)


@pytest.mark.usefixtures("due_now")
async def test_an_idle_task_is_compacted_before_its_cache_expires(store: SqliteStore) -> None:
    _, a = idle_agent()
    model = Model([call_at(10_000), answer_at(BIG), answer_at(20_000, "two")])
    client, tid = await start(store, a, model)
    (compacted,) = await compactions(client, tid)
    (done,) = await records(client, tid, "compacted")
    assert done["reason"] == "idle"
    assert IDLE_NOTICE in [d["text"] for d in await records(client, tid, "notice")]
    # The summarize call shares the task's prompt prefix.
    (summ,) = model.summarize_requests
    assert summ.system == model.requests[-1].system and summ.tools == model.requests[-1].tools
    # The next prompt is answered after the summary.
    await client.send(tid, "more")
    await runtime(store, a, model).run_until_idle()
    assert model.requests[-1].messages[0].text == compacted.summary
    await assert_rebuilds(store, a, tid)


async def test_idle_compaction_needs_the_one_hour_cache_and_a_large_context(
        store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _, a = idle_agent()
    model = Model([call_at(10_000), answer_at(BIG)])
    client, tid = await start(store, a, model)
    assert not await records(client, tid, "idle_armed")
    one_hour_cache(monkeypatch, 0)
    model = Model([call_at(10_000), answer_at(50_000)])
    client, tid = await start(store, a, model)
    assert not await records(client, tid, "idle_armed") and not await compactions(client, tid)


async def test_a_late_idle_firing_is_dropped(store: SqliteStore,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    one_hour_cache(monkeypatch, -1_000)
    _, a = idle_agent()
    model = Model([call_at(10_000), answer_at(BIG)])
    client, tid = await start(store, a, model)
    assert [d["outcome"] for d in await records(client, tid, "idle_done")] == ["lapsed"]
    assert not await compactions(client, tid) and not model.summarize_requests


async def test_idle_compaction_is_off_by_default(store: SqliteStore,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    one_hour_cache(monkeypatch, 0)
    a = agent(Compaction())
    model = Model([call_at(10_000), answer_at(BIG)])
    client, tid = await start(store, a, model)
    assert not await records(client, tid, "idle_armed") and not await compactions(client, tid)


async def test_a_compaction_takes_the_idle_timer_back(store: SqliteStore,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    one_hour_cache(monkeypatch, 10_000)
    _, a = idle_agent()
    model = Model([call_at(10_000), answer_at(BIG), answer_at(20_000, "two")])
    client, tid = await start(store, a, model)
    assert await records(client, tid, "idle_armed")
    await client.send(tid, "more")  # its step compacts at the threshold
    await runtime(store, a, model).run_until_idle()
    assert len(await compactions(client, tid)) == 1
    idle = [r.event.kind for r in await client.events(tid)
            if isinstance(r.event, ExtensionRecorded) and r.event.kind in ("timer", "timer_cleared")
            and r.event.data.get("name") == IDLE_TIMER]
    assert idle == ["timer", "timer_cleared"]


async def test_an_armed_timer_does_nothing_once_idle_compaction_is_off(
        store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    one_hour_cache(monkeypatch, 1.0)  # due 0.9 s after the newest call
    _, a = idle_agent()
    model = Model([call_at(10_000), answer_at(BIG)])
    client, tid = await start(store, a, model)
    assert await records(client, tid, "idle_armed")
    await asyncio.sleep(1.0)
    off = agent(Compaction(auto_compact_precompute=False))
    await runtime(store, off, model).run_until_idle()
    assert [d["outcome"] for d in await records(client, tid, "idle_done")] == ["idle_off"]
    assert not await compactions(client, tid) and not model.summarize_requests


@pytest.mark.usefixtures("due_now")
async def test_an_idle_firing_cut_short_by_a_crash_is_finished(tmp_path: Path) -> None:
    path = str(tmp_path / "t.db")
    crashing = CrashAfter(path)
    crashing.armed = lambda events: any(
        isinstance(e, InputTaken) and any(isinstance(i.body, TimerFired)
                                          and i.body.name == IDLE_TIMER for i in e.items)
        for e in events)
    _, a = idle_agent()
    model = Model([call_at(10_000), answer_at(BIG)])
    tid = await Client(crashing).create(a.name, "go")
    store = await recover(path, crashing, tid, lambda s: runtime(s, a, model))
    client = Client(store)
    (done,) = await records(client, tid, "compacted")
    assert done["reason"] == "idle" and len(model.summarize_requests) == 1
    assert len(await compactions(client, tid)) == 1
    await assert_rebuilds(store, a, tid)
    await store.close()



def test_only_the_one_hour_cache_counts() -> None:
    from noeta.core.loop.state import TaskState

    assert compaction_ext.cache_life(TaskState()) is None
    assert compaction_ext.cache_life(TaskState(cache_ttl=300)) is None
    assert compaction_ext.cache_life(TaskState(cache_ttl=3600)) == 3600
