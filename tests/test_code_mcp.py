"""Phase 4.5 F2 — MCP external tools end-to-end through `noeta code`.

Runs a coding session that calls a tool from a local stdio MCP server
(the in-tree fake server), then proves: the call + three-event envelope
are recorded; default-off adds nothing to the schema; permission governs
MCP (guard-level); a delegated child does not inherit MCP; and the inline
approval round-trip works.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from noeta.presets import official_specs
from tests._session_inputs import build_code_replay_inputs
from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    runner_main_spec,
)
from noeta.protocols.decisions import ToolCall
from noeta.protocols.hooks import GuardContext, ProposedToolCall, Verdict
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode
from noeta.tools.mcp import McpServerSpec
from noeta.tools.mcp.tool import McpTool


_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")


def _echo_spec(alias: str = "fake") -> McpServerSpec:
    return McpServerSpec(alias=alias, argv=(sys.executable, "-u", _FAKE, "echo"))


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


def _mcp_call_responses() -> list[LLMResponse]:
    return [_call("c1", "mcp__fake__echo", {"msg": "hi"}), _end()]


def _run(
    tmp_path: Path,
    *,
    responses: list[LLMResponse],
    mcp_servers: tuple[McpServerSpec, ...],
    require_approval_tools: tuple[str, ...] = (),
):
    """A one-shot SDK host + driver wired for the given MCP servers.

    ``mcp_servers`` maps onto the production resolver seam: the host takes an
    alias→spec resolver callback (``specs.get``) and the per-turn enabled-alias
    list rides on ``driver.start(enabled_mcp=...)``. Returns
    ``(host, driver, enabled_aliases)``.
    """
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    specs = {s.alias: s for s in mcp_servers}
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        require_approval_tools=require_approval_tools,
        mcp_server_resolver=specs.get,
    )
    return host, make_driver(host), tuple(specs)


# -- live discovery + call + record -----------------------------------------


def test_live_call_records_envelope(tmp_path: Path) -> None:
    host, driver, enabled = _run(
        tmp_path, responses=_mcp_call_responses(), mcp_servers=(_echo_spec(),)
    )
    out = driver.start(goal="use the mcp tool", agent="main", enabled_mcp=enabled)
    events = host.event_log.read(out.task_id)
    assert out.status == "terminal"
    starts = [
        e for e in events
        if e.type == "ToolCallStarted" and e.payload.tool_name == "mcp__fake__echo"
    ]
    assert len(starts) == 1
    # the recorded result for that call exists + succeeded
    res = [e for e in events if e.type == "ToolResultRecorded"]
    assert any(e.payload.success for e in res)


def test_default_off_no_mcp_in_schema(tmp_path: Path) -> None:
    # No enabled MCP server ⇒ no mcp__ entry in the recorded request tools.
    host, driver, enabled = _run(
        tmp_path,
        responses=[_call("c1", "glob", {"pattern": "*"}), _end()],
        mcp_servers=(),
    )
    out = driver.start(goal="use the mcp tool", agent="main", enabled_mcp=enabled)
    import json

    events = host.event_log.read(out.task_id)
    first_req = next(e for e in events if e.type == "LLMRequestStarted")
    body = host.content_store.get(first_req.payload.request_ref)
    tools = json.loads(body.decode("utf-8")).get("tools", [])
    assert not any(
        t.get("function", {}).get("name", "").startswith("mcp__") for t in tools
    )


# -- permission governs MCP (guard level) -----------------------------------


def test_guard_denies_mcp_when_max_risk_set() -> None:
    # noeta code does not set max_risk_level (so MCP runs by default), but
    # when a runtime DOES set it, the high-risk MCP tool is denied
    # fail-closed. Honest semantics: not auto-denied by being MCP.
    name = "mcp__fake__echo"
    tool = McpTool(
        name=name,
        remote_tool_name="echo",
        input_schema={},
        client=object(),  # type: ignore[arg-type]
    )
    guard = PermissionGuard(
        PermissionPolicy(allowed_tools=frozenset({name}), max_risk_level="medium"),
        tools={name: tool},
    )
    verdict = guard.check(
        ProposedToolCall(call=ToolCall(tool_name=name, arguments={}, call_id="c")),
        GuardContext(task_id="t"),
    ).verdict
    assert verdict is Verdict.DENY

    # With no max_risk_level the same call is allowed (no auto-deny).
    guard2 = PermissionGuard(
        PermissionPolicy(allowed_tools=frozenset({name})),
        tools={name: tool},
    )
    assert (
        guard2.check(
            ProposedToolCall(call=ToolCall(tool_name=name, arguments={}, call_id="c")),
            GuardContext(task_id="t"),
        ).verdict
        is Verdict.ALLOW
    )


# -- child does not inherit MCP (replay-inputs level) -----------------------


def test_child_replay_inputs_have_no_mcp(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    from noeta.storage.memory import InMemoryContentStore

    # The child runtime is built via build_code_replay_inputs WITHOUT
    # mcp_tool_specs (the SDK host's child build passes none unless the child
    # opts into MCP), so no mcp__ tool can appear in the child tool set / schema.
    child = build_code_replay_inputs(
        workspace_dir=ws,
        agent=official_specs()["main"],
        content_store=InMemoryContentStore(),
        model="gpt-test",
        max_steps=20,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    assert not any(name.startswith("mcp__") for name in child.tools)


# -- inline approval round-trip (live) --------------------------------------


def test_mcp_call_approval_round_trip(tmp_path: Path) -> None:
    host, driver, enabled = _run(
        tmp_path,
        responses=_mcp_call_responses(),
        mcp_servers=(_echo_spec(),),
        require_approval_tools=("mcp__fake__echo",),
    )
    out = driver.start(goal="use the mcp tool", agent="main", enabled_mcp=enabled)
    assert out.status == "suspended"
    assert out.wake_handle == "approval-c1"
    result = driver.approve(out.task_id, call_id="c1")
    assert result.status == "terminal"


# NOTE: the operator-CLI ``noeta code`` argument-parsing
# helpers (``_parse_mcp_server_arg``/``_mcp_servers_from_args``) and the
# command-internal ``_extract_mcp_tool_specs`` coverage check lived only in
# the now-deleted operator-CLI package. Their tests
# (``test_cli_parse_mcp_server_*``, ``test_cli_duplicate_alias_rejected``,
# ``test_extract_specs_missing_call_diverges``) exercised operator-CLI
# argparse/dispatch surface with no library-reachable behavior to retarget
# to, so they were removed rather than migrated.
