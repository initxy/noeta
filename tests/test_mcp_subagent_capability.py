"""Subtask MCP inheritance gated by the ``mcp`` Capability flag + presets defaults.

Covers the issue's acceptance criteria:

* ``Capabilities``/``AgentSpec`` carry a ``mcp`` flag whose fingerprint
  treatment matches the other "new" flags — written into the descriptor ONLY
  when True (conditional fold), so explore/plan (mcp=False) keep byte-identical
  fingerprints while main/general-purpose (mcp=True) pin a re-keyed golden.
* presets default: main / general-purpose open ``mcp``; explore / plan keep it
  closed (explore stays physically read-only).
* a delegated child inherits the parent task's enabled MCP tool set ONLY when
  its own spec opens ``mcp`` (per-spec opt-in); a child without the flag gets no
  MCP tools at all.
* the opt-in child connects its OWN independent server session, so its recording
  carries its own ``mcp__<alias>__<tool>`` spec, and replay never reconnects.

The child-inheritance gate is exercised through the production ``SdkHost`` +
``InteractionDriver``: the delegation path drives a parent that spawns the child
(``drive_pending_subtasks`` → ``_child_mcp_aliases``), so the child's own first
request reflects whether it inherited the enabled MCP set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from noeta.agent.host.mcp_registry import McpServerRegistry
from noeta.agent.spec import Capabilities
from noeta.client import SdkHost
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.policies.react import SPAWN_SUBAGENT_TOOL
from noeta.presets import official_specs
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode
from noeta.tools.mcp import McpHttpServerSpec
from tests._fixtures.fake_http_mcp_server import FakeHttpMcpServer
from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    official_registry,
    preset_spec,
    runner_main_spec,
)


CHILD_GOAL = "do the work"
PARENT_GOAL = "delegate then finish"


# ---------------------------------------------------------------------------
# scripted-provider helpers
# ---------------------------------------------------------------------------


def _spawn_call(call_id: str, agent: str, goal: str = CHILD_GOAL) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": agent, "goal": goal},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _mcp_call(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "x.py").write_text("foo\n")
    return ws


def _req_tool_names(req: LLMRequest) -> set[str]:
    return {t["function"]["name"] for t in req.tools}


# ---------------------------------------------------------------------------
# 1. Capabilities.mcp flag — fingerprint conditional-fold (matches other flags)
# ---------------------------------------------------------------------------


def test_mcp_flag_default_false_zero_drift() -> None:
    # A spec with mcp=False (default) has the SAME fingerprint as one that never
    # mentions mcp — the descriptor writes the key only when True (the new-flag
    # conditional-write rule shared by skill_invocation / memory).
    base = Capabilities(skill_invocation=True)
    explicit_off = Capabilities(skill_invocation=True, mcp=False)
    assert base == explicit_off
    # the descriptor folds it away, so explore (mcp=False) keeps its golden.
    specs = official_specs()
    assert specs["explore"].capabilities.mcp is False
    assert specs["plan"].capabilities.mcp is False


def test_mcp_flag_true_changes_identity() -> None:
    # Flipping mcp on is a real identity change (structural ``==`` since the
    # verify-era fingerprint digest was retired), exactly like the other
    # behaviour-shaping flags.
    from noeta.agent.spec import AgentSpec, ComponentRef

    off = AgentSpec(
        name="x", instructions="i", policy=ComponentRef("react"),
        capabilities=Capabilities(),
    )
    on = AgentSpec(
        name="x", instructions="i", policy=ComponentRef("react"),
        capabilities=Capabilities(mcp=True),
    )
    assert off != on
    assert off.capabilities != on.capabilities


def test_presets_mcp_defaults() -> None:
    specs = official_specs()
    assert specs["main"].capabilities.mcp is True
    assert specs["general-purpose"].capabilities.mcp is True
    assert specs["explore"].capabilities.mcp is False
    assert specs["plan"].capabilities.mcp is False


# ---------------------------------------------------------------------------
# 2. SDK host delegation path — opt-in inheritance gate
# ---------------------------------------------------------------------------


def _session_with_mcp(
    ws: Path,
    *,
    child_agent: str,
    responses: list[LLMResponse],
    server: FakeHttpMcpServer,
) -> tuple[SdkHost, InteractionDriver, FakeLLMProvider]:
    """A one-shot SDK host that delegates to ``child_agent`` with one enabled
    MCP server.

    ``delegate_to=(child_agent,)`` maps to delegation on the main spec +
    ``spawnable=(child_agent,)``, with the named child registered alongside it;
    the enabled alias rides on ``driver.start(enabled_mcp=("echo",))``. Returns
    ``(host, driver, provider)`` — the shared ``FakeLLMProvider`` carries
    ``received_requests`` for the white-box child-schema assertions.
    """
    provider = FakeLLMProvider(responses=responses)
    spec = McpHttpServerSpec(alias="echo", url="https://x/echo")
    specs = {spec.alias: spec}
    host = make_host(
        make_registry(
            runner_main_spec("main", delegation=True, spawnable=(child_agent,)),
            preset_spec(child_agent),
        ),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        mcp_server_resolver=specs.get,
        mcp_http_post=server.post,
    )
    return host, make_driver(host), provider


def test_explore_child_gets_no_mcp_tools(tmp_path: Path) -> None:
    # explore (mcp=False) — even though the session has an enabled MCP server,
    # the read-only scout child inherits NO MCP tool. Its first LLMRequest tool
    # schema must not carry any mcp__ tool.
    ws = _make_ws(tmp_path)
    server = FakeHttpMcpServer(mode="echo")
    _host, driver, provider = _session_with_mcp(
        ws,
        child_agent="explore",
        responses=[_spawn_call("s1", "explore"), _end("scouted"), _end("done")],
        server=server,
    )
    driver.start(goal=PARENT_GOAL, agent="main", enabled_mcp=("echo",))
    # received_requests[0] = parent spawn turn; [1] = child's first turn.
    child_req = provider.received_requests[1]
    names = _req_tool_names(child_req)
    assert not any(n.startswith("mcp__") for n in names), (
        f"explore child must inherit NO mcp tools, got {sorted(names)}"
    )


def test_general_purpose_child_inherits_and_calls_mcp(tmp_path: Path) -> None:
    # general-purpose (mcp=True) — the worker child inherits the session's
    # enabled MCP server, its first LLMRequest carries mcp__echo__echo, and it
    # can actually call it.
    ws = _make_ws(tmp_path)
    server = FakeHttpMcpServer(mode="echo")
    host, driver, provider = _session_with_mcp(
        ws,
        child_agent="general-purpose",
        responses=[
            _spawn_call("s1", "general-purpose"),  # parent delegates
            _mcp_call("c1", "mcp__echo__echo", {"msg": "hi"}),  # child uses MCP
            _end("worker-done"),  # child finishes
            _end("done"),  # parent finishes
        ],
        server=server,
    )
    out = driver.start(goal=PARENT_GOAL, agent="main", enabled_mcp=("echo",))
    assert out.status == "terminal"
    child_req = provider.received_requests[1]
    assert "mcp__echo__echo" in _req_tool_names(child_req)

    # The child actually called the inherited MCP tool — find its task stream.
    child_ids = [
        str(e.payload.subtask_id)
        for e in host.event_log.read(out.task_id)
        if e.type == "SubtaskSpawned"
    ]
    assert len(child_ids) == 1
    child_events = host.event_log.read(child_ids[0])
    starts = [
        e for e in child_events
        if e.type == "ToolCallStarted"
        and e.payload.tool_name == "mcp__echo__echo"
    ]
    assert len(starts) == 1
    assert any(
        e.type == "ToolResultRecorded" and e.payload.success
        for e in child_events
    )

    # Independent recording: the CHILD's own first LLMRequest recorded its
    # own mcp tool spec (R-1 — replay rebuilds the stub off this, never
    # reconnects).
    child_first_req = next(
        e for e in child_events if e.type == "LLMRequestStarted"
    )
    body = host.content_store.get(
        child_first_req.payload.request_ref
    ).decode("utf-8")
    assert "mcp__echo__echo" in body


def test_no_enabled_servers_child_byte_identical(tmp_path: Path) -> None:
    # No session MCP servers ⇒ even an mcp=True child gets no MCP tools,
    # byte-identical to the pre-0042 child boundary.
    ws = _make_ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[
            _spawn_call("s1", "general-purpose"),
            _end("worker"),
            _end("done"),
        ]
    )
    host = make_host(
        make_registry(
            runner_main_spec(
                "main", delegation=True, spawnable=("general-purpose",)
            ),
            preset_spec("general-purpose"),
        ),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    driver = make_driver(host)
    driver.start(goal=PARENT_GOAL, agent="main")
    child_req = provider.received_requests[1]
    assert not any(
        n.startswith("mcp__") for n in _req_tool_names(child_req)
    )


# ---------------------------------------------------------------------------
# 3. SdkHost server resolver path — same opt-in gate
# ---------------------------------------------------------------------------


def _sdk_host(
    ws: Path,
    *,
    responses: list[LLMResponse],
    server: FakeHttpMcpServer,
    registry: McpServerRegistry,
) -> tuple[SdkHost, FakeLLMProvider, InMemoryEventLog, InMemoryContentStore]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    # CRITICAL: without the ChildLifecycleObserver the spawned child is never
    # enqueued and the parent's SubtaskCompleted wake never fires.
    from noeta.core.wiring import wire_default_observers

    wire_default_observers(event_log, dispatcher)
    provider = FakeLLMProvider(responses=responses)
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=provider,
        model="gpt-test",
        workspace_dir=ws,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
        registry=official_registry(),
        aliases={"default": "main"},
        mcp_server_resolver=registry.resolve_spec,
        mcp_http_post=server.post,
        policy_wrapper=multi_turn_policy_wrapper,
    )
    return host, provider, event_log, content_store


def test_server_path_explore_child_no_mcp(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    server = FakeHttpMcpServer(mode="echo")
    registry = McpServerRegistry(tmp_path / "m.json")
    registry.upsert_http(alias="echo", url="https://x/echo")
    host, provider, event_log, content_store = _sdk_host(
        ws,
        responses=[_spawn_call("s1", "explore"), _end("scouted"), _end("done")],
        server=server,
        registry=registry,
    )
    driver = InteractionDriver(host)
    started = driver.start(
        goal=PARENT_GOAL, agent="main", enabled_mcp=("echo",)
    )
    assert started.status in ("terminal", "suspended")
    child_req = provider.received_requests[1]
    assert not any(
        n.startswith("mcp__") for n in _req_tool_names(child_req)
    )
    # explore connected nothing on its own stream.
    child_ids = [
        str(e.payload.subtask_id)
        for e in event_log.read(started.task_id)
        if e.type == "SubtaskSpawned"
    ]
    assert len(child_ids) == 1
    child_events = event_log.read(child_ids[0])
    first_req = next(e for e in child_events if e.type == "LLMRequestStarted")
    body = content_store.get(first_req.payload.request_ref).decode("utf-8")
    assert "mcp__echo__echo" not in body


def test_server_path_gp_child_inherits_mcp(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    server = FakeHttpMcpServer(mode="echo")
    registry = McpServerRegistry(tmp_path / "m.json")
    registry.upsert_http(alias="echo", url="https://x/echo")
    host, provider, event_log, content_store = _sdk_host(
        ws,
        responses=[
            _spawn_call("s1", "general-purpose"),
            _mcp_call("c1", "mcp__echo__echo", {"msg": "hi"}),
            _end("worker-done"),
            _end("done"),
        ],
        server=server,
        registry=registry,
    )
    driver = InteractionDriver(host)
    started = driver.start(
        goal=PARENT_GOAL, agent="main", enabled_mcp=("echo",)
    )
    assert started.status in ("terminal", "suspended")
    child_req = provider.received_requests[1]
    assert "mcp__echo__echo" in _req_tool_names(child_req)
    # The child's OWN recording carries the inherited mcp tool spec (R-1).
    child_ids = [
        str(e.payload.subtask_id)
        for e in event_log.read(started.task_id)
        if e.type == "SubtaskSpawned"
    ]
    assert len(child_ids) == 1
    child_events = event_log.read(child_ids[0])
    starts = [
        e for e in child_events
        if e.type == "ToolCallStarted"
        and e.payload.tool_name == "mcp__echo__echo"
    ]
    assert len(starts) == 1
    first_req = next(e for e in child_events if e.type == "LLMRequestStarted")
    recorded = json.loads(
        content_store.get(first_req.payload.request_ref).decode("utf-8")
    )
    tool_names = {t["function"]["name"] for t in recorded["tools"]}
    assert "mcp__echo__echo" in tool_names
