"""Workflow: a script replayed by Node.js against recorded agent results;
agents are child tasks, the run continues as they end, its end arrives as a
task notification, and a run resumes from an earlier one's results."""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, TaskCreated, ToolFinished
from noeta.core.runner import Runtime
from noeta.core.types import ModelRequest, ModelResponse
from noeta.plugins.workflow import Orchestration
from noeta.plugins.workflow.engine import Engine, EngineMissing
from noeta.testing import reply

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")

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
    """The parent's turns are scripted; a workflow agent answers its prompt."""

    family = "scripted"

    def __init__(self, *main: ModelResponse) -> None:
        self.main = list(main)
        self.parent: list[ModelRequest] = []
        self.children: list[ModelRequest] = []

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        first = request.messages[0].text if request.messages else ""
        if first.startswith("count "):
            self.children.append(request)
            word = first.split()[1]
            return reply(json.dumps({"n": len(word)}))
        if first.startswith("sum "):
            self.children.append(request)
            nums = first.split()[1].split(",")
            return reply(str(sum(int(n) for n in nums)))
        self.parent.append(request)
        return self.main.pop(0)


def runtime(store: SqliteStore, model: Model, ext: Orchestration) -> Runtime:
    main = Agent(name="main", model="m", extensions=[ext])
    worker = Agent(name="general-purpose", model="m", description="worker")
    return Runtime(store=store, agents={"main": main, "general-purpose": worker},
                   provider_for=lambda _m: model, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                   sleep=_no_sleep)


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
    # Two counting agents (schema-typed: output_schema in their config), then the sum.
    assert len(model.children) == 3
    assert model.children[0].output_schema == {"type": "object",
                                               "properties": {"n": {"type": "number"}},
                                               "required": ["n"]}
    assert model.children[2].messages[0].text.startswith("sum 2,4")
    notice = model.parent[-1].messages[-1].text
    assert "<task-notification>" in notice and "<status>completed</status>" in notice
    assert '"counts": [\n    2,\n    4\n  ]' in notice and '"total": "6"' in notice
    logs = [e.data["data"]["log"] for e in events if isinstance(e, ExtensionRecorded)
            and e.kind == "notice" and (e.data.get("data") or {}).get("log")]
    assert logs == ["counted 2"]
    children = [r.task_id for r in await client.tasks(parent_id=tid)]
    assert len(children) == 3
    created = [(await client.events(c))[0].event for c in children]
    assert all(isinstance(c, TaskCreated) and c.agent == "general-purpose" for c in created)


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


async def test_a_bad_meta_and_a_missing_engine_are_refused(store: SqliteStore) -> None:
    bad = "const x = 1\nexport const meta = {name: 'a', description: 'b'}"
    model = Model(reply("", ("Workflow", {"script": bad})), reply("ok"))

    class Missing(Engine):
        def command(self) -> list[str]:
            raise EngineMissing("node was not found on PATH")

    rt = runtime(store, model, Orchestration(engine=Engine()))
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [result] = [e for e in (r.event for r in await client.events(tid))
                if isinstance(e, ToolFinished)]
    assert result.result.is_error and "must be the first statement" in result.result.text

    model2 = Model(reply("", ("Workflow", {"script": SCRIPT})), reply("ok"))
    rt2 = runtime(store, model2, Orchestration(engine=Missing()))
    tid2 = await client.create("main", "go")
    await rt2.run_until_idle()
    [missing] = [e for e in (r.event for r in await client.events(tid2))
                 if isinstance(e, ToolFinished)]
    assert missing.result.is_error and "needs Node.js 18" in missing.result.text


async def test_the_engine_refuses_nondeterminism_and_code_from_strings() -> None:
    engine = Engine()
    head = "export const meta = {name: 't', description: 'd'}\n"
    for body, error in [("return Date.now()", "Date.now()"), ("return Math.random()", "Math"),
                        ("return eval('1')", "Code generation"),
                        ("return agent.constructor('return process')()", "Code generation")]:
        out = await engine.replay(head + body, None, {}, [])
        assert out.status == "error" and error in out.error, body
    assert (await engine.replay(head + "return typeof process", None, {}, [])).value \
        == "undefined"
