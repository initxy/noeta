"""Compaction through the real runner: when it fires and what it keeps, the
summarize call, the message layout after it, ``/compact``, failures, and —
for every model call — the request a restarted runner rebuilds from the log
is the one that was sent."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import ContextOverflow, Transient
from noeta.core.events import Compacted, ExtensionRecorded, Waiting
from noeta.core.types import Message, ModelRequest, ModelResponse, ToolUse, Usage
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.extension import THRASHING_REASON, command_text
from noeta.plugins.compaction.summary import (
    CONTINUE_NOTE,
    HEAD_DROPPED_NOTE,
    KEPT_NOTE,
    SUMMARIZE_PROMPT,
    SUMMARY_PREAMBLE,
)
from noeta.plugins.fs import FileTools
from noeta.plugins.fs.read import ReadTool
from noeta.plugins.instructions import Instructions

from ._harness import FORMATTED, Model, Probe, answer, call, note_reply, rebuilt, run, runtime

#: Unknown model "m": 128K window, 16,384 output. A 34K cap leaves an
#: effective 17,616 and a threshold of 4,616 tokens: about eight probes.
SMALL = 34_000


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def agent(*, tools: Any = None, **kw: Any) -> Agent:
    kw.setdefault("auto_compact_window", SMALL)
    exts = kw.pop("extensions", ())
    return Agent(name="a", model="m", tools=tools if tools is not None else [Probe()],
                 extensions=(*exts, Compaction(**kw)))


def big_call(name: str = "Probe") -> Callable[[ModelRequest], ModelResponse]:
    """A tool call whose reply reports a context already over the threshold."""
    def step(request: ModelRequest) -> ModelResponse:
        use = ToolUse(id=f"b{len(request.messages)}x{id(request)}", name=name, input={})
        return ModelResponse(message=Message(role="assistant", content=[use]),
                             stop_reason="tool_use", usage=Usage(uncached=10_000, output=5))
    return step


async def events(client: Client, tid: str) -> list[Any]:
    return [r.event for r in await client.events(tid)]


async def compactions(client: Client, tid: str) -> list[Compacted]:
    return [e for e in await events(client, tid) if isinstance(e, Compacted)]


async def assert_rebuilds(store: SqliteStore, a: Agent, tid: str) -> None:
    pairs = await rebuilt(store, a, tid)
    assert pairs and all(live == again for live, again in pairs), pairs


def summary_text(request: ModelRequest) -> str:
    return next(m.text for m in request.messages if m.text.startswith(SUMMARY_PREAMBLE))


async def test_auto_compaction_keeps_the_tipping_results(store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(12)), answer("done")])
    client, tid = await run(store, a, model)
    first, *_ = await compactions(client, tid)
    assert first.summary.startswith(f"{SUMMARY_PREAMBLE}\n\n<system-reminder>\n"
                                    "<tools-used-before-summary>Probe</tools-used-before-summary>")
    assert f"{FORMATTED}\n{CONTINUE_NOTE}\n\n{KEPT_NOTE}" in first.summary
    # The summarize call is the main request with the instruction appended
    # to its last user message (the newest call and results left out).
    summ = model.summarize_requests[0]
    assert summ.tools == model.requests[0].tools and summ.tool_choice is None
    assert summ.max_tokens == model.requests[0].max_tokens
    last = summ.messages[-1]
    assert last.role == "user" and last.content[-1].text.startswith(SUMMARIZE_PROMPT)  # type: ignore[union-attr]
    # The request after it: the summary, then the kept reply and results.
    after = next(r for r in model.requests if r.messages[0].text.startswith(SUMMARY_PREAMBLE))
    assert after.messages[1].role == "assistant" and after.messages[1].tool_uses
    assert summ.messages[-2].role == "assistant"
    assert after.messages[1].tool_uses[0].id not in {
        u.id for m in summ.messages for u in m.tool_uses}
    recorded = [e for e in await events(client, tid)
                if isinstance(e, ExtensionRecorded) and e.kind == "compacted"]
    assert recorded[0].data["reason"] == "auto"
    await assert_rebuilds(store, a, tid)


async def test_over_before_the_results_summarizes_everything(store: SqliteStore) -> None:
    a = agent()
    model = Model([call(), call(), big_call(), big_call(), answer("done")])
    client, tid = await run(store, a, model)
    first, *_ = await compactions(client, tid)
    assert KEPT_NOTE not in first.summary
    after = next(r for r in model.requests if r.messages[0].text.startswith(SUMMARY_PREAMBLE))
    assert all(m.role == "user" for m in after.messages)
    await assert_rebuilds(store, a, tid)


async def test_the_request_after_a_compaction_is_sent_as_it_is(store: SqliteStore) -> None:
    a = agent()
    model = Model([big_call(), big_call(), big_call(), answer("done")])
    client, tid = await run(store, a, model)
    # Every reply reports 10K tokens: each request after the first is
    # compacted, but never twice in a row without a request in between.
    assert len(await compactions(client, tid)) == 3
    assert len(model.requests) == 4
    await assert_rebuilds(store, a, tid)


async def test_rapid_refills_stop_auto_compaction(store: SqliteStore) -> None:
    a = agent()
    model = Model([big_call() for _ in range(8)])
    client, tid = await run(store, a, model)
    assert len(await compactions(client, tid)) == 4
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting)
    assert last.detail["turn_failed"].startswith(f"{THRASHING_REASON}: ")


async def test_overflow_compacts_and_retries(store: SqliteStore) -> None:
    a = agent(auto_compact_window=None)
    model = Model([call(), call(), ContextOverflow("prompt is too long"), answer("done")])
    client, tid = await run(store, a, model)
    evs = await events(client, tid)
    assert [e for e in evs if isinstance(e, Compacted)]
    assert model.requests[-1].messages[0].text.startswith(SUMMARY_PREAMBLE)
    assert evs[-1].detail == {"answer": "done"}
    recorded = [e for e in evs if isinstance(e, ExtensionRecorded) and e.kind == "compacted"]
    assert recorded[0].data["reason"] == "overflow"
    await assert_rebuilds(store, a, tid)


async def test_an_empty_reply_fails_the_turn(store: SqliteStore) -> None:
    a = agent()
    empty = ModelResponse(
        message=Message(role="assistant", content=[ToolUse(id="x", name="Probe", input={})]),
        stop_reason="tool_use", usage=Usage(output=3))
    model = Model([*(call() for _ in range(12)), answer("done")], summaries=[empty])
    client, tid = await run(store, a, model)
    last = (await events(client, tid))[-1]
    assert last.detail["turn_failed"].startswith(
        "compaction_summary_failed: empty summarize response (")
    assert not await compactions(client, tid)
    assert len(model.summarize_requests) == 1  # no tool-free retry


async def test_a_failed_summarize_call_is_retried_with_a_smaller_ceiling(
        store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(12)), answer("done")],
                  summaries=[Transient("stream cut"), note_reply()])
    client, tid = await run(store, a, model)
    first, second = model.summarize_requests[:2]
    assert first.max_tokens == model.requests[0].max_tokens
    assert second.max_tokens == min(64_000, first.max_tokens or 64_000)
    assert await compactions(client, tid)


async def test_an_overflowing_summarize_call_drops_the_oldest_rounds(
        store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(12)), answer("done")],
                  summaries=[ContextOverflow("too long"), note_reply()])
    client, tid = await run(store, a, model)
    first, second = model.summarize_requests[:2]
    assert first.messages[0].text == "go"
    assert all(m.text != "go" for m in second.messages)
    (compacted, *_) = await compactions(client, tid)
    assert HEAD_DROPPED_NOTE in compacted.summary


async def test_a_later_summary_carries_the_earlier_one(store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(26)), answer("done")])
    client, tid = await run(store, a, model)
    done = await compactions(client, tid)
    assert len(done) >= 2
    second = model.summarize_requests[1]
    assert second.messages[0].origin is None
    assert second.messages[0].text == done[0].summary
    await assert_rebuilds(store, a, tid)


async def test_layout_after_a_compaction(store: SqliteStore, tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    (work / "AGENTS.md").write_text("RULE: say hi")
    target = work / "notes.txt"
    target.write_text("alpha\nbeta\n")
    home = tmp_path / "home"
    a = agent(tools=[Probe()], home=str(home),
              extensions=(Instructions(), FileTools(tools=[ReadTool()])))
    model = Model([call("Read", file_path=str(target)), *(call() for _ in range(14)),
                   answer("done")])
    client, tid = await run(store, a, model, env_for=lambda _s: LocalExecEnv(str(work)))
    (compacted, *_) = await compactions(client, tid)
    # Ahead of the summary: the project instructions, read again.
    [lead] = [m.text for m in compacted.lead]
    assert lead.startswith("Below are instructions from the codebase") and \
        lead.endswith("(project instructions, part of the repository):\n\nRULE: say hi")
    # After the kept messages: the files read before, as a Read call.
    assert compacted.after[0].text.startswith(
        f'The Read tool was called with this input: {{"file_path":"{target}"}}\nIt returned:')
    assert "alpha" in compacted.after[0].text and compacted.after[0].origin == "system"
    # The full transcript, as JSON lines, named in the summary.
    transcript = next(home.glob("projects/*/*.jsonl"))
    assert str(transcript) in compacted.summary
    first = json.loads(transcript.read_text().splitlines()[0])
    assert first["role"] == "user"
    after = next(r for r in model.requests if any(
        m.text.startswith(SUMMARY_PREAMBLE) for m in r.messages))
    texts = [m.text for m in after.messages]
    assert texts[0].startswith("Below are instructions from the codebase") and \
        texts[1].startswith(SUMMARY_PREAMBLE)
    assert texts.index(compacted.after[0].text) > 2  # after the kept reply and results
    await assert_rebuilds(store, a, tid)


async def test_compact_command(store: SqliteStore) -> None:
    a = agent(auto_compact_window=None)
    model = Model([call(), answer("done"), answer("again")])
    client, tid = await run(store, a, model)
    await client.send(tid, command_text("keep the probe ids"))
    await runtime(store, a, model).run_until_idle()
    (compacted,) = await compactions(client, tid)
    assert len(model.requests) == 2  # the command made no main-loop call
    (summ,) = model.summarize_requests
    instruction = summ.messages[-1].text
    assert "keep the probe ids" in instruction and instruction.startswith(SUMMARIZE_PROMPT)
    assert "<command-name>" not in "".join(m.text for m in summ.messages)
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "input"
    # The next message is answered after the summary and the command.
    await client.send(tid, "and now?")
    await runtime(store, a, model).run_until_idle()
    texts = [m.text for m in model.requests[-1].messages]
    assert texts[0] == compacted.summary
    assert "<command-name>/compact</command-name>" in texts[1] and texts[-1] == "and now?"
    await assert_rebuilds(store, a, tid)


async def test_compact_command_with_nothing_to_summarize(store: SqliteStore) -> None:
    a = agent()
    client = Client(store)
    tid = await client.create(a.name, command_text(""))
    model = Model([])
    await runtime(store, a, model).run_until_idle()
    assert not model.requests and not model.summarize_requests
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "input"


async def test_compaction_model_gets_its_own_output_ceiling(store: SqliteStore) -> None:
    a = agent(compaction_model="claude-haiku-4-5")
    model = Model([*(call() for _ in range(12)), answer("done")])
    await run(store, a, model)
    summ = model.summarize_requests[0]
    assert summ.model == "claude-haiku-4-5" and summ.max_tokens == 64_000
    assert all(r.model == "m" for r in model.requests)


def test_rapid_refill_count() -> None:
    ext = Compaction()
    st = ext.initial_state()
    for turn in (3, 3, 4, 5):
        st = ext.reduce(st, "compacted", {"turn": turn})
    assert (st.compactions, st.rapid) == (4, 3)
    st = ext.reduce(st, "compacted", {"turn": 9})
    assert st.rapid == 0
