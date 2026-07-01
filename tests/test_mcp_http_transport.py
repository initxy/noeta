"""Remote HTTP MCP transport + host-side config + by-alias enable.

Covers the issue's acceptance criteria:

* the SDK has a SYNCHRONOUS HTTP MCP client (``initialize`` / ``tools/list`` /
  ``tools/call``) — request-response only, no server-push / SSE long connection
  (the one SSE shape we accept is a one-shot ``data:`` response, still
  request-response);
* ``build_mcp_tools`` wraps an ``McpHttpServerSpec`` with the SAME
  ``mcp__alias__tool`` naming / collision / ``risk_level=high``
  + R-1 the F2 stdio path uses;
* the host-side config store (``McpServerRegistry``) persists credentials and
  NEVER echoes them — a chat/task request body carries only enabled aliases;
* the management API (``parse_*`` are unaffected; the registry CRUD is exercised
  directly + via the resolver injected into the SdkHost);
* end-to-end: a task with ``enabled_mcp=["remote"]`` connects the (fake) HTTP
  server and the model actually calls its tool; the recording carries the call;
* replay reads the recorded result and NEVER reconnects (a no-post sentinel).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from noeta.agent.host.mcp_registry import McpServerRegistry
from noeta.client import SdkHost
from noeta.execution.driver import InteractionDriver
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode
from noeta.tools.mcp import (
    McpConfigError,
    McpError,
    McpHttpClient,
    McpHttpServerSpec,
    build_mcp_tools,
    parse_mcp_tool_specs,
)
from noeta.tools.mcp.tool import McpTool
from tests._fixtures.fake_http_mcp_server import FakeHttpMcpServer
from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    official_registry,
    runner_main_spec,
)


# ---------------------------------------------------------------------------
# 1. McpHttpClient — synchronous request-response, header injection, caps
# ---------------------------------------------------------------------------


def _client(server: FakeHttpMcpServer, **kw: Any) -> McpHttpClient:
    return McpHttpClient(
        url="https://example.test/mcp",
        headers={"Authorization": "Bearer secret-token"},
        post=server.post,
        **kw,
    )


def test_http_client_handshake_list_call() -> None:
    server = FakeHttpMcpServer(mode="echo")
    c = _client(server)
    c.start()
    tools = c.list_tools()
    assert [t["name"] for t in tools] == ["echo"]
    result = c.call_tool("echo", {"msg": "hi"})
    text = result["content"][0]["text"]
    assert json.loads(text) == {"msg": "hi"}
    # Every request carried the injected credential header on the wire.
    assert all(
        h.get("Authorization") == "Bearer secret-token" for h in server.seen_headers
    )


def test_http_client_sse_response_is_parsed() -> None:
    # An SSE (text/event-stream) one-shot body is still request-response: the
    # client reads the single matching JSON-RPC object and stops.
    server = FakeHttpMcpServer(mode="sse")
    c = _client(server)
    c.start()
    result = c.call_tool("echo", {"a": 1})
    assert json.loads(result["content"][0]["text"]) == {"a": "1"} or json.loads(
        result["content"][0]["text"]
    ) == {"a": 1}


def test_http_client_transport_fault_raises_mcp_error() -> None:
    server = FakeHttpMcpServer(mode="boom")
    c = _client(server)
    with pytest.raises(McpError):
        c.start()


def test_http_client_total_cap_enforced() -> None:
    server = FakeHttpMcpServer(mode="echo")
    c = _client(server, total_cap=4)  # tiny cap → any real response trips it
    with pytest.raises(McpError):
        c.start()


def test_http_client_empty_url_rejected() -> None:
    with pytest.raises(McpError):
        McpHttpClient(url="")


# ---------------------------------------------------------------------------
# 2. build_mcp_tools wraps an HTTP spec like the stdio path (naming / R-1)
# ---------------------------------------------------------------------------


def test_build_mcp_tools_http_spec_wrapping() -> None:
    server = FakeHttpMcpServer(mode="echo")
    spec = McpHttpServerSpec(alias="remote", url="https://x/mcp")
    tools, clients, _skipped = build_mcp_tools((spec,), http_post=server.post)
    try:
        assert list(tools) == ["mcp__remote__echo"]
        t = tools["mcp__remote__echo"]
        assert isinstance(t, McpTool)
        assert t.risk_level == "high"
    finally:
        for c in clients:
            c.shutdown()


def test_http_spec_bad_alias_and_empty_url_fail_fast() -> None:
    with pytest.raises(McpConfigError):
        McpHttpServerSpec(alias="BadAlias", url="https://x")
    with pytest.raises(McpConfigError):
        McpHttpServerSpec(alias="ok", url="")


def test_credentials_never_in_recorded_request_tools() -> None:
    # The wrapped tool's schema (what rides into the recording) carries no token.
    server = FakeHttpMcpServer(mode="echo")
    spec = McpHttpServerSpec(
        alias="remote",
        url="https://x/mcp",
        headers=(("Authorization", "Bearer secret-token"),),
    )
    tools, clients, _skipped = build_mcp_tools((spec,), http_post=server.post)
    try:
        blob = json.dumps(
            {
                n: {"schema": t.input_schema, "desc": t.description}
                for n, t in tools.items()
            }
        )
        assert "secret-token" not in blob
    finally:
        for c in clients:
            c.shutdown()


# ---------------------------------------------------------------------------
# 3. McpServerRegistry — host-side store, credential scrubbing, resolve
# ---------------------------------------------------------------------------


def test_registry_persist_and_scrub(tmp_path: Path) -> None:
    path = tmp_path / "mcp_servers.json"
    reg = McpServerRegistry(path)
    reg.upsert_http(
        alias="github",
        url="https://mcp.example/github",
        headers={"Authorization": "Bearer ghp_secret"},
    )
    # Persisted file holds the credential (host-side only).
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["github"]["headers"]["Authorization"] == "Bearer ghp_secret"
    # The public view (management API / capabilities) scrubs the value.
    pub = reg.get("github").as_public_dict()
    assert pub["alias"] == "github"
    assert pub["url"] == "https://mcp.example/github"
    assert pub["header_names"] == ["Authorization"]
    assert "Bearer ghp_secret" not in json.dumps(pub)
    # Reloads from disk.
    reg2 = McpServerRegistry(path)
    reg2.load()
    assert reg2.get("github").url == "https://mcp.example/github"
    assert reg2.delete("github") is True
    assert reg2.delete("github") is False


def test_registry_resolve_spec(tmp_path: Path) -> None:
    reg = McpServerRegistry(tmp_path / "m.json")
    reg.upsert_http(alias="remote", url="https://x/mcp", headers={"X-Key": "k"})
    spec = reg.resolve_spec("remote")
    assert isinstance(spec, McpHttpServerSpec)
    assert spec.url == "https://x/mcp"
    assert spec.headers_dict() == {"X-Key": "k"}
    assert reg.resolve_spec("unknown") is None


def test_registry_rejects_bad_alias(tmp_path: Path) -> None:
    reg = McpServerRegistry(tmp_path / "m.json")
    with pytest.raises(McpConfigError):
        reg.upsert_http(alias="Bad Alias", url="https://x")


# ---------------------------------------------------------------------------
# 3b. (issue 02) — stdio entries + per-server tool subset
# ---------------------------------------------------------------------------


def test_registry_upsert_stdio_persists_and_resolves(tmp_path: Path) -> None:
    from noeta.tools.mcp import McpServerSpec

    path = tmp_path / "m.json"
    reg = McpServerRegistry(path)
    reg.upsert_stdio(
        alias="fs",
        command="my-server",
        args=["--root", "/tmp"],
        env={"TOKEN": "secret-env"},
    )
    # Resolves to a stdio spec: argv = command + args, env carried.
    spec = reg.resolve_spec("fs")
    assert isinstance(spec, McpServerSpec)
    assert spec.argv == ("my-server", "--root", "/tmp")
    assert spec.env_dict() == {"TOKEN": "secret-env"}
    # The public view scrubs env VALUES to names only (an env var may be a secret).
    pub = reg.get("fs").as_public_dict()
    assert pub["type"] == "stdio"
    assert pub["command"] == "my-server"
    assert pub["args"] == ["--root", "/tmp"]
    assert pub["env_names"] == ["TOKEN"]
    assert "secret-env" not in json.dumps(pub)
    # Reloads from disk with the env preserved (host-side only).
    reg2 = McpServerRegistry(path)
    reg2.load()
    assert reg2.resolve_spec("fs").env_dict() == {"TOKEN": "secret-env"}


def test_registry_tool_subset_persists_and_rides_into_spec(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    reg = McpServerRegistry(path)
    reg.upsert_http(
        alias="remote", url="https://x/mcp", tools=["alpha", "gamma"]
    )
    spec = reg.resolve_spec("remote")
    assert spec.tool_subset == ("alpha", "gamma")
    # None subset (the default) round-trips as None (keep all).
    reg.upsert_http(alias="all", url="https://y/mcp")
    assert reg.resolve_spec("all").tool_subset is None
    # set_tools replaces the subset on an existing entry without re-posting.
    reg.set_tools("remote", ["beta"])
    assert reg.resolve_spec("remote").tool_subset == ("beta",)
    reg.set_tools("remote", None)  # clear → keep all
    assert reg.resolve_spec("remote").tool_subset is None
    assert reg.set_tools("never-configured", ["x"]) is None
    # Subset persists across reload.
    reg.set_tools("all", ["alpha"])
    reg2 = McpServerRegistry(path)
    reg2.load()
    assert reg2.resolve_spec("all").tool_subset == ("alpha",)


def test_registry_discover_tools_lists_full_menu_ignoring_subset(
    tmp_path: Path,
) -> None:
    # discover_tools must show EVERY advertised tool (not just the ticked ones)
    # so the UI can present the menu to pick from.
    server = FakeHttpMcpServer(mode="multi")
    reg = McpServerRegistry(tmp_path / "m.json", http_post=server.post)
    reg.upsert_http(alias="remote", url="https://x/mcp", tools=["alpha"])
    menu = reg.discover_tools("remote")
    assert [t["name"] for t in menu] == ["alpha", "beta", "gamma"]
    assert reg.discover_tools("unknown") is None


# ---------------------------------------------------------------------------
# 4a. By-alias enable end-to-end through the SdkHost + driver
# ---------------------------------------------------------------------------


def _call(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end() -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="done")],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _mcp_responses() -> list[LLMResponse]:
    return [_call("c1", "mcp__remote__echo", {"msg": "hi"}), _end()]


def _host_with_mcp(
    workspace: Path,
    *,
    responses: list[LLMResponse],
    server: FakeHttpMcpServer,
    registry: McpServerRegistry,
) -> tuple[SdkHost, InMemoryEventLog, InMemoryContentStore]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        workspace_dir=workspace,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
        registry=official_registry(),
        aliases={"default": "main"},
        # The alias→spec resolver (the agent-layer store) + the
        # injectable HTTP transport (the fake server, so no real network).
        mcp_server_resolver=registry.resolve_spec,
        mcp_http_post=server.post,
    )
    return host, event_log, content_store


def test_e2e_enable_by_alias_calls_remote_tool(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    server = FakeHttpMcpServer(mode="echo")
    registry = McpServerRegistry(tmp_path / "m.json")
    registry.upsert_http(
        alias="remote",
        url="https://x/mcp",
        headers={"Authorization": "Bearer secret-token"},
    )
    host, event_log, content_store = _host_with_mcp(
        ws, responses=_mcp_responses(), server=server, registry=registry
    )
    driver = InteractionDriver(host)

    # The request carries ONLY the enabled alias — no url, no token.
    started = driver.start(
        goal="use the remote mcp tool", agent="main", enabled_mcp=("remote",)
    )
    assert started.status in ("terminal", "suspended")

    events = event_log.read(started.task_id)
    starts = [
        e for e in events
        if e.type == "ToolCallStarted"
        and e.payload.tool_name == "mcp__remote__echo"
    ]
    assert len(starts) == 1
    assert any(e.type == "ToolResultRecorded" and e.payload.success for e in events)
    # The credential rode on the wire to the fake server, never elsewhere.
    assert any(
        h.get("Authorization") == "Bearer secret-token" for h in server.seen_headers
    )
    # The recorded request tools carry the mcp tool BUT no credential.
    first_req = next(e for e in events if e.type == "LLMRequestStarted")
    body = content_store.get(first_req.payload.request_ref).decode("utf-8")
    assert "mcp__remote__echo" in body
    assert "secret-token" not in body


def test_e2e_no_enabled_mcp_has_no_remote_tool(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    server = FakeHttpMcpServer(mode="echo")
    registry = McpServerRegistry(tmp_path / "m.json")
    registry.upsert_http(alias="remote", url="https://x/mcp")
    host, event_log, content_store = _host_with_mcp(
        ws,
        responses=[_call("c1", "glob", {"pattern": "*"}), _end()],
        server=server,
        registry=registry,
    )
    driver = InteractionDriver(host)
    # No enabled_mcp ⇒ no mcp__ tool in the recorded request, no connection.
    started = driver.start(goal="no mcp", agent="main")
    first_req = next(
        e for e in event_log.read(started.task_id) if e.type == "LLMRequestStarted"
    )
    tools = json.loads(
        content_store.get(first_req.payload.request_ref).decode("utf-8")
    ).get("tools", [])
    assert not any(
        t.get("function", {}).get("name", "").startswith("mcp__") for t in tools
    )
    assert server.seen_headers == []  # never connected


def test_e2e_tool_subset_only_ticked_tools_reach_the_model(tmp_path: Path) -> None:
    # With a stored subset, only the ticked tools enter the schema
    # the model sees — the request body still carries only the alias (D3).
    ws = tmp_path / "ws"
    ws.mkdir()
    server = FakeHttpMcpServer(mode="multi")
    registry = McpServerRegistry(tmp_path / "m.json")
    registry.upsert_http(
        alias="remote", url="https://x/mcp", tools=["alpha", "gamma"]
    )
    host, event_log, content_store = _host_with_mcp(
        ws, responses=[_end()], server=server, registry=registry
    )
    driver = InteractionDriver(host)
    started = driver.start(
        goal="subset", agent="main", enabled_mcp=("remote",)
    )
    first_req = next(
        e for e in event_log.read(started.task_id) if e.type == "LLMRequestStarted"
    )
    body = content_store.get(first_req.payload.request_ref).decode("utf-8")
    tools = json.loads(body).get("tools", [])
    mcp_names = {
        t.get("function", {}).get("name", "")
        for t in tools
        if t.get("function", {}).get("name", "").startswith("mcp__")
    }
    assert mcp_names == {"mcp__remote__alpha", "mcp__remote__gamma"}
    assert "mcp__remote__beta" not in mcp_names
    # The request body carries the alias, never the tool-name subset (D3).
    assert "remote" in body  # the alias does ride (as a tool namespace)
    # The MCP ``tool_subset`` config field must not leak onto the wire. Match
    # the quoted JSON key, not a bare substring: the env block now renders the
    # workspace path, and a pytest tmp_path can itself contain "tool_subset"
    # (this test's own name does), which is NOT a config leak.
    assert '"tool_subset"' not in body


# ---------------------------------------------------------------------------
# 4b. The recorded request carries the tool spec + never the credential (R-1)
#
# Driven through the production ``SdkHost`` + ``InteractionDriver`` (the same
# assembly as section 4a) with the HTTP transport injected via ``mcp_http_post``
# (the fake server), so the live run connects + records; the recording carries
# the mcp tool spec a replay rebuilds its stub from, and the credential never
# enters the persisted bytes.
# ---------------------------------------------------------------------------


def _runner(
    tmp_path: Path,
    *,
    server: FakeHttpMcpServer,
) -> Any:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    spec = McpHttpServerSpec(
        alias="remote",
        url="https://x/mcp",
        headers=(("Authorization", "Bearer secret-token"),),
    )
    specs = {spec.alias: spec}
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=_mcp_responses()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        mcp_server_resolver=specs.get,
        mcp_http_post=server.post,
    )
    return host, make_driver(host)


def test_live_runner_records_http_mcp_call(tmp_path: Path) -> None:
    server = FakeHttpMcpServer(mode="echo")
    host, driver = _runner(tmp_path, server=server)
    out = driver.start(
        goal="use the remote mcp tool", agent="main", enabled_mcp=("remote",)
    )
    events = host.event_log.read(out.task_id)
    assert out.status == "terminal"
    starts = [
        e for e in events
        if e.type == "ToolCallStarted"
        and e.payload.tool_name == "mcp__remote__echo"
    ]
    assert len(starts) == 1
    assert any(e.type == "ToolResultRecorded" and e.payload.success for e in events)
    assert any(
        h.get("Authorization") == "Bearer secret-token" for h in server.seen_headers
    )


def test_recorded_request_redacts_mcp_credential(tmp_path: Path) -> None:
    """The recorded LLM request tools carry the MCP tool specs but never the
    server credential — a connection header stays out of the persisted bytes."""
    server = FakeHttpMcpServer(mode="echo")
    host, driver = _runner(tmp_path, server=server)
    out = driver.start(
        goal="use the remote mcp tool", agent="main", enabled_mcp=("remote",)
    )
    assert len(server.calls) > 0  # live DID connect + call

    first_req = next(
        e for e in host.event_log.read(out.task_id)
        if e.type == "LLMRequestStarted"
    )
    recorded_tools = json.loads(
        host.content_store.get(first_req.payload.request_ref).decode("utf-8")
    )["tools"]
    specs = parse_mcp_tool_specs(recorded_tools)
    assert [s.name for s in specs] == ["mcp__remote__echo"]
    # The recorded request tools carry no credential.
    assert "secret-token" not in host.content_store.get(
        first_req.payload.request_ref
    ).decode("utf-8")
