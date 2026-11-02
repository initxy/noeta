"""The ``Mcp`` extension end to end: a scripted model, a real sqlite store, a
real stdio server."""

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
from noeta.core.events import ExtensionRecorded, ToolFinished, Waiting
from noeta.core.extension import Ask, Deny, Extension, Gate, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.runner import Runtime
from noeta.core.types import Image, ModelRequest, ToolUse
from noeta.plugins.mcp import Mcp, McpPool, McpServer
from noeta.plugins.mcp.config import describe_tool, make_tool_name, map_server_names
from noeta.plugins.mcp.tools import McpTool
from noeta.plugins.tool_search import ToolSearch
from noeta.testing import ScriptedProvider, reply

FAKE = str(Path(__file__).with_name("fake_server.py"))


def server(alias: str = "fake", *, deferred: bool = False, **env: str) -> McpServer:
    """A test server; its tools are sent in full unless ``deferred``."""
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
    """The results in call order (one reply's calls run concurrently and may
    finish in any order; ``reply`` numbers its call ids in order)."""
    return sorted((e for e in evs if isinstance(e, ToolFinished)),
                  key=lambda e: int(e.result.call_id.removeprefix("call_")))


def records(evs: list[Any], kind: str) -> list[ExtensionRecorded]:
    return [e for e in evs if isinstance(e, ExtensionRecorded) and e.extension == "mcp"
            and e.kind == kind]


def tool_names(request: ModelRequest) -> list[str]:
    """The server tools a request sends."""
    return [t.name for t in request.tools if t.name.startswith("mcp__")]


def texts(result: Any) -> list[str]:
    return [b.text for b in result.content if not isinstance(b, Image)]


async def test_server_tools_are_offered_called_and_recorded_once(
        store: SqliteStore, pool: McpPool) -> None:
    provider = ScriptedProvider([
        reply("", ("mcp__fake__echo", {"text": "hi"}), ("mcp__fake__fail", {}),
              ("mcp__fake__image", {})),
        reply("done"),
        reply("again"),
    ])
    agent = Agent(name="a", model="m", extensions=[Mcp([server()], pool=pool), ToolSearch()])
    rt = runtime(store, provider, agent)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    first = provider.requests[0]
    assert tool_names(first) == ["mcp__fake__die", "mcp__fake__echo", "mcp__fake__fail",
                                 "mcp__fake__image", "mcp__fake__pid", "mcp__fake__sleep"]
    echo = next(t for t in first.tools if t.name == "mcp__fake__echo")
    assert echo.description == "Echo the text back." and not echo.defer_loading
    # Server tools come last, the placeholder second to last (before the
    # last server tool); the resource tools stay deferred.
    sent = [t.name for t in first.tools]
    assert sent[0] == "ToolSearch" and sent[-2] == "DeferredToolPlaceholder"
    assert all(n.startswith("mcp__") for n in sent[1:-2] + sent[-1:])
    evs = await events(client, tid)
    echo_r, fail_r, image_r = (f.result for f in finished(evs))
    assert texts(echo_r) == ["echo: hi"]
    assert fail_r.is_error and texts(fail_r) == ["it broke"]
    assert texts(image_r) == ["[audio (audio/wav, 12 bytes): not shown]"]
    assert isinstance(image_r.content[0], Image)
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
    assert not result.is_error and texts(result) == ["echo: x"]
    assert "mcp__fake__echo" in tool_names(provider2.requests[0])


async def test_deferred_server_tools_are_loaded_with_toolsearch_then_called_directly(
        store: SqliteStore, pool: McpPool) -> None:
    provider = ScriptedProvider([
        reply("", ("mcp__fake__echo", {"text": "early"})),
        reply("", ("ToolSearch", {"query": "select:mcp__fake__echo,ListMcpResourcesTool,"
                                           "ReadMcpResourceTool,ReadMcpResourceDirTool",
                                  "max_results": 4})),
        reply("", ("mcp__fake__echo", {"text": "via"}),
              ("ReadMcpResourceTool", {"server": "fake", "uri": "file:///notes.md"}),
              ("ListMcpResourcesTool", {}),
              ("ReadMcpResourceDirTool", {"server": "fake", "uri": "file:///docs"}),
              ("ReadMcpResourceDirTool", {"server": "fake", "uri": "file:///notes.md"})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m", extensions=[Mcp([server(deferred=True)], pool=pool),
                                                   ToolSearch()])
    client = Client(store)
    tid = await client.create("a", "go")
    await runtime(store, provider, agent).run_until_idle()
    names = [t.name for t in provider.requests[0].tools]
    assert names == ["DeferredToolPlaceholder", "ToolSearch"]
    loaded = provider.requests[2].tools
    assert [(t.name, t.defer_loading) for t in loaded] == [
        ("ListMcpResourcesTool", True), ("ReadMcpResourceDirTool", True),
        ("ReadMcpResourceTool", True), ("ToolSearch", False),
        ("DeferredToolPlaceholder", True), ("mcp__fake__echo", True)]
    early, search, echo, read, listing, folder, not_folder = (
        f.result for f in finished(await events(client, tid)))
    assert folder.text == ("2 entries in file:///docs:\na.md  file:///docs/a.md\n"
                           "sub/  file:///docs/sub")
    assert not_folder.is_error and "could not list" in not_folder.text
    assert early.is_error and early.text.startswith(
        "InputValidationError: mcp__fake__echo is a deferred tool")
    assert [b.tool_name for b in search.content] == [  # type: ignore[union-attr]
        "mcp__fake__echo", "ListMcpResourcesTool", "ReadMcpResourceTool",
        "ReadMcpResourceDirTool"]
    after = provider.requests[2].messages[-1]
    assert after.text == "Tool loaded."
    assert texts(echo) == ["echo: via"]
    assert read.text == '{"contents":[{"uri":"file:///notes.md","text":"the notes"}]}'
    assert json.loads(listing.text) == [{"uri": "file:///notes.md", "name": "notes",
                                         "server": "fake"}]


class DenyEcho(Extension):
    name = "guard"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Deny(reason="no echo") if call.name == "mcp__fake__echo" else None


async def test_guards_see_server_tool_calls(store: SqliteStore, pool: McpPool) -> None:
    provider = ScriptedProvider([
        reply("", ("mcp__fake__echo", {"text": "x"})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m", extensions=[Mcp([server()], pool=pool), DenyEcho()])
    client = Client(store)
    tid = await client.create("a", "go")
    await runtime(store, provider, agent).run_until_idle()
    [result] = [f.result for f in finished(await events(client, tid))]
    assert result.is_error and result.text == "no echo"


async def test_an_approved_server_tool_call_runs(store: SqliteStore, pool: McpPool) -> None:
    provider = ScriptedProvider([
        reply("", ("mcp__fake__echo", {"text": "y"})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m", extensions=[Mcp([server()], pool=pool), AskForMcp()])
    client = Client(store)
    tid = await client.create("a", "go")
    rt = runtime(store, provider, agent)
    await rt.run_until_idle()
    waiting = (await events(client, tid))[-1]
    assert isinstance(waiting, Waiting) and "mcp__fake__echo" in str(waiting.detail)
    await client.answer(tid, waiting.ids[0], decision="allow")
    await rt.run_until_idle()
    [result] = [f.result for f in finished(await events(client, tid))]
    assert texts(result) == ["echo: y"]


async def test_a_dead_server_is_dropped_and_reported_once(
        store: SqliteStore, pool: McpPool) -> None:
    dead = McpServer(alias="dead", command=["/no/such/binary"], deferred=True)
    provider = ScriptedProvider([reply("one"), reply("two")])
    agent = Agent(name="a", model="m", extensions=[Mcp([dead, server()], pool=pool),
                                                   ToolSearch()])
    client = Client(store)
    tid = await client.create("a", "go")
    rt = runtime(store, provider, agent)
    await rt.run_until_idle()
    await client.send(tid, "again")
    await rt.run_until_idle()
    names = [t.name for t in provider.requests[0].tools]
    assert "ToolSearch" in names  # the prefix does not move
    assert not any(n.startswith("mcp__dead__") for n in names)
    downs = records(await events(client, tid), "server_down")
    assert len(downs) == 1 and downs[0].data["alias"] == "dead"
    assert "spawn failed" in downs[0].data["reason"]


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


def test_descriptions_are_the_servers_own_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    assert describe_tool(None) == ("", "")
    assert describe_tool("Echo.") == ("Echo.", "Echo.")
    listed, loaded = describe_tool("é" * 5000)
    assert listed == "é" * 4096 + "… [truncated]"  # characters, not bytes
    assert loaded == "é" * 5000
    listed, loaded = describe_tool("x" * 20_000)
    assert loaded == "x" * 16_384 + "… [truncated]"
    monkeypatch.setenv("NOETA_MAX_MCP_DESCRIPTION_LENGTH", "10")
    assert describe_tool("y" * 50) == ("y" * 10 + "… [truncated]",) * 2
    monkeypatch.setenv("NOETA_MAX_MCP_DESCRIPTION_LENGTH", "nonsense")
    assert describe_tool("y" * 50) == ("y" * 50,) * 2


def test_a_long_description_is_recorded_in_both_forms() -> None:
    from noeta.plugins.mcp.config import McpServer
    from noeta.plugins.mcp.extension import _server_entry
    entry = _server_entry(McpServer(alias="s", command=["x"]),
                          [{"name": "big", "description": "d" * 5000},
                           {"name": "small", "description": "Echo."}])
    big, small = entry["tools"]
    assert big["description"].endswith("… [truncated]") and big["loaded_description"] == "d" * 5000
    assert "loaded_description" not in small


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
                       headers={"Authorization": "Bearer secret"}, deferred=False)
    provider = ScriptedProvider([reply("", ("mcp__docs__search_docs", {})), reply("ok")])
    agent = Agent(name="a", model="m", extensions=[Mcp([remote], pool=pool)])
    client = Client(store)
    tid = await client.create("a", "go")
    try:
        await runtime(store, provider, agent).run_until_idle()
    finally:
        await pool.close()
    [result] = [f.result for f in finished(await events(client, tid))]
    assert texts(result) == ["found"]
    assert set(seen) == {"Bearer secret"}
    # Credentials stay host-side: nothing in the log carries them.
    assert "secret" not in str(await client.events(tid))


async def test_permissions_ask_about_server_tools_not_toolsearch(
        store: SqliteStore, pool: McpPool) -> None:
    from noeta.plugins.permissions import Permissions

    provider = ScriptedProvider([
        reply("", ("ToolSearch", {"query": "select:mcp__fake__echo", "max_results": 1})),
        reply("", ("mcp__fake__echo", {"text": "p"})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m",
                  extensions=[Mcp([server(deferred=True)], pool=pool), Permissions(mode="default")])
    client = Client(store)
    tid = await client.create("a", "go")
    rt = runtime(store, provider, agent)
    await rt.run_until_idle()
    waiting = (await events(client, tid))[-1]
    # ToolSearch (low risk) ran; the server tool waits for approval.
    assert isinstance(waiting, Waiting) and waiting.on == "approval"
    assert "mcp__fake__echo" in str(waiting.detail) and len(waiting.ids) == 1
    await client.answer(tid, waiting.ids[0], decision="allow")
    await rt.run_until_idle()
    results = [f.result for f in finished(await events(client, tid))]
    assert texts(results[-1]) == ["echo: p"]


async def test_tasks_that_never_come_back_are_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    # A task the runner never lets go of here (taken by another worker, a
    # crash) leaves no event: its entries go once newer tasks crowd it out.
    monkeypatch.setattr("noeta.plugins.mcp.extension.TASKS_KEEP", 2)
    ext = Mcp([McpServer(alias="s", command=["true"])])
    for n in range(5):
        assert await ext._resolve(TaskView(state=TaskState(task_id=f"t{n}", config={"cwd": "/w"})))
    assert set(ext._known) == set(ext._scopes) == set(ext._roots) == {"t3", "t4"}


async def test_a_failed_resolution_leaves_no_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_a: object) -> list[McpServer]:
        raise OSError("unreadable settings")

    monkeypatch.setattr("noeta.plugins.mcp.extension.read_servers", broken)
    ext = Mcp([McpServer(alias="s", command=["true"])], settings_files=["/nowhere.json"])
    for n in range(5):
        view = TaskView(state=TaskState(task_id=f"t{n}", config={"cwd": "/w"}))
        assert not await ext._resolve(view)
    assert not ext._known and not ext._scopes and not ext._roots


def test_the_resource_tools_run_again_after_a_crash() -> None:
    """Reading resources changes nothing: each is retried, not answered
    "outcome unknown", when a crash cut it off."""
    from noeta.plugins.mcp.tools import (
        ListMcpResourcesTool,
        ReadMcpResourceDirTool,
        ReadMcpResourceTool,
    )

    for tool in (ListMcpResourcesTool, ReadMcpResourceTool, ReadMcpResourceDirTool):
        assert getattr(tool, "retry_safe", False) is True, tool


async def test_scopes_partition_connections(store: SqliteStore, pool: McpPool) -> None:
    def make(scope: str) -> Agent:
        return Agent(name=f"a-{scope}", model="m",
                     extensions=[Mcp([server()], pool=pool,
                                     scope=lambda v: v.state.config.get("user"))])

    provider = ScriptedProvider([reply("", ("mcp__fake__pid", {})), reply("ok")] * 3)
    rt = runtime(store, provider, make("x"))
    client = Client(store)
    pids = []
    for user in ("u1", "u1", "u2"):
        tid = await client.create("a-x", "go", config={"user": user})
        await rt.run_until_idle()
        result = finished(await events(client, tid))[0].result
        pids.append(result.text)
    assert pids[0] == pids[1] != pids[2]


async def test_server_instructions_are_told_and_withdrawn(
        store: SqliteStore, pool: McpPool) -> None:
    docs = server("docs", deferred=True, FAKE_INSTRUCTIONS="Call resolve before query.")
    late = server("late", deferred=True, FAKE_INSTRUCTIONS="Always pass a project id.")
    configured = [docs]
    provider = ScriptedProvider([reply("one"), reply("two"), reply("three"), reply("four")])
    agent = Agent(name="a", model="m", extensions=[
        Mcp(lambda _view: list(configured), pool=pool), ToolSearch()])
    client = Client(store)
    tid = await client.create("a", "go")
    await runtime(store, provider, agent).run_until_idle()
    configured.append(late)
    await client.send(tid, "more")
    await runtime(store, provider, agent).run_until_idle()
    configured.remove(docs)
    await client.send(tid, "again")
    await runtime(store, provider, agent).run_until_idle()
    await client.send(tid, "quiet")
    await runtime(store, provider, agent).run_until_idle()

    def notes(i: int) -> list[str]:
        return [m.text for m in provider.requests[i].messages
                if m.role == "system" and "MCP server" in m.text]

    assert notes(0) == ["# MCP Server Instructions\nThese MCP servers gave instructions for "
                        "using their tools and resources:\n## docs\nCall resolve before query."]
    assert notes(1)[-1].endswith("## late\nAlways pass a project id.")
    assert notes(2)[-1].startswith("These MCP servers disconnected; their instructions above "
                                   "no longer apply:\ndocs\nThis is background information")
    assert notes(3) == notes(2)


def test_long_server_instructions_are_cut() -> None:
    from noeta.plugins.mcp.instructions import cap_instructions
    assert cap_instructions("short") == "short"
    cut = cap_instructions("x" * 5000)
    assert cut == "x" * 4096 + "… [truncated]"
