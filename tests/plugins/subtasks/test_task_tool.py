"""The Task tool: foreground children, fan-out, refusals, depth, roster."""

from __future__ import annotations

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import Fatal
from noeta.core.events import TaskCreated, TaskEnded, ToolFinished
from noeta.core.runner.texts import STOPPED_CALL_ERROR
from noeta.core.store.sqlite import SqliteStore
from noeta.plugins.subtasks import SubagentSpec, Subtasks, texts
from noeta.plugins.subtasks.tool import task_schema
from noeta.testing import ScriptedProvider, reply
from tests.plugins.subtasks.conftest import Probe, echo, events, runtime

ROSTER = [
    SubagentSpec(name="worker", description="Does work."),
    SubagentSpec(name="scout", description="Looks around.", agent="worker"),
]


def parent(**kw: object) -> Agent:
    tools = kw.pop("tools", ())
    return Agent(name="main", model="p", tools=tools,  # type: ignore[arg-type]
                 extensions=[Subtasks(ROSTER, **kw)])  # type: ignore[arg-type]


def worker() -> Agent:
    return Agent(name="worker", model="c", one_shot=True, extensions=[Subtasks()])


def task(prompt: str, agent: str = "worker", **extra: object) -> tuple[str, dict[str, object]]:
    return ("Task", {"description": "do it", "prompt": prompt, "subagent_type": agent, **extra})


async def results(client: Client, tid: str) -> list[tuple[str, bool]]:
    return [(e.result.text, e.result.is_error) for e in await events(client, tid)
            if isinstance(e, ToolFinished)]


def test_schema() -> None:
    schema = task_schema(Subtasks(ROSTER).roster)
    assert list(schema["properties"]) == ["description", "prompt", "subagent_type", "background"]
    assert schema["required"] == ["description", "prompt", "subagent_type"]
    agent = schema["properties"]["subagent_type"]
    assert agent["enum"] == ["scout", "worker"]
    assert agent["description"] == (
        "The type of specialized agent to use for this task. Available: "
        "scout — Looks around.; worker — Does work.")


async def test_foreground_child_returns_its_answer(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", task("count to 3", agent="scout")), reply("relayed")])
    c = ScriptedProvider([reply("1 2 3")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go", config={"cwd": "/w", "other": 1})
    await rt.run_until_idle()
    assert await results(client, tid) == [("1 2 3", False)]
    [child] = await client.tasks(parent_id=tid)
    created = (await events(client, child.task_id))[0]
    assert isinstance(created, TaskCreated) and created.agent == "worker"
    assert created.config == {"cwd": "/w", "subtask_depth": 1, "subagent_type": "scout"}
    assert c.requests[0].messages[0].text == "count to 3"
    # A sub-agent without a roster is offered no Task tool.
    assert [t.name for t in c.requests[0].tools] == []
    assert [t.name for t in p.requests[0].tools] == ["Task"]


async def test_fan_out_runs_children_and_answers_in_spawn_order(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", task("a"), task("b"), task("c")), reply("all done")])
    c = ScriptedProvider([echo(), echo(), echo()])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    assert len(await client.tasks(parent_id=tid)) == 3
    results_msg = p.requests[1].messages[-1]
    assert [b.text for b in results_msg.content] == [  # type: ignore[union-attr]
        "did: a", "did: b", "did: c"]


async def test_failed_child_shows_its_error(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", task("x")), reply("noted")])
    c = ScriptedProvider([Fatal("bad key")])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [(text, is_error)] = await results(client, tid)
    assert is_error and text.startswith("llm_error: bad key")


async def test_bad_background_refuses_the_whole_response(store: SqliteStore) -> None:
    probe = Probe()
    p = ScriptedProvider([reply("", ("Probe", {}), task("x", background="false")),
                          reply("ok")])
    rt = runtime(store, {"p": p}, parent(tools=[probe]), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    expected = texts.NOTHING_RAN.format(reason=texts.BAD_BACKGROUND.format(value="false"))
    assert expected.startswith("Nothing in this response ran: 'background' must be true or false")
    assert await results(client, tid) == [(expected, True), (expected, True)]
    assert probe.runs == 0 and await client.tasks(parent_id=tid) == []


async def test_empty_prompt_and_missing_fields_are_refused(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", task("  ")),
                          reply("", ("Task", {"description": "d", "prompt": 3})),
                          reply("ok")])
    rt = runtime(store, {"p": p}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    assert await results(client, tid) == [
        (texts.NOTHING_RAN.format(reason=texts.EMPTY_PROMPT), True),
        (texts.MISSING_FIELDS, True),
    ]


async def test_legacy_agent_goal_shape_is_accepted(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", ("Task", {"agent": "worker", "goal": "legacy"})),
                          reply("ok")])
    c = ScriptedProvider([echo()])
    rt = runtime(store, {"p": p, "c": c}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    assert await results(client, tid) == [("did: legacy", False)]


async def test_fan_out_limit_refuses_task_calls_but_runs_the_rest(store: SqliteStore) -> None:
    probe = Probe()
    p = ScriptedProvider([reply("", task("a"), ("Probe", {}), task("b"), task("c")),
                          reply("ok")])
    rt = runtime(store, {"p": p}, parent(tools=[probe], max_fanout=2), worker())
    client = Client(store)
    await client.create("main", "go")
    await rt.run_until_idle()
    refusal = texts.FANOUT_TOO_BIG.format(n=3, limit=2)
    assert refusal == ("3 Task calls in one response exceeds the limit of 2. None were "
                       "started — re-issue them in batches of 2 or fewer.")
    sent = p.requests[1].messages[-1].content
    assert [(b.text, b.is_error) for b in sent] == [  # type: ignore[union-attr]
        (refusal, True), ("seen", False), (refusal, True), (refusal, True)]
    assert probe.runs == 1


async def test_unknown_sub_agent_is_refused_and_the_turn_goes_on(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", task("a"), task("b", agent="nobody")), reply("myself then")])
    rt = runtime(store, {"p": p}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    refusal = ("Delegation refused: no sub-agent named 'nobody' (available: scout, worker). "
               "No sub-agent started; none of this response's Task calls ran. Use an offered "
               "sub-agent name, or do the work yourself.")
    assert await results(client, tid) == [(refusal, True), (refusal, True)]
    assert (await client.get(tid)).waiting_on == "input"


async def test_depth_limit(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", task("a")), reply("ok")])
    rt = runtime(store, {"p": p}, parent(), worker())
    client = Client(store)
    tid = await client.create("main", "go", config={"subtask_depth": 3})
    await rt.run_until_idle()
    [(text, _)] = await results(client, tid)
    assert text.startswith("Delegation refused: max_subtask_depth=3 reached. No sub-agent")


async def test_exclusive_tools_refuse_the_whole_response(store: SqliteStore) -> None:
    probe = Probe()
    p = ScriptedProvider([reply("", task("a"), ("Probe", {})), reply("ok")])
    rt = runtime(store, {"p": p}, parent(tools=[probe], exclusive_with={"Probe"}), worker())
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    text = ("Nothing in this response ran: Task cannot share a response with Probe. "
            "Re-issue the other calls in a separate response.")
    assert await results(client, tid) == [(text, True), (text, True)]
    assert probe.runs == 0


async def test_stopping_the_turn_withdraws_and_cancels_the_child(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", task("slow"))])
    rt = runtime(store, {"p": p}, parent(queue="kids"), worker())  # the child is never run
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_once()
    [child] = await client.tasks(parent_id=tid)
    await client.cancel(tid, turn_only=True)
    await rt.run_until_idle()
    assert await results(client, tid) == [(STOPPED_CALL_ERROR, True)]
    kids = runtime(store, {}, worker(), queues=("kids",))
    await kids.run_until_idle()
    end = (await events(client, child.task_id))[-1]
    assert isinstance(end, TaskEnded) and end.status == "cancelled"


async def test_nested_delegation_counts_depth(store: SqliteStore) -> None:
    lead = Agent(name="worker", model="c", one_shot=True,
                 extensions=[Subtasks(ROSTER, max_depth=1)])
    p = ScriptedProvider([reply("", task("go deeper")), reply("ok")])
    c = ScriptedProvider([reply("", task("deepest")), reply("could not")])
    rt = runtime(store, {"p": p, "c": c}, parent(), lead)
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    [(text, _)] = await results(client, child.task_id)
    assert text.startswith("Delegation refused: max_subtask_depth=1 reached.")
    assert await results(client, tid) == [("could not", False)]
