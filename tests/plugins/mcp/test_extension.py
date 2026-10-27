"""The ``Mcp`` extension end to end: a scripted model, a real sqlite store, a
real stdio server."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, ToolFinished, ToolStarted, Waiting
from noeta.core.extension import Ask, Deny, Extension, Gate, TaskView
from noeta.core.runner import Runtime
from noeta.core.store.sqlite import SqliteStore
from noeta.core.types import Image, ModelRequest, ToolUse
from noeta.plugins.mcp import Mcp, McpPool, McpServer
from noeta.plugins.mcp.config import describe_tool, make_tool_name, map_server_names
from noeta.plugins.mcp.tools import McpTool
from noeta.testing import ScriptedProvider, reply

FAKE = str(Path(__file__).with_name("fake_server.py"))


def server(alias: str = "fake", *, deferred: bool = False, **env: str) -> McpServer:
    return McpServer(alias=alias, command=[sys.executable, FAKE], env=env, deferred=deferred)


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


def runtime(store: SqliteStore, provider: ScriptedProvider, *agents: Agent) -> Runtime:
    return Runtime(store=store, agents={a.name: a for a in agents},
                   provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                   sleep=_no_sleep)


async def events(client: Client, tid: str) -> list[Any]:
    return [r.event for r in await client.events(tid)]


def finished(evs: list[Any]) -> list[ToolFinished]:
    return [e for e in evs if isinstance(e, ToolFinished)]


def records(evs: list[Any], kind: str) -> list[ExtensionRecorded]:
    return [e for e in evs if isinstance(e, ExtensionRecorded) and e.extension == "mcp"
            and e.kind == kind]


def tool_names(request: ModelRequest) -> list[str]:
    return [t.name for t in request.tools]


async def test_server_tools_are_offered_called_and_recorded_once(
        store: SqliteStore, pool: McpPool) -> None:
    provider = ScriptedProvider([
        reply("", ("mcp__fake__echo", {"text": "hi"}), ("mcp__fake__fail", {}),
              ("mcp__fake__image", {})),
        reply("done"),
        reply("again"),
    ])
    agent = Agent(name="a", model="m", extensions=[Mcp([server()], pool=pool)])
    rt = runtime(store, provider, agent)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    first = provider.requests[0]
    assert tool_names(first) == ["mcp__fake__die", "mcp__fake__echo", "mcp__fake__fail",
                                 "mcp__fake__image", "mcp__fake__sleep"]
    echo = next(t for t in first.tools if t.name == "mcp__fake__echo")
    assert echo.description == "[MCP server 'fake'] Echo the text back."
    evs = await events(client, tid)
    echo_r, fail_r, image_r = (f.result for f in finished(evs))
    body = json.loads(echo_r.text)
    assert body["text"] == "echo: hi" and "pid" in body["structured_content"]
    assert fail_r.is_error and json.loads(fail_r.text)["text"] == "it broke"
    assert json.loads(image_r.text) == {"text": "[audio (audio/wav, 12 bytes): not shown]",
                                        "non_text_blocks": 2}
    assert any(isinstance(b, Image) for b in image_r.content)
    # A second turn with the same tools records nothing new; the prefix holds.
    await client.send(tid, "more")
    await rt.run_until_idle()
    assert len(records(await events(client, tid), "listed")) == 1
    assert provider.requests[2].tools == first.tools


async def test_the_tool_set_changes_only_at_the_next_turn(
        store: SqliteStore, pool: McpPool, tmp_path: Path) -> None:
    tools_file = tmp_path / "tools.json"
    tools_file.write_text(json.dumps([{"name": "a", "inputSchema": {"type": "object"}}]))
    provider = ScriptedProvider([reply("one"), reply("two")])
    agent = Agent(name="a", model="m", extensions=[
        Mcp([server(FAKE_TOOLS_FILE=str(tools_file))], pool=pool)])
    rt = runtime(store, provider, agent)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    tools_file.write_text(json.dumps([{"name": "a", "inputSchema": {"type": "object"}},
                                      {"name": "b", "inputSchema": {"type": "object"}}]))
    await client.send(tid, "again")
    await rt.run_until_idle()
    assert tool_names(provider.requests[0]) == ["mcp__fake__a"]
    assert tool_names(provider.requests[1]) == ["mcp__fake__a", "mcp__fake__b"]
    assert len(records(await events(client, tid), "listed")) == 2


class AskForMcp(Extension):
    name = "asker"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Ask(prompt=f"run {call.name}?") if call.name.startswith("mcp__") else None


async def test_a_task_resumed_in_a_fresh_process_keeps_its_mcp_tools(
        store: SqliteStore, tmp_path: Path) -> None:
    """The 0.6 P0: an approval pending across a restart died on an unknown
    tool. The tool list comes from the log and the server list from the
    task's config, so a new process (new extension, new pool) carries on."""
    def servers(view: TaskView) -> list[McpServer]:
        return [server(view.state.config["mcp_alias"])]

    provider = ScriptedProvider([reply("", ("mcp__fake__echo", {"text": "x"}))])
    pool1 = McpPool()
    agent1 = Agent(name="a", model="m", extensions=[Mcp(servers, pool=pool1), AskForMcp()])
    client = Client(store)
    tid = await client.create("a", "go", config={"mcp_alias": "fake"})
    await runtime(store, provider, agent1).run_until_idle()
    waiting = (await events(client, tid))[-1]
    assert isinstance(waiting, Waiting) and waiting.on == "approval"
    await pool1.close()  # the first process is gone

    provider2 = ScriptedProvider([reply("ok")])
    pool2 = McpPool()
    agent2 = Agent(name="a", model="m", extensions=[Mcp(servers, pool=pool2), AskForMcp()])
    await client.answer(tid, waiting.ids[0], decision="allow")
    try:
        await runtime(store, provider2, agent2).run_until_idle()
    finally:
        await pool2.close()
    result = finished(await events(client, tid))[0].result
    assert not result.is_error and json.loads(result.text)["text"] == "echo: x"
    assert "mcp__fake__echo" in tool_names(provider2.requests[0])


async def test_deferred_servers_are_reached_through_toolsearch_and_mcpcall(
        store: SqliteStore, pool: McpPool) -> None:
    provider = ScriptedProvider([
        reply("", ("ToolSearch", {"query": "echo text"}), ("ToolSearch", {"query": ""})),
        reply("", ("McpCall", {"tool": "mcp__fake__echo", "arguments": {"text": "via"}}),
              ("McpCall", {"tool": "mcp__fake__nope", "arguments": {}}),
              ("McpCall", {"tool": "mcp__fake__echo"})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m", extensions=[Mcp([server(deferred=True)], pool=pool)])
    client = Client(store)
    tid = await client.create("a", "go")
    await runtime(store, provider, agent).run_until_idle()
    assert tool_names(provider.requests[0]) == ["ToolSearch", "McpCall"]
    evs = await events(client, tid)
    by_id = {f.result.call_id: f.result for f in finished(evs)}
    ids = [u.id for m in provider.requests[2].messages for u in m.tool_uses]
    search, listing, routed, unknown, no_args = (by_id[i] for i in ids)
    [hit] = json.loads(search.text)
    assert hit["name"] == "mcp__fake__echo" and hit["input_schema"]["type"] == "object"
    assert listing.text.splitlines()[0] == "mcp__fake__die — [MCP server 'fake'] Exits the server."
    assert json.loads(routed.text)["text"] == "echo: via"
    started = [e.call for e in evs if isinstance(e, ToolStarted)]
    assert started[-1].name == "mcp__fake__echo" and started[-1].id == ids[2]
    assert unknown.is_error and unknown.text == (
        "'mcp__fake__nope' is not a deferred tool. If it is in your tool list, call it "
        "directly; otherwise find the right name with ToolSearch.")
    assert no_args.is_error and "McpCall needs `arguments`" in no_args.text


class DenyEcho(Extension):
    name = "guard"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Deny(reason="no echo") if call.name == "mcp__fake__echo" else None


async def test_guards_see_the_real_tool_behind_mcpcall(
        store: SqliteStore, pool: McpPool) -> None:
    provider = ScriptedProvider([
        reply("", ("McpCall", {"tool": "mcp__fake__echo", "arguments": {"text": "x"}})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m",
                  extensions=[Mcp([server(deferred=True)], pool=pool), DenyEcho()])
    client = Client(store)
    tid = await client.create("a", "go")
    await runtime(store, provider, agent).run_until_idle()
    [result] = [f.result for f in finished(await events(client, tid))]
    assert result.is_error and result.text == "no echo"


async def test_an_approved_mcpcall_runs_the_real_tool(store: SqliteStore, pool: McpPool) -> None:
    provider = ScriptedProvider([
        reply("", ("McpCall", {"tool": "mcp__fake__echo", "arguments": {"text": "y"}})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m",
                  extensions=[Mcp([server(deferred=True)], pool=pool), AskForMcp()])
    client = Client(store)
    tid = await client.create("a", "go")
    rt = runtime(store, provider, agent)
    await rt.run_until_idle()
    waiting = (await events(client, tid))[-1]
    assert isinstance(waiting, Waiting) and "mcp__fake__echo" in str(waiting.detail)
    await client.answer(tid, waiting.ids[0], decision="allow")
    await rt.run_until_idle()
    [result] = [f.result for f in finished(await events(client, tid))]
    assert json.loads(result.text)["text"] == "echo: y"


async def test_a_dead_server_is_dropped_and_reported_once(
        store: SqliteStore, pool: McpPool) -> None:
    dead = McpServer(alias="dead", command=["/no/such/binary"], deferred=True)
    provider = ScriptedProvider([reply("one"), reply("two")])
    agent = Agent(name="a", model="m", extensions=[Mcp([dead, server()], pool=pool)])
    client = Client(store)
    tid = await client.create("a", "go")
    rt = runtime(store, provider, agent)
    await rt.run_until_idle()
    await client.send(tid, "again")
    await rt.run_until_idle()
    names = tool_names(provider.requests[0])
    assert "ToolSearch" in names and "McpCall" in names  # the prefix does not move
    assert not any(n.startswith("mcp__dead__") for n in names)
    downs = records(await events(client, tid), "server_down")
    assert len(downs) == 1 and downs[0].data["alias"] == "dead"
    assert "spawn failed" in downs[0].data["reason"]


async def test_scopes_partition_connections(store: SqliteStore, pool: McpPool) -> None:
    def make(scope: str) -> Agent:
        return Agent(name=f"a-{scope}", model="m",
                     extensions=[Mcp([server()], pool=pool,
                                     scope=lambda v: v.state.config.get("tenant"))])

    provider = ScriptedProvider([reply("", ("mcp__fake__echo", {})), reply("ok")] * 3)
    rt = runtime(store, provider, make("x"))
    client = Client(store)
    pids = []
    for tenant in ("t1", "t1", "t2"):
        tid = await client.create("a-x", "go", config={"tenant": tenant})
        await rt.run_until_idle()
        result = finished(await events(client, tid))[0].result
        pids.append(json.loads(result.text)["structured_content"]["pid"])
    assert pids[0] == pids[1] != pids[2]


async def test_a_cross_server_name_clash_drops_the_second_server(
        store: SqliteStore, pool: McpPool) -> None:
    clash = json.dumps([{"name": "b__t", "inputSchema": {"type": "object"}}])
    other = json.dumps([{"name": "t", "inputSchema": {"type": "object"}}])
    provider = ScriptedProvider([reply("hi")])
    agent = Agent(name="a", model="m", extensions=[Mcp(
        [server("a", FAKE_TOOLS=clash), server("a__b", FAKE_TOOLS=other)], pool=pool)])
    client = Client(store)
    tid = await client.create("a", "go")
    await runtime(store, provider, agent).run_until_idle()
    assert tool_names(provider.requests[0]) == ["mcp__a__b__t"]
    [down] = records(await events(client, tid), "server_down")
    assert down.data["alias"] == "a__b" and "rename one alias" in down.data["reason"]


# ---------------------------------------------------------------- naming


def test_tool_names_are_provider_safe_and_stable() -> None:
    assert make_tool_name("gh", "list.issues") == "mcp__gh__list_issues"
    long = make_tool_name("gh", "x" * 80)
    assert len(long) == 64 and long.startswith("mcp__gh__x")
    assert long == make_tool_name("gh", "x" * 80)
    names = map_server_names("gh", ["a.b", "a_b", "a-b"])
    assert names["a_b"] == "mcp__gh__a_b"  # the already-safe one keeps its name
    assert names["a.b"] != names["a_b"] and names["a.b"].startswith("mcp__gh__a_b_")
    assert len(set(names.values())) == 3


def test_descriptions_are_attributed_and_capped() -> None:
    assert describe_tool("s", None) == "[MCP server 's']"
    text = describe_tool("s", "é" * 2000)
    assert text.endswith("[truncated: description exceeded 1024 bytes]")


def test_mcp_results_are_not_clearable() -> None:
    async def invoke(*_: Any) -> Any:
        raise AssertionError

    tool = McpTool(name="mcp__s__t", alias="s", remote="t", description="", input_schema={},
                   invoke=invoke)
    assert tool.clearable is False


async def test_a_remote_http_server_works_end_to_end(store: SqliteStore) -> None:
    import httpx

    from noeta.plugins.mcp.pool import connect

    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        seen.append(request.headers.get("Authorization"))
        if "id" not in body:
            return httpx.Response(202)
        result: dict[str, Any] = {
            "initialize": {},
            "tools/list": {"tools": [{"name": "search.docs", "description": "Search.",
                                      "inputSchema": {"type": "object"}}]},
            "tools/call": {"content": [{"type": "text", "text": "found"}]},
        }[body["method"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result},
                              headers={"Mcp-Session-Id": "abc"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pool = McpPool(connect=lambda s: connect(s, http=http))
    remote = McpServer(alias="docs", url="https://mcp.example/mcp",
                       headers={"Authorization": "Bearer secret"})
    provider = ScriptedProvider([reply("", ("mcp__docs__search_docs", {})), reply("ok")])
    agent = Agent(name="a", model="m", extensions=[Mcp([remote], pool=pool)])
    client = Client(store)
    tid = await client.create("a", "go")
    try:
        await runtime(store, provider, agent).run_until_idle()
    finally:
        await pool.close()
    [result] = [f.result for f in finished(await events(client, tid))]
    assert json.loads(result.text) == {"text": "found"}
    assert set(seen) == {"Bearer secret"}
    # Credentials stay host-side: nothing in the log carries them.
    assert "secret" not in str(await client.events(tid))


async def test_permissions_ask_about_the_real_tool_behind_mcpcall(
        store: SqliteStore, pool: McpPool) -> None:
    from noeta.plugins.permissions import Permissions

    provider = ScriptedProvider([
        reply("", ("McpCall", {"tool": "mcp__fake__echo", "arguments": {"text": "p"}}),
              ("ToolSearch", {"query": "echo"})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m",
                  extensions=[Mcp([server(deferred=True)], pool=pool), Permissions()])
    client = Client(store)
    tid = await client.create("a", "go")
    rt = runtime(store, provider, agent)
    await rt.run_until_idle()
    waiting = (await events(client, tid))[-1]
    # ToolSearch (low risk) ran; the routed call waits as the real, high-risk tool.
    assert isinstance(waiting, Waiting) and waiting.on == "approval"
    assert "mcp__fake__echo" in str(waiting.detail) and len(waiting.ids) == 1
    await client.answer(tid, waiting.ids[0], decision="allow")
    await rt.run_until_idle()
    results = [f.result for f in finished(await events(client, tid))]
    assert json.loads(results[-1].text)["text"] == "echo: p"
