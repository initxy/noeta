"""Compaction through the real runner: proactive and passive compaction, the
summarize call's retries and failures, clearing, the file restore,
RecallHistory, and — for every model call — the request a restarted runner
rebuilds from the log is the one that was sent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import ContextOverflow
from noeta.core.events import Compacted, ExtensionRecorded, ToolFinished, Waiting
from noeta.core.types import Message, ModelResponse, ToolResult, ToolUse, Usage
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.clearing import CLEARED_MARKER
from noeta.plugins.compaction.summary import (
    SUMMARY_FRAME,
    SUMMARY_TRUNCATION_MARKER,
    VERBATIM_HEADER,
)
from noeta.plugins.fs import FileTools
from noeta.plugins.fs.read import ReadTool

from ._harness import NOTE, Model, Probe, answer, call, note_reply, rebuilt, run

#: Unknown model "m": 128K window, 16,384 output -> a 29,384-token reserve.
#: A 34K cap leaves a 4,616-token usable window and a 1,538-token tail.
SMALL = 34_000


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def agent(*, tools: Any = None, **kw: Any) -> Agent:
    kw.setdefault("auto_compact_window", SMALL)
    kw.setdefault("clearable_tools", ())
    exts = kw.pop("extensions", ())
    return Agent(name="a", model="m", tools=tools if tools is not None else [Probe()],
                 extensions=(*exts, Compaction(**kw)))


async def events(client: Client, tid: str) -> list[Any]:
    return [r.event for r in await client.events(tid)]


async def assert_rebuilds(store: SqliteStore, a: Agent, tid: str) -> None:
    pairs = await rebuilt(store, a, tid)
    assert pairs and all(live == again for live, again in pairs), pairs


async def test_proactive_compaction_and_restart_identity(store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(12)), answer("done")])
    client, tid = await run(store, a, model)
    evs = await events(client, tid)
    compactions = [e for e in evs if isinstance(e, Compacted)]
    assert len(compactions) == 1
    assert compactions[0].summary == f"{SUMMARY_FRAME}\n\n{NOTE}"
    assert isinstance(evs[-1], Waiting) and evs[-1].detail == {"answer": "done"}
    # The request right after the compaction opens with the note and carries
    # the pointer at the collapsed range as a trailing reminder.
    after = next(r for r in model.requests if r.messages[0].text.startswith(SUMMARY_FRAME))
    assert after.messages[-1].origin == "system"
    assert after.messages[-1].text.startswith("The note at the head of this conversation")
    # The summarize call: main system + tools, the instruction last, no
    # tool_choice on the first attempt.
    (summ,) = model.summarize_requests
    assert summ.tools == model.requests[0].tools and summ.tool_choice is None
    recorded = [e for e in evs if isinstance(e, ExtensionRecorded) and e.kind == "compacted"]
    assert recorded and recorded[0].data["reason"] == "proactive"
    await assert_rebuilds(store, a, tid)


async def test_fold_twice_gives_the_same_request(store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(12)), answer("done")])
    _client, tid = await run(store, a, model)
    first = await rebuilt(store, a, tid)
    second = await rebuilt(store, a, tid)
    assert first == second


async def test_micro_valve_clears_in_batches_and_never_the_log(store: SqliteStore) -> None:
    # A large window (the relief valve stays shut) clearing from 2,000 tokens.
    a = agent(clearable_tools=("Probe",), batch_tokens=1_000, auto_compact_window=60_000,
              clear_tool_outputs_at=2_000)
    model = Model([*(call() for _ in range(12)), answer("done")])
    client, tid = await run(store, a, model)
    cleared_counts = []
    for r in model.requests:
        results = [b for m in r.messages for b in m.content if isinstance(b, ToolResult)]
        cleared = [b for b in results if b.text == CLEARED_MARKER]
        cleared_counts.append(len(cleared))
        # The newest five results are never cleared by the micro valve.
        assert all(b.text != CLEARED_MARKER for b in results[-5:])
    assert max(cleared_counts) > 0
    assert all(n % 2 == 0 for n in cleared_counts)  # two ~500-token results per batch
    assert not any(isinstance(e, Compacted) for e in await events(client, tid))
    finished = [e for e in await events(client, tid) if isinstance(e, ToolFinished)]
    assert all(CLEARED_MARKER not in e.result.text for e in finished)
    await assert_rebuilds(store, a, tid)


async def test_relief_valve_then_compaction_rebuild_byte_for_byte(store: SqliteStore) -> None:
    a = agent(clearable_tools=("Probe",))
    model = Model([*(call() for _ in range(40)), answer("done")])
    client, tid = await run(store, a, model)
    evs = await events(client, tid)
    assert [e for e in evs if isinstance(e, Compacted)]
    assert [e for e in evs if isinstance(e, ExtensionRecorded) and e.kind == "cleared"]
    assert any(b.text == CLEARED_MARKER for r in model.requests for m in r.messages
               for b in m.content if isinstance(b, ToolResult))
    await assert_rebuilds(store, a, tid)


async def test_unclearable_results_are_kept(store: SqliteStore) -> None:
    a = agent(batch_tokens=1_000)  # clearable_tools=() in agent()
    model = Model([*(call() for _ in range(6)), answer("done")])
    await run(store, a, model)
    assert not any(b.text == CLEARED_MARKER for r in model.requests for m in r.messages
                   for b in m.content if isinstance(b, ToolResult))


async def test_overflow_compacts_and_retries(store: SqliteStore) -> None:
    a = agent()
    model = Model([call(), call(), call(), call(), ContextOverflow("prompt is too long"),
                   answer("done")])
    client, tid = await run(store, a, model)
    evs = await events(client, tid)
    assert [e for e in evs if isinstance(e, Compacted)]
    assert model.requests[-1].messages[0].text.startswith(SUMMARY_FRAME)
    assert evs[-1].detail == {"answer": "done"}
    recorded = [e for e in evs if isinstance(e, ExtensionRecorded) and e.kind == "compacted"]
    assert recorded[0].data["reason"] == "overflow"
    await assert_rebuilds(store, a, tid)


async def test_no_progress_fails_the_turn(store: SqliteStore) -> None:
    # The model's whole window: the tail covers the whole history.
    a = agent(auto_compact_window=None)
    model = Model([call(), ContextOverflow("prompt is too long")])
    client, tid = await run(store, a, model)
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting)
    assert last.detail["turn_failed"] == "compaction_no_progress"


async def test_a_reply_that_is_not_a_note_fails_the_turn(store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(12)), answer("done")],
                  summaries=[note_reply("The file is long. Fetching the rest.")])
    client, tid = await run(store, a, model)
    last = (await events(client, tid))[-1]
    detail = last.detail["turn_failed"]
    assert detail.startswith("compaction_summary_failed: summarize response is not a note ("
                             "stop_reason=end_turn, output_tokens=50, text_len=")
    assert not any(isinstance(e, Compacted) for e in await events(client, tid))


async def test_a_tool_call_reply_is_retried_without_tools(store: SqliteStore) -> None:
    tool_only = ModelResponse(
        message=Message(role="assistant", content=[ToolUse(id="x", name="Probe", input={})]),
        stop_reason="tool_use", usage=Usage(output=3))
    a = agent()
    model = Model([*(call() for _ in range(12)), answer("done")],
                  summaries=[tool_only, note_reply()])
    client, tid = await run(store, a, model)
    first, second = model.summarize_requests
    assert first.tool_choice is None and second.tool_choice == "none"
    (compacted,) = [e for e in await events(client, tid) if isinstance(e, Compacted)]
    assert compacted.usage.output == 3 + 50


async def test_an_overflowing_summarize_call_drops_the_oldest_rounds(
        store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(12)), answer("done")],
                  summaries=[ContextOverflow("too long"), note_reply()])
    client, tid = await run(store, a, model)
    first, second = model.summarize_requests
    assert second.messages[0].text == SUMMARY_TRUNCATION_MARKER
    # The oldest fifth of the rounds (here: the opening goal) is gone.
    assert first.messages[0].text == "go"
    assert all(m.text != "go" for m in second.messages)
    assert [e for e in await events(client, tid) if isinstance(e, Compacted)]


async def test_safety_constraints_survive_two_compactions(store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(26)), answer("done")])
    client, tid = await run(store, a, model, goal="go\nDo not touch secrets.txt")
    compactions = [e for e in await events(client, tid) if isinstance(e, Compacted)]
    assert len(compactions) >= 2
    for c in compactions:
        assert f"{VERBATIM_HEADER}\n- Do not touch secrets.txt" in c.summary
    # Summary of summary: the second input opens with the first note, as a
    # plain user turn (so its constraints are read again).
    second = model.summarize_requests[1]
    assert second.messages[0].origin is None
    assert second.messages[0].text == compactions[0].summary
    await assert_rebuilds(store, a, tid)


async def test_recent_files_are_restored(store: SqliteStore, tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    target = work / "notes.txt"
    target.write_text("alpha\nbeta\n")
    a = agent(tools=[Probe()], extensions=(FileTools(tools=[ReadTool()]),),
              auto_compact_window=36_000)
    model = Model([call("Read", file_path=str(target)), *(call() for _ in range(14)),
                   answer("done")])
    client, tid = await run(store, a, model, env_for=lambda _s: LocalExecEnv(str(work)))
    (compacted, *_) = [e for e in await events(client, tid) if isinstance(e, Compacted)]
    texts = [m.text for m in compacted.kept]
    assert texts[0] == f'Called the Read tool with the following input: {{"file_path":"{target}"}}'
    assert texts[1].startswith("Result of calling the Read tool:\n")
    assert "alpha" in texts[1]
    assert all(m.origin == "system" for m in compacted.kept)


async def test_recall_history_pages_back_the_collapsed_range(store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(12)), call("RecallHistory", offset=0, limit=2),
                   answer("done")])
    client, tid = await run(store, a, model)
    results = [e.result for e in await events(client, tid) if isinstance(e, ToolFinished)]
    recall = results[-1]
    assert not recall.is_error
    assert recall.text.startswith("Collapsed range: ")
    assert "[0] user:\n  text: go" in recall.text


async def test_thrashing_latches_the_read_suggestion(store: SqliteStore) -> None:
    ext = Compaction(auto_compact_window=SMALL)
    st = ext.initial_state()
    for marker in (4, 6, 8, 10):
        st = ext.reduce(st, "compacted", {"marker": marker})
    assert st.thrashing
    st = ext.reduce(st, "compacted", {"marker": 40})
    assert not st.thrashing


async def test_compaction_model_gets_its_own_output_ceiling(store: SqliteStore) -> None:
    a = agent(compaction_model="claude-haiku-4-5")
    model = Model([*(call() for _ in range(12)), answer("done")])
    await run(store, a, model)
    (summ,) = model.summarize_requests
    assert summ.model == "claude-haiku-4-5" and summ.max_tokens == 64_000
    assert all(r.model == "m" for r in model.requests)
