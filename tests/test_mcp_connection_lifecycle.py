"""MCP connection lifecycle: freeze + deterministic order
+ skip-on-failure + warning event.

Covers the issue's acceptance criteria:

* the tool set is connected + discovered ONCE at task start and then frozen
  (the resolver caches per (agent, …, mcp_aliases), so a re-resolve never
  reconnects / re-discovers);
* enabled servers are appended alias-sorted, tools name-sorted, so the
  recorded ``tools`` order + stable hash is identical in live + replay even when
  one server is skipped;
* a single server that cannot connect is SKIPPED (option B): its tools never
  enter the (still frozen) set, one ``McpServerSkipped`` observer event is
  recorded on the task stream (front-end surface), and the task continues with
  the surviving servers' tools — it does NOT fail the whole task;
* replay reads the recorded result, NEVER reconnects, and a recording that
  carries a ``McpServerSkipped`` event replays to the same terminal.
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
    McpError,
    McpHttpServerSpec,
    McpServerSkip,
    build_mcp_tools,
)
from tests._fixtures.routing_http_mcp_server import RoutingHttpMcpServer
from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    official_registry,
    runner_main_spec,
)


# ---------------------------------------------------------------------------
# 1. build_mcp_tools(skip_on_failure=True) — per-server skip, order preserved
# ---------------------------------------------------------------------------


def _http_spec(alias: str, marker: str) -> McpHttpServerSpec:
    # The X-Server marker header rides on the wire so the router can dispatch;
    # it is a normal header (no credential) — but it also proves headers are
    # per-server.
    return McpHttpServerSpec(
        alias=alias, url=f"https://x/{alias}", headers=(("X-Server", marker),)
    )


def test_build_skip_on_failure_drops_one_keeps_rest() -> None:
    # Two servers: "bad" fails its handshake, "good" works. skip_on_failure=True
    # ⇒ the bad one is dropped + recorded, the good one's tool survives.
    router = RoutingHttpMcpServer({"BAD": "boom", "GOOD": "echo"})
    # Pass alias-sorted (D7: callers sort) — "bad" before "good".
    specs = (_http_spec("bad", "BAD"), _http_spec("good", "GOOD"))
    tools, clients, skipped = build_mcp_tools(
        specs, http_post=router.post, skip_on_failure=True
    )
    try:
        assert list(tools) == ["mcp__good__echo"]  # only the surviving server
        assert [s.alias for s in skipped] == ["bad"]
        assert isinstance(skipped[0], McpServerSkip)
        assert skipped[0].reason  # a non-empty fault message
    finally:
        for c in clients:
            c.shutdown()


def test_build_skip_on_failure_false_is_fail_fast() -> None:
    # Default (discover_tools / menu path): a failing server raises, no skip.
    router = RoutingHttpMcpServer({"BAD": "boom"})
    with pytest.raises(McpError):
        build_mcp_tools(
            (_http_spec("bad", "BAD"),), http_post=router.post
        )


def test_build_skip_all_failing_returns_empty_tools_plus_skips() -> None:
    router = RoutingHttpMcpServer({"A": "boom", "B": "boom"})
    specs = (_http_spec("a", "A"), _http_spec("b", "B"))
    tools, clients, skipped = build_mcp_tools(
        specs, http_post=router.post, skip_on_failure=True
    )
    try:
        assert tools == {}
        assert sorted(s.alias for s in skipped) == ["a", "b"]
    finally:
        for c in clients:
            c.shutdown()


def test_build_skip_preserves_deterministic_order() -> None:
    # Three servers, the MIDDLE one fails: the surviving tools stay alias-sorted
    # then name-sorted, identical to a build that never had the bad one.
    router = RoutingHttpMcpServer({"A": "multi", "B": "boom", "C": "echo"})
    specs = (
        _http_spec("aaa", "A"),
        _http_spec("bbb", "B"),
        _http_spec("ccc", "C"),
    )
    tools, clients, skipped = build_mcp_tools(
        specs, http_post=router.post, skip_on_failure=True
    )
    try:
        assert list(tools) == [
            "mcp__aaa__alpha",
            "mcp__aaa__beta",
            "mcp__aaa__gamma",
            "mcp__ccc__echo",
        ]
        assert [s.alias for s in skipped] == ["bbb"]
    finally:
        for c in clients:
            c.shutdown()


# ---------------------------------------------------------------------------
# 2. SdkHost / driver e2e — skip + warning event + remaining tools usable
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


def _host(
    workspace: Path,
    *,
    responses: list[LLMResponse],
    router: RoutingHttpMcpServer,
    registry: McpServerRegistry,
    multi_turn: bool = False,
) -> tuple[SdkHost, InMemoryEventLog, InMemoryContentStore]:
    from noeta.execution.driver import multi_turn_policy_wrapper

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
        mcp_server_resolver=registry.resolve_spec,
        mcp_http_post=router.post,
        policy_wrapper=multi_turn_policy_wrapper if multi_turn else None,
    )
    return host, event_log, content_store


def test_e2e_skip_failing_server_emits_warning_and_uses_survivor(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    # "bad" connector boom-fails; "good" works and exposes echo.
    router = RoutingHttpMcpServer({"BAD": "boom", "GOOD": "echo"})
    registry = McpServerRegistry(tmp_path / "m.json")
    registry.upsert_http(alias="bad", url="https://x/bad", headers={"X-Server": "BAD"})
    registry.upsert_http(
        alias="good", url="https://x/good", headers={"X-Server": "GOOD"}
    )
    host, event_log, content_store = _host(
        ws,
        responses=[_call("c1", "mcp__good__echo", {"msg": "hi"}), _end()],
        router=router,
        registry=registry,
    )
    driver = InteractionDriver(host)
    started = driver.start(
        goal="use surviving mcp tool",
        agent="main",
        enabled_mcp=("bad", "good"),
    )
    assert started.status in ("terminal", "suspended")
    events = event_log.read(started.task_id)

    # The bad server was skipped + recorded ONE observer warning event.
    skips = [e for e in events if e.type == "McpServerSkipped"]
    assert len(skips) == 1
    assert skips[0].payload.alias == "bad"
    assert skips[0].payload.reason  # a fault message (no credential)
    assert skips[0].origin == "observer"

    # The surviving server's tool was actually called + succeeded.
    starts = [
        e for e in events
        if e.type == "ToolCallStarted" and e.payload.tool_name == "mcp__good__echo"
    ]
    assert len(starts) == 1
    assert any(e.type == "ToolResultRecorded" and e.payload.success for e in events)

    # The recorded request tools carry ONLY the surviving server's tool — never
    # the bad one — and no credential.
    first_req = next(e for e in events if e.type == "LLMRequestStarted")
    body = content_store.get(first_req.payload.request_ref).decode("utf-8")
    assert "mcp__good__echo" in body
    assert "mcp__bad__" not in body


def test_e2e_warning_event_carries_no_credential(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    router = RoutingHttpMcpServer({"BAD": "boom"})
    registry = McpServerRegistry(tmp_path / "m.json")
    # The bad server carries a secret credential header — it must never appear
    # in the skip event (D3: credentials never enter any recording).
    registry.upsert_http(
        alias="bad",
        url="https://x/bad",
        headers={"X-Server": "BAD", "Authorization": "Bearer secret-token"},
    )
    host, event_log, content_store = _host(
        ws, responses=[_end()], router=router, registry=registry
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="all fail", agent="main", enabled_mcp=("bad",))
    events = event_log.read(started.task_id)
    skips = [e for e in events if e.type == "McpServerSkipped"]
    assert len(skips) == 1
    assert "secret-token" not in json.dumps(
        {"alias": skips[0].payload.alias, "reason": skips[0].payload.reason}
    )


def test_e2e_tool_set_frozen_no_rediscovery_across_turns(tmp_path: Path) -> None:
    # A second turn (continue chat) with the SAME enabled_mcp must reuse the
    # cached Engine — NO reconnect, NO second skip event (the set is frozen).
    ws = tmp_path / "ws"
    ws.mkdir()
    router = RoutingHttpMcpServer({"BAD": "boom", "GOOD": "echo"})
    registry = McpServerRegistry(tmp_path / "m.json")
    registry.upsert_http(alias="bad", url="https://x/bad", headers={"X-Server": "BAD"})
    registry.upsert_http(
        alias="good", url="https://x/good", headers={"X-Server": "GOOD"}
    )
    host, event_log, content_store = _host(
        ws,
        responses=[_end(), _end()],  # two turns, each ends immediately
        router=router,
        registry=registry,
        multi_turn=True,
    )
    driver = InteractionDriver(host)
    started = driver.start(
        goal="turn 1", agent="main", enabled_mcp=("bad", "good")
    )
    after_turn1 = event_log.read(started.task_id)
    skips1 = [e for e in after_turn1 if e.type == "McpServerSkipped"]
    assert len(skips1) == 1  # skipped once at task start

    # Continue the conversation with the SAME enabled set.
    driver.send_goal(
        task_id=started.task_id, goal="turn 2", enabled_mcp=("bad", "good")
    )
    after_turn2 = event_log.read(started.task_id)
    skips2 = [e for e in after_turn2 if e.type == "McpServerSkipped"]
    # Still exactly one — the frozen, cached Engine never re-discovered.
    assert len(skips2) == 1


# ---------------------------------------------------------------------------
# 3. SDK host path — skip + warning event + survivor usable
# ---------------------------------------------------------------------------


def _mcp_call_responses() -> list[LLMResponse]:
    return [_call("c1", "mcp__good__echo", {"msg": "hi"}), _end()]


def test_runner_skips_failing_server_and_records_warning(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    router = RoutingHttpMcpServer({"BAD": "boom", "GOOD": "echo"})
    # alias-sorted: "bad" then "good".
    specs = {
        "bad": McpHttpServerSpec(
            alias="bad", url="https://x/bad", headers=(("X-Server", "BAD"),)
        ),
        "good": McpHttpServerSpec(
            alias="good", url="https://x/good", headers=(("X-Server", "GOOD"),)
        ),
    }
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=_mcp_call_responses()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        mcp_server_resolver=specs.get,
        mcp_http_post=router.post,
    )
    driver = make_driver(host)
    out = driver.start(
        goal="use surviving mcp tool", agent="main", enabled_mcp=tuple(specs)
    )
    events = host.event_log.read(out.task_id)
    assert out.status == "terminal"

    skips = [e for e in events if e.type == "McpServerSkipped"]
    assert len(skips) == 1
    assert skips[0].payload.alias == "bad"
    assert skips[0].origin == "observer"

    # The surviving server's tool ran.
    starts = [
        e for e in events
        if e.type == "ToolCallStarted" and e.payload.tool_name == "mcp__good__echo"
    ]
    assert len(starts) == 1
    assert any(e.type == "ToolResultRecorded" and e.payload.success for e in events)
