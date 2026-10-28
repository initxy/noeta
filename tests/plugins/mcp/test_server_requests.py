"""What a server asks the client: roots, elicitation (turned into the
kernel's question and parked), and the tool-list change notice."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import Answer, ToolFinished, Waiting
from noeta.core.questions import ask
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext
from noeta.core.types import Image
from noeta.plugins.mcp import Mcp, McpPool, McpServer
from noeta.plugins.mcp.client import RequestContext, initialize_params
from noeta.plugins.mcp.elicitation import elicitation_questions, elicitation_result
from noeta.plugins.mcp.stdio import StdioClient
from noeta.plugins.mcp.tools import McpTool
from noeta.testing import ScriptedProvider, reply

FAKE = str(Path(__file__).with_name("fake_server.py"))
TOOLS = json.dumps([{"name": n, "inputSchema": {"type": "object"}}
                    for n in ("ask", "roots", "changed", "echo")])


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


@pytest.fixture
async def pool() -> AsyncIterator[McpPool]:
    p = McpPool()
    yield p
    await p.close()


async def _no_sleep(_: float) -> None:
    return None


def runtime(store: SqliteStore, provider: ScriptedProvider, agent: Agent) -> Runtime:
    return Runtime(store=store, agents={agent.name: agent},
                   provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                   sleep=_no_sleep)


def server(**env: str) -> McpServer:
    return McpServer(alias="fake", command=[sys.executable, FAKE],
                     env={"FAKE_TOOLS": TOOLS, **env}, deferred=False)


def results(evs: list[Any]) -> list[str]:
    return ["".join(b.text for b in r.event.result.content if not isinstance(b, Image))
            for r in evs if isinstance(r.event, ToolFinished)]


FORM = {"message": "Pick a deploy target",
        "requestedSchema": {"type": "object", "required": ["env", "count"], "properties": {
            "env": {"type": "string", "title": "Environment", "enum": ["dev", "prod"],
                    "enumNames": ["Development", "Production"]},
            "count": {"type": "integer", "description": "How many"},
            "force": {"type": "boolean"},
            "note": {"type": "string"},
            "tags": {"type": "array", "items": {"enum": ["a", "b"]}}}}}


# ---------------------------------------------------------------- the client


def test_initialize_declares_roots_and_elicitation() -> None:
    caps = initialize_params()["capabilities"]
    assert caps == {"roots": {"listChanged": True}, "elicitation": {"form": {}, "url": {}}}
    assert initialize_params(bare_elicitation=True)["capabilities"]["elicitation"] == {}


async def test_roots_and_elicitation_go_to_the_bound_context(tmp_path: Path) -> None:
    client = StdioClient([sys.executable, FAKE])
    await client.start()
    asked: list[dict[str, Any]] = []

    async def elicit(params: dict[str, Any]) -> dict[str, Any]:
        asked.append(params)
        return {"action": "accept", "content": {"x": 1}}

    try:
        # Nobody bound: no roots, and a question is declined.
        roots = await client.call_tool("roots", {})
        assert json.loads(roots["content"][0]["text"]) == {"roots": []}
        declined = await client.call_tool("ask", {"message": "hi"})
        assert json.loads(declined["content"][0]["text"]) == {"action": "decline"}
        with client.bound(RequestContext(roots=[str(tmp_path)], elicit=elicit)):
            roots = await client.call_tool("roots", {})
            answer = await client.call_tool("ask", {"message": "hi"})
        assert json.loads(roots["content"][0]["text"])["roots"] == [
            {"uri": tmp_path.resolve().as_uri(), "name": tmp_path.name}]
        assert json.loads(answer["content"][0]["text"]) == {"action": "accept",
                                                            "content": {"x": 1}}
        assert asked == [{"message": "hi"}]
        assert not client.tools_changed
        await client.call_tool("changed", {})
        assert client.tools_changed
    finally:
        await client.close()


async def test_an_unknown_server_request_is_method_not_found() -> None:
    client = StdioClient([sys.executable, FAKE])
    reply = await client.answer({"jsonrpc": "2.0", "id": 7, "method": "sampling/createMessage"})
    assert reply is not None and reply["error"]["code"] == -32601
    assert await client.answer({"jsonrpc": "2.0", "method": "notifications/message"}) is None


# ---------------------------------------------------------------- the question form


def test_a_form_becomes_questions_and_the_answer_its_content() -> None:
    questions = elicitation_questions("deploy", FORM)
    assert questions[0]["question"].startswith("The MCP server 'deploy' asks: Pick a deploy")
    assert [o["label"] for o in questions[0]["options"]] == ["Development", "Production"]
    assert questions[1]["options"] == [] and "(integer)" in questions[1]["question"]
    assert [o["label"] for o in questions[2]["options"]] == ["Yes", "No", "Skip"]
    assert [o["label"] for o in questions[3]["options"]] == ["Skip"]
    assert questions[4]["multiSelect"] is True
    park = ask(*questions)
    answer = Answer(request_id="c", data={
        "0": {"selected": ["Production"]}, "1": {"other": "3"}, "2": {"selected": ["Skip"]},
        "3": {"other": "ship it"}, "4": {"selected": ["a", "b"]}})
    assert elicitation_result(FORM, answer, park) == {
        "action": "accept",
        "content": {"env": "prod", "count": 3, "note": "ship it", "tags": ["a", "b"]}}
    bad = Answer(request_id="c", data={
        "0": {"selected": ["Development"]}, "1": {"other": "many"}, "2": {"other": "yes"},
        "3": {"other": "x"}, "4": {"other": "c"}})
    assert elicitation_result(FORM, bad, park) == {"action": "decline"}
    assert elicitation_result(FORM, None, park) == {"action": "cancel"}
    dismissed = Answer(request_id="c", decision="deny")
    assert elicitation_result(FORM, dismissed, park) == {"action": "decline"}


def test_a_url_request_and_an_empty_form() -> None:
    url = {"mode": "url", "message": "Sign the form", "url": "https://x/form",
           "elicitationId": "e1"}
    questions = elicitation_questions("s", url)
    assert "https://x/form" in questions[0]["question"]
    park = ask(*questions)
    yes = Answer(request_id="c", data={"0": {"selected": ["Accept"]}})
    no = Answer(request_id="c", data={"0": {"selected": ["Decline"]}})
    assert elicitation_result(url, yes, park) == {"action": "accept"}
    assert elicitation_result(url, no, park) == {"action": "decline"}
    empty = {"message": "Continue?", "requestedSchema": {"type": "object", "properties": {}}}
    assert elicitation_result(empty, yes, ask(*elicitation_questions("s", empty))) == {
        "action": "accept"}


# ---------------------------------------------------------------- end to end


async def test_an_elicitation_parks_the_call_until_the_person_answers(
        store: SqliteStore, pool: McpPool, tmp_path: Path) -> None:
    form = {"message": "Your name?", "requestedSchema": {
        "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}
    provider = ScriptedProvider([reply("", ("mcp__fake__ask", form)), reply("done")])
    agent = Agent(name="a", model="m", extensions=[Mcp([server()], pool=pool)])
    rt = runtime(store, provider, agent)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    waiting = (await client.events(tid))[-1].event
    assert isinstance(waiting, Waiting) and waiting.on == "question"
    row = await client.get(tid)
    assert row.waiting_on == "question"
    await client.answer(tid, waiting.ids[0], data={"0": {"other": "Ada"}})
    await rt.run_until_idle()
    assert results(await client.events(tid)) == [
        json.dumps({"action": "accept", "content": {"name": "Ada"}}, sort_keys=True)]


async def test_a_stopped_turn_cancels_the_question(
        store: SqliteStore, pool: McpPool) -> None:
    provider = ScriptedProvider([reply("", ("mcp__fake__ask", {"message": "ok?"}))])
    agent = Agent(name="a", model="m", extensions=[Mcp([server()], pool=pool)])
    rt = runtime(store, provider, agent)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await client.cancel(tid, turn_only=True)
    await rt.run_until_idle()
    assert any("stopped while the MCP server waited" in r for r in results(
        await client.events(tid)))


async def test_an_ended_task_lets_go_of_its_parked_call(
        store: SqliteStore, pool: McpPool) -> None:
    provider = ScriptedProvider([reply("", ("mcp__fake__ask", {"message": "ok?"}))])
    ext = Mcp([server()], pool=pool)
    rt = runtime(store, provider, Agent(name="a", model="m", extensions=[ext]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    parked = next(iter(ext._parked.values()))
    await client.cancel(tid)
    await rt.run_until_idle()
    assert ext._parked == {}
    assert parked.call.job is not None and parked.call.job.cancelled()


async def test_a_lost_call_answers_outcome_unknown() -> None:
    """The worker that ran the call is gone (a restart): its answer has no
    call to go to."""
    ext = Mcp([server()])
    tool = McpTool(name="mcp__fake__ask", alias="fake", remote="ask", description="",
                   input_schema={}, invoke=ext._invoke, resume=ext._resume)
    ctx = ToolContext(task_id="t", call_id="c", env=None, state={},  # type: ignore[arg-type]
                      record=lambda *_: None)
    out = await tool.resume({}, ctx, Answer(request_id="c", data={"0": {"other": "x"}}))
    assert out.is_error and "outcome is unknown" in out.content[0].text  # type: ignore[union-attr]


async def test_roots_answer_the_tasks_workspace(store: SqliteStore, pool: McpPool,
                                                tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("mcp__fake__roots", {})), reply("done")])
    agent = Agent(name="a", model="m", extensions=[Mcp([server()], pool=pool)])
    rt = runtime(store, provider, agent)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    roots = json.loads(results(await client.events(tid))[0])["roots"]
    assert roots == [{"uri": tmp_path.resolve().as_uri(), "name": tmp_path.name}]


async def test_a_list_changed_notice_refreshes_the_tools_for_the_next_step(
        store: SqliteStore, pool: McpPool, tmp_path: Path) -> None:
    tools_file = tmp_path / "tools.json"
    tools_file.write_text(json.dumps([{"name": "changed", "inputSchema": {"type": "object"}}]))
    grown = json.dumps([{"name": "changed", "inputSchema": {"type": "object"}},
                        {"name": "fresh", "inputSchema": {"type": "object"}}])
    provider = ScriptedProvider([reply("", ("mcp__fake__changed", {})), reply("done")])
    agent = Agent(name="a", model="m", extensions=[
        Mcp([McpServer(alias="fake", command=[sys.executable, FAKE], deferred=False,
                       env={"FAKE_TOOLS_FILE": str(tools_file), "FAKE_CHANGE_TO": grown})],
            pool=pool)])
    rt = runtime(store, provider, agent)
    client = Client(store)
    await client.create("a", "go")
    await rt.run_until_idle()

    def names(i: int) -> list[str]:
        return [t.name for t in provider.requests[i].tools if t.name.startswith("mcp__")]

    assert names(0) == ["mcp__fake__changed"]
    assert names(1) == ["mcp__fake__changed", "mcp__fake__fresh"]
