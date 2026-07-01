"""Per-task MCP provenance: enabled aliases + tool subset
(names only), no credentials; behaviour stays R-1's job.

Covers the issue's acceptance criteria:

* the task's provenance is readable: ``GovernanceState.mcp_provenance`` lists
  the enabled aliases + each one's ticked tool subset (names only);
* credentials (token / url / header values) never appear in any persisted event /
  recording / provenance field;
* the tool behaviour lives in R-1's recorded ``request_ref`` tool spec, not in
  this provenance;
* a task with no MCP carries no provenance event and folds to ``[]``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from noeta.agent.host.mcp_registry import McpServerRegistry
from noeta.client import SdkHost
from noeta.core.fold import fold
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
    McpHttpServerSpec,
    McpServerSpec,
    mcp_provenance_from_specs,
)
from tests._fixtures.routing_http_mcp_server import RoutingHttpMcpServer
from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    official_registry,
    preset_spec,
    runner_main_spec,
)


# ---------------------------------------------------------------------------
# 0. The pure provenance builder (names only, alias-sorted, no credentials)
# ---------------------------------------------------------------------------


def test_provenance_from_specs_names_only_no_credentials() -> None:
    specs = (
        # alphabetically OUT of order on purpose — the builder sorts.
        McpHttpServerSpec(
            alias="notion",
            url="https://secret.example/notion",
            headers=(("Authorization", "Bearer super-secret-token"),),
            tool_subset=("search", "create_page"),
        ),
        McpServerSpec(
            alias="files",
            argv=("npx", "server-fs", "/etc/passwd"),
            env=(("API_KEY", "sk-leak"),),
            tool_subset=None,  # no subset ⇒ all advertised
        ),
    )
    prov = mcp_provenance_from_specs(specs)
    # Alias-sorted; tools sorted; None subset ⇒ [].
    assert prov == [
        {"alias": "files", "tools": []},
        {"alias": "notion", "tools": ["create_page", "search"]},
    ]
    # No credential / url / argv anywhere in the record.
    blob = json.dumps(prov)
    for secret in (
        "super-secret-token",
        "sk-leak",
        "secret.example",
        "/etc/passwd",
        "npx",
        "Authorization",
        "API_KEY",
    ):
        assert secret not in blob


# ---------------------------------------------------------------------------
# Shared SdkHost harness (mirrors the connection-lifecycle test)
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
        mcp_server_resolver=registry.resolve_spec,
        mcp_http_post=router.post,
    )
    return host, event_log, content_store


# ---------------------------------------------------------------------------
# 1. e2e — provenance recorded, readable off fold, names only, no credential
# ---------------------------------------------------------------------------


def test_e2e_records_provenance_with_subset_and_no_credential(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    # Two servers, each carrying a secret credential header. "multi" advertises
    # alpha/beta/gamma; the user ticked only [alpha, beta]. "good" advertises
    # echo with no subset (all).
    router = RoutingHttpMcpServer({"MULTI": "multi", "GOOD": "echo"})
    registry = McpServerRegistry(tmp_path / "m.json")
    registry.upsert_http(
        alias="aaa",
        url="https://x/aaa",
        headers={"X-Server": "MULTI", "Authorization": "Bearer secret-aaa"},
        tools=["alpha", "beta"],
    )
    registry.upsert_http(
        alias="zzz",
        url="https://x/zzz",
        headers={"X-Server": "GOOD", "Authorization": "Bearer secret-zzz"},
    )
    host, event_log, content_store = _host(
        ws,
        responses=[_call("c1", "mcp__zzz__echo", {"msg": "hi"}), _end()],
        router=router,
        registry=registry,
    )
    driver = InteractionDriver(host)
    started = driver.start(
        goal="use mcp", agent="main", enabled_mcp=("zzz", "aaa")
    )
    events = event_log.read(started.task_id)

    # Exactly one provenance event, observer origin, in the pre-loop window.
    prov_events = [e for e in events if e.type == "McpProvenanceRecorded"]
    assert len(prov_events) == 1
    assert prov_events[0].origin == "observer"
    started_idx = next(
        i for i, e in enumerate(events) if e.type == "TaskStarted"
    )
    prov_idx = next(
        i for i, e in enumerate(events) if e.type == "McpProvenanceRecorded"
    )
    assert prov_idx < started_idx  # pre-loop ⇒ recorded before the first request

    # Readable off fold (the task's provenance): alias-sorted, subset names only.
    gov = fold(event_log, content_store, started.task_id).governance
    assert gov.mcp_provenance == [
        {"alias": "aaa", "tools": ["alpha", "beta"]},
        {"alias": "zzz", "tools": []},
    ]

    # No credential anywhere in the whole event stream / provenance.
    whole = json.dumps(
        [
            {"type": e.type, "payload": getattr(e.payload, "servers", None)}
            for e in prov_events
        ]
    )
    assert "secret-aaa" not in whole and "secret-zzz" not in whole
    assert "https://x/" not in whole and "Authorization" not in whole


def test_provenance_records_enabled_alias_even_when_connect_fails(
    tmp_path: Path,
) -> None:
    # A server that resolves to a spec but fails to CONNECT still counts as
    # "enabled this run" — it appears in provenance AND gets a McpServerSkipped.
    ws = tmp_path / "ws"
    ws.mkdir()
    router = RoutingHttpMcpServer({"BAD": "boom", "GOOD": "echo"})
    registry = McpServerRegistry(tmp_path / "m.json")
    registry.upsert_http(
        alias="bad", url="https://x/bad", headers={"X-Server": "BAD"},
        tools=["whatever"],
    )
    registry.upsert_http(
        alias="good", url="https://x/good", headers={"X-Server": "GOOD"}
    )
    host, event_log, content_store = _host(
        ws, responses=[_end()], router=router, registry=registry
    )
    driver = InteractionDriver(host)
    started = driver.start(
        goal="mixed", agent="main", enabled_mcp=("bad", "good")
    )
    gov = fold(event_log, content_store, started.task_id).governance
    # Both enabled aliases are in provenance — provenance records what was
    # ENABLED, independent of whether the connect later succeeded.
    assert [s["alias"] for s in gov.mcp_provenance] == ["bad", "good"]
    assert gov.mcp_provenance[0]["tools"] == ["whatever"]


def test_no_mcp_task_has_empty_provenance_and_no_event(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    router = RoutingHttpMcpServer({"GOOD": "echo"})
    registry = McpServerRegistry(tmp_path / "m.json")
    host, event_log, content_store = _host(
        ws, responses=[_end()], router=router, registry=registry
    )
    driver = InteractionDriver(host)
    started = driver.start(goal="no mcp", agent="main")  # enabled_mcp defaults ()
    events = event_log.read(started.task_id)
    assert not [e for e in events if e.type == "McpProvenanceRecorded"]
    gov = fold(event_log, content_store, started.task_id).governance
    assert gov.mcp_provenance == []


# ---------------------------------------------------------------------------
# 2. R-1 still carries the tool behaviour; provenance carries only names
# ---------------------------------------------------------------------------


def test_provenance_is_names_only_behaviour_lives_in_request_ref(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    router = RoutingHttpMcpServer({"GOOD": "echo"})
    registry = McpServerRegistry(tmp_path / "m.json")
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
    started = driver.start(goal="use", agent="main", enabled_mcp=("good",))
    events = event_log.read(started.task_id)

    # Provenance carries the NAME only — no input_schema / description.
    gov = fold(event_log, content_store, started.task_id).governance
    assert gov.mcp_provenance == [{"alias": "good", "tools": []}]

    # The tool's real shape (input_schema etc.) lives in the recorded
    # request_ref (R-1), NOT in the provenance — that is where a resume reads it.
    first_req = next(e for e in events if e.type == "LLMRequestStarted")
    body = json.loads(content_store.get(first_req.payload.request_ref).decode())
    fn = next(
        t["function"]
        for t in body["tools"]
        if t.get("function", {}).get("name") == "mcp__good__echo"
    )
    # The behaviour anchor (the input schema) is in the recorded request_ref —
    # NOT in the provenance (which is names only).
    assert "parameters" in fn


# ---------------------------------------------------------------------------
# 4. SDK host path — records provenance (names only, no credential)
# ---------------------------------------------------------------------------


def _mcp_call_responses() -> list[LLMResponse]:
    return [_call("c1", "mcp__good__echo", {"msg": "hi"}), _end()]


def test_runner_records_provenance_and_replay_passes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    router = RoutingHttpMcpServer({"GOOD": "echo"})
    spec = McpHttpServerSpec(
        alias="good",
        url="https://x/good",
        headers=(
            ("X-Server", "GOOD"),
            ("Authorization", "Bearer secret-runner"),
        ),
        tool_subset=("echo",),
    )
    specs = {spec.alias: spec}
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
        goal="use surviving mcp tool", agent="main", enabled_mcp=("good",)
    )
    task_id = out.task_id

    from noeta.tools.mcp import parse_mcp_tool_specs

    recorded = host.event_log.read(task_id)

    # Provenance is recorded (names only, no credential) and readable off fold.
    prov = [e for e in recorded if e.type == "McpProvenanceRecorded"]
    assert len(prov) == 1
    gov = fold(host.event_log, host.content_store, task_id).governance
    assert gov.mcp_provenance == [{"alias": "good", "tools": ["echo"]}]
    assert "secret-runner" not in json.dumps(prov[0].payload.servers)

    # The recorded request_ref carries the tool spec but never the credential.
    first_req = next(e for e in recorded if e.type == "LLMRequestStarted")
    recorded_tools = json.loads(
        host.content_store.get(first_req.payload.request_ref).decode("utf-8")
    )["tools"]
    specs_parsed = parse_mcp_tool_specs(recorded_tools)
    assert [s.name for s in specs_parsed] == ["mcp__good__echo"]
    # No credential in the recorded request either.
    assert "secret-runner" not in host.content_store.get(
        first_req.payload.request_ref
    ).decode("utf-8")


# ---------------------------------------------------------------------------
# 5. Subagent path — the opt-in child gets its OWN provenance on its OWN stream
# ---------------------------------------------------------------------------


def _spawn_call(call_id: str, agent: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name="spawn_subagent",
                arguments={"agent": agent, "goal": "scout"},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end_text(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": text},
    )


def test_subagent_opt_in_child_records_own_provenance(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    router = RoutingHttpMcpServer({"ECHO": "echo"})
    spec = McpHttpServerSpec(
        alias="echo",
        url="https://x/echo",
        headers=(("X-Server", "ECHO"),),
    )
    specs = {spec.alias: spec}
    host = make_host(
        make_registry(
            runner_main_spec(
                "main", delegation=True, spawnable=("general-purpose",)
            ),
            preset_spec("general-purpose"),
        ),
        workspace_dir=ws,
        provider=FakeLLMProvider(
            responses=[
                _spawn_call("s1", "general-purpose"),  # parent delegates
                _end_text("worker-done"),  # child finishes
                _end_text("done"),  # parent finishes
            ]
        ),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        mcp_server_resolver=specs.get,
        mcp_http_post=router.post,
    )
    driver = make_driver(host)
    out = driver.start(
        goal="delegate then finish", agent="main", enabled_mcp=("echo",)
    )
    assert out.status == "terminal"
    parent_events = host.event_log.read(out.task_id)
    # Parent recorded its own provenance.
    assert [
        e for e in parent_events if e.type == "McpProvenanceRecorded"
    ]
    # The general-purpose child (mcp=True) recorded its OWN provenance on its
    # OWN stream (independent connect / R-1).
    child_ids = [
        str(e.payload.subtask_id)
        for e in parent_events
        if e.type == "SubtaskSpawned"
    ]
    assert len(child_ids) == 1
    child_events = host.event_log.read(child_ids[0])
    child_prov = [
        e for e in child_events if e.type == "McpProvenanceRecorded"
    ]
    assert len(child_prov) == 1
    assert child_prov[0].payload.servers == [
        {"alias": "echo", "tools": []}
    ]
