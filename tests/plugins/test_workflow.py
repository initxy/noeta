"""Workflow: a script replayed in an embedded V8 against recorded agent results;
agents are child tasks, the run continues as they end, its end arrives as a
task notification, and a run resumes from an earlier one's results."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, TaskCreated, ToolFinished
from noeta.core.runner import Runtime
from noeta.core.types import ModelRequest, ModelResponse
from noeta.plugins.structured_output import StructuredOutput
from noeta.plugins.workflow import Orchestration
from noeta.plugins.workflow import engine as engine_module
from noeta.plugins.workflow.engine import Engine
from noeta.testing import reply
from noeta.toolkit.background_tasks import STOP_RUN

SCRIPT = """export const meta = {
  name: 'count-words',
  description: 'Count words per item, then sum them',
  phases: [{ title: 'Count' }],
}
phase('Count')
const counts = await pipeline(args, x => agent(`count ${x}`, {label: x, schema: SCHEMA}),
                              c => c.n)
log(`counted ${counts.length}`)
const [total] = await parallel([() => agent(`sum ${counts.join(',')}`)])
return { counts, total }
""".replace("SCHEMA", json.dumps({"type": "object", "properties": {"n": {"type": "number"}},
                                   "required": ["n"]}))


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


class Model:
    """The parent's turns are scripted; a workflow agent answers its prompt
    (a counting one through ``StructuredOutput``, unless ``text_counts``)."""

    family = "scripted"

    def __init__(self, *main: ModelResponse, text_counts: bool = False) -> None:
        self.main = list(main)
        self.parent: list[ModelRequest] = []
        self.children: list[ModelRequest] = []
        self.text_counts = text_counts

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        first = request.messages[0].text if request.messages else ""
        if first.startswith("count "):
            self.children.append(request)
            word = first.split()[1]
            if self.text_counts:
                return reply(json.dumps({"n": len(word)}))
            return reply("", ("StructuredOutput", {"n": len(word)}))
        if first.startswith("sum "):
            self.children.append(request)
            nums = first.split()[1].split(",")
            return reply(str(sum(int(n) for n in nums)))
        self.parent.append(request)
        return self.main.pop(0)


def runtime(store: SqliteStore, model: Model, ext: Orchestration,
            env: Any = None) -> Runtime:
    main = Agent(name="main", model="m", extensions=[ext])
    worker = Agent(name="general-purpose", model="m", description="worker",
                   extensions=[StructuredOutput()])
    return Runtime(store=store, agents={"main": main, "general-purpose": worker},
                   provider_for=lambda _m: model, env_for=lambda _s: env,
                   sleep=_no_sleep)


async def test_saved_workflows_come_from_noetas_own_places(store: SqliteStore,
                                                           tmp_path: Path) -> None:
    work, home = tmp_path / "work", tmp_path / "home"
    for path, name in [(work / ".noeta/workflows", "project"), (home / "workflows", "user"),
                       (work / ".claude/workflows", "claude")]:
        path.mkdir(parents=True)
        (path / f"{name}.js").write_text(
            f"export const meta = {{name: '{name}-wf', description: 'd'}}\nreturn 1\n")
    client = Client(store)
    texts = {}
    for name in ["project", "user", "claude"]:
        model = Model(reply("", ("Workflow", {"name": name})), *[reply("ok")] * 3)
        rt = runtime(store, model, Orchestration(engine=Engine(), home=str(home)),
                     env=LocalExecEnv(str(work)))
        tid = await client.create("main", "go")
        await rt.run_until_idle()
        [result] = [e for e in (r.event for r in await client.events(tid))
                    if isinstance(e, ToolFinished)]
        texts[name] = (result.result.is_error, result.result.text)
    assert texts["project"][1].startswith('Workflow "project-wf" started')
    assert texts["user"][1].startswith('Workflow "user-wf" started')
    # Claude Code's directory is not one of noeta's places.
    # The refusal names the saved workflows there are.
    assert texts["claude"] == (True, "Workflow: no saved workflow is named 'claude'. Saved "
                                     "workflows: project, user.")


async def test_a_workflow_runs_its_agents_and_notifies_its_end(store: SqliteStore) -> None:
    model = Model(reply("", ("Workflow", {"script": SCRIPT, "args": ["ab", "abcd"]})),
                  reply("started"), reply("the total is 6"))
    ext = Orchestration(engine=Engine())
    rt = runtime(store, model, ext)
    client = Client(store)
    tid = await client.create("main", "use a workflow to count")
    await rt.run_until_idle()
    events = [r.event for r in await client.events(tid)]
    started = next(e for e in events if isinstance(e, ToolFinished))
    assert "started in the background as wf_" in started.result.text
    # Two counting agents (schema-typed: StructuredOutput takes their answer), then the sum.
    assert len(model.children) == 3
    [tool] = [t for t in model.children[0].tools if t.name == "StructuredOutput"]
    assert tool.input_schema == {"type": "object", "properties": {"n": {"type": "number"}},
                                 "required": ["n"]}
    assert model.children[0].output_schema is None
    assert "calling the StructuredOutput tool once" in model.children[0].messages[0].text
    assert not any(t.name == "StructuredOutput" for t in model.children[2].tools)
    assert model.children[2].messages[0].text.startswith("sum 2,4")
    notice = model.parent[-1].messages[-1].text
    assert "<task-notification>" in notice and "<status>completed</status>" in notice
    assert '<summary>Dynamic workflow "count-words" completed</summary>' in notice
    assert "<usage><agent_count>3</agent_count><subagent_tokens>15</subagent_tokens>" \
           "<tool_uses>2</tool_uses><duration_ms>" in notice
    assert '"counts": [\n    2,\n    4\n  ]' in notice and '"total": "6"' in notice
    logs = [e.data["data"]["log"] for e in events if isinstance(e, ExtensionRecorded)
            and e.kind == "notice" and (e.data.get("data") or {}).get("log")]
    assert logs == ["counted 2"]
    children = [r.task_id for r in await client.tasks(parent_id=tid)]
    assert len(children) == 3
    created = [(await client.events(c))[0].event for c in children]
    assert all(isinstance(c, TaskCreated) and c.agent == "general-purpose" for c in created)


class _HeldModel(Model):
    """A model whose workflow agents wait until let go."""

    def __init__(self, *main: ModelResponse) -> None:
        super().__init__(*main)
        self.go = asyncio.Event()
        self.waiting = 0

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        first = request.messages[0].text if request.messages else ""
        if first.startswith(("count ", "sum ")):
            self.waiting += 1
            await self.go.wait()
        return await super().complete(request, on_delta)


async def _until(check: Any) -> None:
    for _ in range(500):
        if await check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_task_stop_ends_a_run_and_cancels_its_agents(store: SqliteStore) -> None:
    # TaskStop posts the run's stop: the run ends killed, its running agents
    # are cancelled, nothing more starts, and its notice says it was stopped
    # (Claude Code's).
    model = _HeldModel(reply("", ("Workflow", {"script": SCRIPT, "args": ["ab", "abcd"]})),
                       reply("started"), reply("stopping"), reply("stopped"))
    rt = runtime(store, model, Orchestration(engine=Engine()))
    client = Client(store)
    tid = await client.create("main", "use a workflow to count")
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))

    async def agents_waiting() -> bool:
        return model.waiting == 2

    await _until(agents_waiting)
    rid = next(e for e in (r.event for r in await client.events(tid))
               if isinstance(e, ToolFinished)).meta["run_id"]
    # TaskStop posts it during a turn; a signal opens none, so a message does.
    await client.signal(tid, STOP_RUN, {"run_id": rid})
    await client.send(tid, "stop it")

    async def agents_ended() -> bool:
        rows = await client.tasks(parent_id=tid)
        return len(rows) == 2 and all(r.status == "ended" for r in rows)

    await _until(agents_ended)
    model.go.set()
    await asyncio.sleep(0.2)
    stop.set()
    await serving
    events = [r.event for r in await client.events(tid)]
    ends = [e.data for e in events if isinstance(e, ExtensionRecorded)
            and e.extension == "workflow" and e.kind == "ended"]
    assert ends == [{"run_id": rid, "status": "killed", "error": ""}]
    assert [r.end_status for r in await client.tasks(parent_id=tid)] == ["cancelled"] * 2
    assert model.children == []  # no agent answered, and the sum never started
    notice = model.parent[-1].messages[-1].text
    assert "<status>killed</status>" in notice
    assert '<summary>Dynamic workflow "count-words" was stopped</summary>' in notice


async def test_resume_reuses_finished_agents(store: SqliteStore) -> None:
    model = Model(reply("", ("Workflow", {"script": SCRIPT, "args": ["ab", "abcd"]})),
                  reply("started"), reply("done once"))
    ext = Orchestration(engine=Engine())
    rt = runtime(store, model, ext)
    client = Client(store)
    tid = await client.create("main", "use a workflow")
    await rt.run_until_idle()
    first = next(e for e in (r.event for r in await client.events(tid))
                 if isinstance(e, ToolFinished)).meta["run_id"]
    model.main += [reply("", ("Workflow", {"script": SCRIPT, "args": ["ab", "abcd", "xyz"],
                                           "resumeFromRunId": first})),
                   reply("again"), reply("done twice")]
    before = len(model.children)
    await client.send(tid, "add xyz")
    await rt.run_until_idle()
    # Only the new item's count and the new sum ran.
    assert [c.messages[0].text.split("\n")[0] for c in model.children[before:]] == [
        "count xyz", "sum 2,4,3"]


async def test_a_schema_agent_that_never_calls_structured_output_fails(
        store: SqliteStore) -> None:
    script = ("export const meta = {name: 'one', description: 'd'}\n"
              "return await agent('count ab', {schema: {type: 'object', properties: {n: {type: 'number'}}}})\n")
    model = Model(reply("", ("Workflow", {"script": script})),
                  reply("started"), reply("done"), text_counts=True)
    rt = runtime(store, model, Orchestration(engine=Engine()))
    client = Client(store)
    tid = await client.create("main", "use a workflow")
    await rt.run_until_idle()
    # The JSON in its text is not taken: it was reminded once, then failed.
    counts = [c for c in model.children if c.messages[0].text.startswith("count ")]
    assert len(counts) == 2 and "[structured-output-enforce]" in counts[1].messages[-1].text
    failed = [e for e in (r.event for r in await client.events(tid))
              if isinstance(e, ExtensionRecorded) and e.kind == "agent_failed"]
    assert len(failed) == 1


async def test_a_schema_agent_started_by_an_older_noeta_answers_in_json_text(
        store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child started before agents answered through ``StructuredOutput``
    (its prompt asked for JSON text; its log holds no such call) and ending
    after the upgrade: its text, valid for the schema, is its result."""
    import noeta.plugins.workflow as workflow

    monkeypatch.setattr(workflow, "SCHEMA_NOTE", "\n\nYou are a step of a workflow script: "
                        "your final reply is parsed as JSON matching this schema and returned "
                        "to the script:\n{}")
    script = ("export const meta = {name: 'one', description: 'd'}\n"
              "return await agent('count abc', {schema: {type: 'object', properties: "
              "{n: {type: 'number'}}, required: ['n']}})\n")
    model = Model(reply("", ("Workflow", {"script": script})),
                  reply("started"), reply("done"), text_counts=True)
    main = Agent(name="main", model="m", extensions=[Orchestration(engine=Engine())])
    old = Agent(name="general-purpose", model="m", description="worker")
    rt = Runtime(store=store, agents={"main": main, "general-purpose": old},
                 provider_for=lambda _m: model, env_for=lambda _s: None, sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("main", "use a workflow")
    await rt.run_until_idle()
    events = [r.event for r in await client.events(tid)]
    [result] = [e.data for e in events if isinstance(e, ExtensionRecorded)
                and e.kind == "result"]
    assert result["value"] == {"n": 3}
    assert not [e for e in events if isinstance(e, ExtensionRecorded)
                and e.kind == "agent_failed"]


async def test_a_schema_agent_whose_tools_list_lacks_structured_output_still_answers(
        store: SqliteStore) -> None:
    """A custom agent with a ``tools:`` allow-list run with a schema: the
    answer tool is offered anyway."""
    script = ("export const meta = {name: 'one', description: 'd'}\n"
              "return await agent('count abcd', {agentType: 'reader', schema: {type: 'object', "
              "properties: {n: {type: 'number'}}, required: ['n']}})\n")
    model = Model(reply("", ("Workflow", {"script": script})), reply("started"), reply("done"))
    main = Agent(name="main", model="m", extensions=[Orchestration(engine=Engine())])
    reader = Agent(name="reader", model="m", description="reads", allowed_tools=["Read"],
                   extensions=[StructuredOutput()])
    rt = Runtime(store=store, agents={"main": main, "reader": reader},
                 provider_for=lambda _m: model, env_for=lambda _s: None, sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("main", "use a workflow")
    await rt.run_until_idle()
    assert [t.name for t in model.children[0].tools] == ["StructuredOutput"]
    [result] = [e.data for e in (r.event for r in await client.events(tid))
                if isinstance(e, ExtensionRecorded) and e.kind == "result"]
    assert result["value"] == {"n": 4}


class _RemoteEnv(LocalExecEnv):
    """An env whose files are not on this machine (a container's)."""

    def __init__(self, cwd: str) -> None:
        super().__init__(cwd)
        self.files: dict[str, bytes] = {}

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def read_bytes(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


async def test_a_saved_script_is_written_where_script_path_reads_it(
        store: SqliteStore, tmp_path: Path) -> None:
    script = "export const meta = {name: 'one', description: 'd'}\nreturn 1\n"
    model = Model(reply("", ("Workflow", {"script": script})), reply("started"), reply("ok"))
    env = _RemoteEnv(str(tmp_path))
    rt = runtime(store, model, Orchestration(engine=Engine()), env=env)
    rt.spill_root = str(tmp_path / "spill")
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    started = next(e for e in (r.event for r in await client.events(tid))
                   if isinstance(e, ToolFinished))
    path = started.meta["script_path"]
    assert env.files[path].decode() == script and not Path(path).exists()
    model.main += [reply("", ("Workflow", {"scriptPath": path})), reply("again"), reply("ok")]
    await client.send(tid, "again")
    await rt.run_until_idle()
    texts = [e.result.text for e in (r.event for r in await client.events(tid))
             if isinstance(e, ToolFinished)]
    assert texts[-1].startswith('Workflow "one" started')


async def test_a_bad_meta_and_a_missing_engine_are_refused(
        store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = "const x = 1\nexport const meta = {name: 'a', description: 'b'}"
    model = Model(reply("", ("Workflow", {"script": bad})), reply("ok"))
    rt = runtime(store, model, Orchestration(engine=Engine()))
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [result] = [e for e in (r.event for r in await client.events(tid))
                if isinstance(e, ToolFinished)]
    assert result.result.is_error and "must be the first statement" in result.result.text

    # A platform where mini-racer does not import: the tool says so and starts nothing.
    monkeypatch.setitem(sys.modules, "py_mini_racer", None)
    engine_module._mini_racer.cache_clear()
    try:
        model2 = Model(reply("", ("Workflow", {"script": SCRIPT})), reply("ok"))
        rt2 = runtime(store, model2, Orchestration(engine=Engine()))
        tid2 = await client.create("main", "go")
        await rt2.run_until_idle()
    finally:
        engine_module._mini_racer.cache_clear()
    [missing] = [e for e in (r.event for r in await client.events(tid2))
                 if isinstance(e, ToolFinished)]
    assert missing.result.is_error
    assert "embedded JavaScript engine does not load here" in missing.result.text
    assert "mini-racer" in missing.result.text
    assert await client.tasks(parent_id=tid2) == []


HEAD = "export const meta = {name: 't', description: 'd'}\n"


async def test_the_engine_refuses_nondeterminism_and_code_from_strings() -> None:
    engine = Engine()
    for body, error in [("return Date.now()", "Date.now()"), ("return Math.random()", "Math"),
                        ("return new Date()", "new Date() without arguments"),
                        ("return eval('1')", "Code generation"),
                        ("return agent.constructor('return process')()", "Code generation"),
                        ("return (async () => {}).constructor('return 1')", "Code generation"),
                        ("return (function* () {}).constructor('yield 1')", "Code generation")]:
        out = await engine.replay(HEAD + body, None, {}, [])
        assert out.status == "error" and error in out.error, body
    # A date from a timestamp is deterministic and allowed.
    out = await engine.replay(HEAD + "return new Date(0).toISOString()", None, {}, [])
    assert out.value == "1970-01-01T00:00:00.000Z"


async def test_the_sandbox_has_no_host_api() -> None:
    names = ["require", "process", "fetch", "module", "Buffer", "setTimeout", "setInterval",
             "WebAssembly", "SharedArrayBuffer", "Atomics", "Intl", "Temporal", "WeakRef",
             "FinalizationRegistry", "XMLHttpRequest", "importScripts"]
    body = f"return {json.dumps(names)}.filter(n => typeof globalThis[n] !== 'undefined')"
    out = await Engine().replay(HEAD + body, None, {}, [])
    assert out.status == "done" and out.value == []
    out = await Engine().replay(HEAD + "return require('fs')", None, {}, [])
    assert out.status == "error" and "require is not defined" in out.error
    out = await Engine().replay(HEAD + "await import('fs')", None, {}, [])
    assert out.status == "error"


# A script spinning after an await is ended by closing its context; mini-racer
# then reports the stopped call's callback on its closed loop.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
async def test_a_replay_has_a_time_limit() -> None:
    engine = Engine(timeout=0.5)
    for body in ["while (true) {}", "await agent('a'); while (true) {}",
                 "await Promise.resolve(); for (;;) {}"]:
        started = time.monotonic()
        out = await engine.replay(HEAD + body, None, {_key("a"): "x"}, [])
        assert out.status == "error" and "stopped" in out.error and "0.5 s" in out.error, body
        assert time.monotonic() - started < 5
    # The next replay gets a fresh context and runs normally.
    assert (await engine.replay(HEAD + "return 1", None, {}, [])).value == 1


async def test_a_replay_has_a_memory_limit() -> None:
    engine = Engine(max_memory=32 * 1024 * 1024)
    body = "const a = []; while (true) a.push(new Array(100000).fill(1))"
    out = await engine.replay(HEAD + body, None, {}, [])
    assert out.status == "error" and "32 MB" in out.error
    assert (await engine.replay(HEAD + "return [1, 2]", None, {}, [])).value == [1, 2]


async def test_a_slow_replay_does_not_block_the_event_loop() -> None:
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    out = await Engine(timeout=0.5).replay(HEAD + "while (true) {}", None, {}, [])
    ticker.cancel()
    assert out.status == "error" and ticks >= 10


def _key(prompt: str, options: dict[str, Any] | None = None, n: int = 0) -> str:
    """The key agent() gives a call (sha256 of its prompt and sorted options)."""
    text = json.dumps([prompt, options or {}], separators=(",", ":"), ensure_ascii=False,
                      sort_keys=True)
    return f"{hashlib.sha256(text.encode()).hexdigest()[:24]}-{n}"


async def test_a_replay_of_parallel_and_pipeline_moves_forward_with_results() -> None:
    engine = Engine()
    script = HEAD + """
phase('Map')
const doubled = await pipeline(args, x => agent(`double ${x}`, {label: String(x)}),
                               (d, x, i) => {
                                 if (d === null) throw new Error('lost')
                                 return agent(`check ${d} ${x} ${i}`, {model: 'haiku'})
                               })
log(`stage one: ${doubled.join('|')}`)
const [a, b] = await parallel([() => agent('first'), () => { throw new Error('dropped') },
                               () => agent('first')])
return {doubled, a, b}
"""
    first = await engine.replay(script, [1, 2], {}, [])
    assert first.status == "blocked"
    assert [(p["prompt"], p["label"], p["phase"]) for p in first.pending] == [
        ("double 1", "1", "Map"), ("double 2", "2", "Map")]
    assert first.pending[0]["key"] == _key("double 1")
    # One item's first stage is done: its second stage starts, the other waits.
    second = await engine.replay(script, [1, 2], {_key("double 1"): "2"}, [])
    assert [p["prompt"] for p in second.pending] == ["double 2", "check 2 1 0"]
    assert second.pending[1]["options"] == {"model": "haiku"}
    # The second item's agent failed: its later stage is skipped and it becomes null.
    results = {_key("double 1"): "2", _key("check 2 1 0", {"model": "haiku"}): "ok-1"}
    third = await engine.replay(script, [1, 2], results, [_key("double 2")])
    assert third.logs == ["stage one: ok-1|"]
    assert [p["key"] for p in third.pending] == [_key("first"), _key("first", n=1)]
    results |= {_key("first"): "A", _key("first", n=1): "B"}
    done = await engine.replay(script, [1, 2], results, [_key("double 2")])
    assert done.status == "done"
    assert done.value == {"doubled": ["ok-1", None], "a": "A", "b": None}


async def test_meta_must_be_a_pure_literal() -> None:
    engine = Engine()
    for meta, error in [("{name: x, description: 'd'}", "x is not defined"),
                        ("{name: JSON.stringify(1), description: 'd'}", "calls and functions"),
                        ("{name: 'a', description: 'd', get x() { return 1 }}", "calls"),
                        ("{name: `a${1}`, description: 'd'}", "interpolation"),
                        ("{name: 'a'}", "meta.description is required")]:
        out = await engine.check(f"export const meta = {meta}\nreturn 1")
        assert error in out.get("error", ""), meta
    ok = await engine.check("export const meta = {name: 'a (b)', description: `d`, "
                            "phases: [{title: 'P', detail: 'x => y'}]}\nreturn 1")
    assert ok == {"meta": {"name": "a (b)", "description": "d",
                           "phases": [{"title": "P", "detail": "x => y"}]}}


def test_a_continuation_keeps_finished_runs_only() -> None:
    ext = Orchestration()
    st = ext.reduce(None, "run", {"run_id": "r1"})
    st = ext.reduce(st, "run", {"run_id": "r2"})
    st = ext.reduce(st, "ended", {"run_id": "r2", "status": "done"})
    assert list(ext.continue_state(st)["runs"]) == ["r2"]


def test_a_run_keeps_two_to_sixteen_agents_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.plugins import workflow
    for cpus, want in [(1, 2), (3, 2), (8, 6), (64, 16)]:
        monkeypatch.setattr(workflow.os, "cpu_count", lambda c=cpus: c)
        assert workflow.default_max_running() == want



class _Sink:
    post = None

    def __init__(self) -> None:
        self.records: list[tuple[str, str, Any, Any]] = []

    def notify(self, text: str, *, kind: str | None = None, data: Any = None) -> None:
        pass

    def record(self, extension: str, kind: str, data: Any = None, *,
               message: Any = None) -> None:
        self.records.append((extension, kind, data, message))


@pytest.mark.parametrize("status", ["failed", "killed", "completed"])
async def test_the_end_notice_says_how_to_resume_or_rerun(status: str) -> None:
    # Claude Code's: a failed or stopped run's notice says how to resume it
    # from its saved script, a completed one's how to re-run what changed;
    # failed agents are listed.
    ext = Orchestration(engine=Engine())
    run = {"run_id": "wf_abc123", "name": "audit", "call_id": "c1", "started_at": 0,
           "script_path": "/spill/t/workflows/wf_abc123.js", "args": ["a"],
           "agents": {"k1": {"label": "scan a"}}, "spent": 7, "calls": 4,
           "failures": ["scan a: cancelled"]}
    sink = _Sink()
    await ext._end(sink, run, status, value={"ok": 1}, error="the script failed")
    notice = sink.records[-1][3].text
    call = "Workflow({scriptPath: '/spill/t/workflows/wf_abc123.js', " \
           "resumeFromRunId: 'wf_abc123', args: [\"a\"]})"
    summary = {"failed": 'Dynamic workflow "audit" failed: the script failed',
               "killed": 'Dynamic workflow "audit" was stopped',
               "completed": 'Dynamic workflow "audit" completed'}[status]
    assert f"<summary>{summary}</summary>" in notice
    assert ("<recovery>" in notice) is (status != "completed")
    assert ("<diagnostics>" in notice) is (status == "completed")
    assert ("<result>" in notice) is (status == "completed")
    assert call in notice
    assert "<failures>scan a: cancelled</failures>" in notice
    assert "<usage><agent_count>1</agent_count><subagent_tokens>7</subagent_tokens>" \
           "<tool_uses>4</tool_uses>" in notice
    del run["script_path"]
    await ext._end(sink, run, status, value=None, error="x")
    assert "resumeFromRunId" not in sink.records[-1][3].text
