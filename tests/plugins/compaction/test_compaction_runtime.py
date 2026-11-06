"""Compaction through the real runner: when it fires and what it keeps, the
summarize call, the message layout after it, ``/compact``, failures, and —
for every model call — the request a restarted runner rebuilds from the log
is the one that was sent."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

import msgspec
import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import records
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import ContextOverflow, Transient
from noeta.core.events import Compacted, ExtensionRecorded, ModelReplied, Waiting
from noeta.core.extension import Deny, Extension, Reminder, TaskView
from noeta.core.loop.state import fold
from noeta.core.types import (
    Message,
    ModelRequest,
    ModelResponse,
    SystemPart,
    Text,
    ToolUse,
    Usage,
)
from noeta.host.usage import TreeUsage, add_events
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.extension import command_text
from noeta.plugins.compaction.summary import (
    CONTINUE_NOTE,
    FALLBACK_SYSTEM,
    HEAD_DROPPED_NOTE,
    KEPT_NOTE,
    SUMMARIZE_PROMPT,
    SUMMARY_PREAMBLE,
)
from noeta.plugins.fs import FileTools
from noeta.plugins.fs.read import ReadTool
from noeta.plugins.instructions import Instructions
from noeta.toolkit import plugin_records as pr

from ._harness import FORMATTED, Model, Probe, answer, call, note_reply, rebuilt, run, runtime

#: Unknown model "m": 128K window, 16,384 output. A 9,232-token cap holds
#: back half of it (the two reserves shrunk to fit): a threshold of 4,616
#: tokens, about eight probes.
SMALL = 9_232


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


async def test_the_first_reply_after_a_compaction_is_summarized_with_it(
        store: SqliteStore) -> None:
    a = agent()
    model = Model([call(), big_call(), big_call(), answer("done")])
    client, tid = await run(store, a, model)
    first, second = await compactions(client, tid)
    # The first keeps the newest reply; the second comes right after the
    # first post-compaction request and summarizes everything.
    assert KEPT_NOTE in first.summary and KEPT_NOTE not in second.summary
    after = next(r for r in model.requests if r.messages[0].text == second.summary)
    assert all(m.role == "user" for m in after.messages)
    await assert_rebuilds(store, a, tid)


async def test_the_request_after_a_compaction_is_sent_as_it_is(store: SqliteStore) -> None:
    a = agent()
    model = Model([big_call(), big_call(), big_call(), answer("done")])
    client, tid = await run(store, a, model)
    # Every reply reports 10K tokens. With nothing before the first reply to
    # summarize the second request goes as it is; after that each request
    # but the one right after a compaction is compacted first.
    assert len(await compactions(client, tid)) == 2
    assert len(model.requests) == 4
    await assert_rebuilds(store, a, tid)


async def test_a_context_that_keeps_refilling_stops_after_three_rapid_refills(
        store: SqliteStore) -> None:
    """Claude Code's rapid-refill breaker: a context that is over the
    threshold again within 3 model calls of the previous compaction, 3
    times in a row, ends the turn instead of summarizing on every step."""
    a = agent()
    model = Model([call(), *(big_call() for _ in range(7)), answer("done")])
    client, tid = await run(store, a, model)
    assert len(await compactions(client, tid)) == 3
    last = (await events(client, tid))[-1]
    assert last.detail["turn_failed"].startswith("autocompact_thrashing: Automatic compaction "
                                                 "stopped")
    recorded = [e.data["rapid_refills"] for e in await events(client, tid)
                if isinstance(e, ExtensionRecorded) and e.kind == "compacted"]
    assert recorded == [0, 1, 2]
    await assert_rebuilds(store, a, tid)


async def test_a_refill_in_a_new_turn_starts_the_count_again(store: SqliteStore) -> None:
    a = agent()
    model = Model([call(), *(big_call() for _ in range(3)), answer("one"),
                   *(big_call() for _ in range(3)), answer("two")])
    client, tid = await run(store, a, model)
    await client.send(tid, "again")
    await runtime(store, a, model).run_until_idle()
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.detail == {"answer": "two"}
    assert len(await compactions(client, tid)) >= 4  # two in each turn at least


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


async def test_an_overflow_right_after_each_compaction_stops_after_three_refills(
        store: SqliteStore) -> None:
    """What a compaction re-attaches can by itself overflow the real window
    (a model whose catalog window is larger): the rapid-refill breaker ends
    that overflow -> compact -> overflow loop as it ends the automatic one."""

    class Note(Extension):
        """A note with every request: something new to summarize each time."""

        name = "note"

        async def reminders(self, view: TaskView) -> list[Reminder]:
            return [Reminder(text=f"Step {view.state.steps_in_turn}.")]

    a = agent(auto_compact_window=None, extensions=[Note()])
    model = Model([call(), call(), *(ContextOverflow("prompt is too long") for _ in range(20))])
    client, tid = await run(store, a, model)
    evs = await events(client, tid)
    assert evs[-1].detail["turn_failed"].startswith("autocompact_thrashing: ")
    recorded = [e.data for e in evs if isinstance(e, ExtensionRecorded) and e.kind == "compacted"]
    assert [d["reason"] for d in recorded] == ["overflow"] * 3
    assert [d["rapid_refills"] for d in recorded] == [0, 1, 2]
    assert len(model.requests) == 6


async def test_overflow_summarizes_the_request_as_it_was_sent(store: SqliteStore) -> None:
    import msgspec

    class Route(Extension):
        """Runs after compaction: compaction's own hook never sees this."""

        name = "route"

        async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
            return msgspec.structs.replace(request, model="routed")

    a = Agent(name="a", model="m", tools=[Probe()],
              extensions=(Compaction(auto_compact_window=None), Route()))
    model = Model([call(), ContextOverflow("prompt is too long"), answer("done")])
    client, tid = await run(store, a, model)
    assert await compactions(client, tid)
    assert [r.model for r in model.summarize_requests] == ["routed"]


async def test_an_empty_reply_takes_the_fallback_call(store: SqliteStore) -> None:
    """Claude Code's second summarize call when the first brings no
    summary: a plain summarizer system prompt, only ``Read``, no cache."""
    a = agent()
    empty = ModelResponse(
        message=Message(role="assistant", content=[ToolUse(id="x", name="Probe", input={})]),
        stop_reason="tool_use", usage=Usage(output=3))
    model = Model([*(call() for _ in range(12)), answer("done")], summaries=[empty])
    client, tid = await run(store, a, model)
    first, second = model.summarize_requests[:2]
    assert second.system == [SystemPart(text=FALLBACK_SYSTEM)]
    assert first.tools and second.tools == [] and second.cache_after == []
    assert second.messages == first.messages
    assert await compactions(client, tid)
    assert (await events(client, tid))[-1].detail == {"answer": "done"}
    await assert_rebuilds(store, a, tid)


async def test_a_failed_auto_compaction_sends_the_request_and_stops_after_three(
        store: SqliteStore) -> None:
    """Claude Code's: a failed automatic compaction does not end the turn;
    the request goes out uncompacted, and after three failures in a row
    automatic compaction is off for the task. The log holds the count, so a
    rebuilt request (another worker) is the one sent."""
    a = agent()
    empty = ModelResponse(message=Message(role="assistant", content=[Text(text="")]),
                          stop_reason="end_turn", usage=Usage(output=1))
    model = Model([*(call() for _ in range(12)), answer("done")], summaries=[empty] * 8)
    client, tid = await run(store, a, model)
    evs = await events(client, tid)
    assert evs[-1].detail == {"answer": "done"}
    assert not [e for e in evs if isinstance(e, Compacted)]
    failed = [e.data for e in evs if isinstance(e, ExtensionRecorded) and e.kind == "failed"]
    assert [d["failures"] for d in failed] == [1, 2, 3]
    assert failed[0]["detail"].startswith("empty summarize response (")
    # Each failure: the summarize call and its fallback; none after the third.
    assert len(model.summarize_requests) == 6
    assert len(model.requests) == 13
    await assert_rebuilds(store, a, tid)


async def test_every_summarize_call_counts_in_the_tasks_usage(store: SqliteStore) -> None:
    """Failed summarize calls (and their fallback) cost too: each is recorded
    in the log (``core`` / ``spent``), so the task's usage, ``/cost`` and the
    USD budget count them on any worker."""
    a = agent()
    empty = ModelResponse(message=Message(role="assistant", content=[Text(text="")]),
                          stop_reason="end_turn", usage=Usage(uncached=7, output=1))
    model = Model([*(call() for _ in range(12)), answer("done")],
                  summaries=[empty, empty, note_reply()])
    _, tid = await run(store, a, model)
    recs = await store.read(tid)
    spent = [s for s in (records.decode(r.event) for r in recs) if isinstance(s, records.Spent)]
    assert [(s.usage, s.purpose) for s in spent] == [
        (Usage(uncached=7, output=1), "compaction"), (Usage(uncached=7, output=1), "compaction"),
        (Usage(uncached=100, output=50), "compaction")]
    assert all(s.model == "m" for s in spent)
    replies = sum((r.event.response.usage for r in recs if isinstance(r.event, ModelReplied)),
                  Usage())
    state = fold(recs, {e.name: e.reduce for e in a.all_extensions()},
                 {e.name: e.initial_state() for e in a.all_extensions()})
    assert state.usage == replies + Usage(uncached=114, output=52)
    total = TreeUsage()
    add_events(total, recs)
    assert total.usage == state.usage
    await assert_rebuilds(store, a, tid)


async def test_a_failed_summarize_call_is_retried_as_it_was(store: SqliteStore) -> None:
    """Like any call, through the retry helper: the retried call is the same
    request (the fallback call comes only once the retries are spent)."""
    a = agent()
    model = Model([*(call() for _ in range(12)), answer("done")],
                  summaries=[Transient("stream cut"), note_reply()])
    client, tid = await run(store, a, model)
    first, second = model.summarize_requests[:2]
    assert first.max_tokens == model.requests[0].max_tokens
    assert second == first
    assert await compactions(client, tid)


async def test_an_overflowing_summarize_call_drops_the_oldest_rounds(
        store: SqliteStore) -> None:
    a = agent()
    model = Model([*(call() for _ in range(12)), answer("done")],
                  summaries=[ContextOverflow("too long"), ContextOverflow("too long"),
                             note_reply()])
    client, tid = await run(store, a, model)
    first, fallback, second = model.summarize_requests[:3]
    assert first.messages[0].text == "go" and fallback.messages[0].text == "go"
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
    assert second.messages[0].origin == "inline"
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
    # The files read before, as a Read call, first in the next request's
    # system turn.
    restored = f'The Read tool was called with this input: {{"file_path":"{target}"}}\nIt returned:'
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
    [file] = [m for m in after.messages if m.text.startswith(restored)]
    assert file.role == "system" and "alpha" in file.text
    assert after.messages.index(file) > 1
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
    # Claude Code's order: the kept reply, what is given again in the
    # person's turn, then the command's own message.
    assert compacted.kept[0].text == "done"
    assert "<command-name>/compact</command-name>" in compacted.kept[-1].text
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "input"
    # The next message is answered after the summary and the command.
    await client.send(tid, "and now?")
    await runtime(store, a, model).run_until_idle()
    texts = [m.text for m in model.requests[-1].messages]
    # The summary, the newest reply kept after it, the command, the prompt.
    assert texts[0] == compacted.summary and KEPT_NOTE in compacted.summary
    assert texts[1] == "done"
    assert "<command-name>/compact</command-name>" in texts[2] and texts[-1] == "and now?"
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


class _BeforeCompact(Extension):
    """Answers ``before_compact`` with ``verdict`` and remembers each ask."""

    name = "before_compact_probe"

    def __init__(self, verdict: str | Deny | None) -> None:
        self.verdict = verdict
        self.asked: list[tuple[str, str]] = []

    async def before_compact(self, view: TaskView, trigger: Literal["manual", "auto"],
                             instructions: str) -> str | Deny | None:
        self.asked.append((trigger, instructions))
        return self.verdict


async def test_an_extension_adds_summary_instructions(store: SqliteStore) -> None:
    probe = _BeforeCompact("Keep every file path.")
    a = agent(auto_compact_window=None, extensions=[probe])
    model = Model([call(), answer("done")])
    client, tid = await run(store, a, model)
    await client.send(tid, command_text("keep the probe ids"))
    await runtime(store, a, model).run_until_idle()
    assert probe.asked == [("manual", "keep the probe ids")]
    instruction = model.summarize_requests[0].messages[-1].text
    assert "keep the probe ids\n\nKeep every file path." in instruction


async def test_an_extension_can_refuse_a_compaction(store: SqliteStore) -> None:
    probe = _BeforeCompact(Deny(reason="not now"))
    a = agent(extensions=[probe])
    model = Model([*(call() for _ in range(12)), answer("done")])
    client, tid = await run(store, a, model)
    assert not await compactions(client, tid) and not model.summarize_requests
    assert probe.asked and all(t == "auto" for t, _ in probe.asked)
    await client.send(tid, command_text(""))
    await runtime(store, a, model).run_until_idle()
    assert not await compactions(client, tid)
    notices = [e.data["text"] for e in await events(client, tid)
               if isinstance(e, ExtensionRecorded) and e.kind == "notice"]
    assert notices[-1] == "Compaction skipped: not now"


@pytest.mark.parametrize("task_id", ["../../escaped", "a/../../b", "..", "/abs/x"])
async def test_a_transcript_stays_in_the_project_directory_whatever_the_task_id(
        tmp_path: Path, task_id: str) -> None:
    """A child's id carries the provider's tool-use id, which a gateway
    picks."""
    from types import SimpleNamespace

    from noeta.core.loop.state import TaskState

    work = tmp_path / "w"
    work.mkdir()
    ext = Compaction(home=str(tmp_path / "home"))
    async def read_log(_task_id: str, after: int = 0) -> list[Any]:
        return []

    view = SimpleNamespace(env=LocalExecEnv(str(work)), task_id=task_id, read_log=read_log)
    written = await ext._transcript(view, TaskState(task_id=task_id))  # type: ignore[arg-type]
    assert written is not None
    from noeta.toolkit.paths import project_slug

    project = tmp_path / "home" / "projects" / project_slug(str(work))
    assert Path(written).parent == project and Path(written).resolve().parent == project


async def test_the_plan_file_is_given_again_after_a_compaction(
        store: SqliteStore, tmp_path: Path) -> None:
    """Claude Code's ``plan_file_reference``: after a compaction the task's
    plan file, path and whole text, whenever there is one (here in plan
    mode), after the files read again and ahead of the skills."""
    import re

    from noeta.plugins.permissions import Permissions
    from noeta.plugins.plan_mode import PlanMode
    from noeta.plugins.plan_mode.plan import plan_file_text

    plans = tmp_path / "plans"
    written: list[str] = []

    def plan_then_call(request: ModelRequest) -> ModelResponse:
        path = re.search(rf"{re.escape(str(plans))}/[\w-]+\.md", "\n".join(
            m.text for m in request.messages))
        assert path is not None
        Path(path.group(0)).parent.mkdir(parents=True, exist_ok=True)
        Path(path.group(0)).write_text("1. Probe.\n2. Report.\n")
        written.append(path.group(0))
        return call()(request)

    # Plan mode off the classifier: its safeguards block names the exec
    # env's directory, which the rebuild check below runs without.
    a = agent(extensions=(Permissions(mode="default", plans_dir=str(plans), allow=["Probe"]),
                          PlanMode(plans_dir=str(plans), auto_during_plan=False)))
    model = Model([plan_then_call, *(call() for _ in range(11)), answer("done")])
    client = Client(store)
    tid = await client.create(a.name, "go", config={"permission_mode": "plan"})
    await runtime(store, a, model).run_until_idle()
    assert await compactions(client, tid)
    after = next(r for r in model.requests if r.messages[0].text.startswith(SUMMARY_PREAMBLE))
    reference = plan_file_text(written[0], "1. Probe.\n2. Report.\n")
    assert sum(m.text.count(reference) for m in after.messages if m.role == "system") == 1
    later = model.requests[model.requests.index(after) + 1]
    assert sum(m.text.count(reference) for m in later.messages) == 1  # sent once
    await assert_rebuilds(store, a, tid)



def thresholds(recs: list[Any]) -> list[Any]:
    return [r.event.data for r in recs if isinstance(r.event, ExtensionRecorded)
            and r.event.kind == pr.AUTO_COMPACT_KIND]


async def test_a_model_switch_records_the_threshold_again(store: SqliteStore) -> None:
    """Front ends read "N% until auto-compact" from the task's log."""
    import msgspec

    class Switch(Extension):
        """Sends the turn's second request to a model with another window."""

        name = "switch"

        async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
            return msgspec.structs.replace(request, model="claude-opus-5-5") \
                if view.state.steps_in_turn else request

    a = Agent(name="a", model="m", tools=[Probe()], extensions=(Switch(), Compaction()))
    client, tid = await run(store, a, Model([call(), answer("one")]))
    first, switched = thresholds(await client.events(tid))
    assert first["threshold"] != switched["threshold"]


async def test_a_continued_task_records_the_threshold_in_its_own_log(
        store: SqliteStore) -> None:
    a = Agent(name="a", model="m", tools=[Probe()], extensions=(Compaction(),))
    model = Model([answer("one"), answer("two")])
    client, src = await run(store, a, model)
    assert thresholds(await client.events(src))
    fork = await client.create("a", "go on", history_from=src)
    await runtime(store, a, model).run_until_idle()
    assert thresholds(await client.events(fork))


async def test_a_long_tasks_snapshot_stays_bounded_across_compactions(
        store: SqliteStore) -> None:
    """The fold keeps the compacted conversation, not every message ever
    added: the snapshot after the last of many compactions is about the size
    of the one after the first, while the log grows with every call."""
    from noeta.core import snapshots

    a = agent()
    model = Model([*(call() for _ in range(120)), answer("done")])
    client, tid = await run(store, a, model)
    log = await client.events(tid)
    exts = a.all_extensions()
    reducers = {e.name: e.reduce for e in exts}
    state = fold([], reducers, {e.name: e.initial_state() for e in exts})
    sizes = []
    for rec in log:
        fold([rec], reducers, state=state)
        if isinstance(rec.event, Compacted):
            data = snapshots.encode(state)
            assert data is not None
            sizes.append(len(data))
    assert len(sizes) >= 10
    whole = len(b"".join(msgspec.json.encode(r) for r in log))
    # Bounded by the compacted conversation and the plugins' state (a few
    # bytes per call id): no growth with the number of compactions.
    assert max(sizes[-3:]) < 1.25 * max(sizes[:3]) and max(sizes) < whole / 5, (sizes, whole)


async def test_the_saved_transcript_holds_the_whole_conversation(
        store: SqliteStore, tmp_path: Path) -> None:
    """Read from the log at each compaction: the second transcript still
    has the first prompt and every call the first compaction collapsed."""
    home = tmp_path / "home"
    work = tmp_path / "w"
    work.mkdir()
    a = agent(home=str(home))
    model = Model([*(call() for _ in range(26)), answer("done")])
    client, tid = await run(store, a, model, env_for=lambda _s: LocalExecEnv(str(work)))
    assert len(await compactions(client, tid)) >= 2
    transcript = next(home.glob("projects/*/*.jsonl"))
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert lines[0]["role"] == "user" and lines[0]["content"][0]["text"] == "go"
    uses = [b["id"] for m in lines if m["role"] == "assistant" for b in m["content"]
            if b.get("type") == "tool_use"]
    called = [e.response.message.tool_uses[0].id for e in await events(client, tid)
              if isinstance(e, ModelReplied) and e.response.message.tool_uses]
    # Every call up to the last compaction, in order; no summary among them.
    assert len(uses) > 8 and uses == called[:len(uses)]
    assert not any(m["content"][0].get("text", "").startswith(SUMMARY_PREAMBLE)
                   for m in lines if m["content"])


async def test_a_log_read_that_fails_leaves_no_transcript_and_no_failure(
        tmp_path: Path) -> None:
    """The saved transcript is incidental: a store error reading the log
    costs the file, not the compaction."""
    from types import SimpleNamespace

    from noeta.core.loop.state import TaskState

    work = tmp_path / "w"
    work.mkdir()
    ext = Compaction(home=str(tmp_path / "home"))

    async def read_log(_task_id: str, after: int = 0) -> list[Any]:
        raise records.NewerRecord("a newer worker wrote this log")

    view = SimpleNamespace(env=LocalExecEnv(str(work)), task_id="t", read_log=read_log)
    assert await ext._transcript(view, TaskState(task_id="t")) is None  # type: ignore[arg-type]
