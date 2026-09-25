"""Deferred MCP schemas: a deferred server's tools stay registered under their
real names, but the provider sees two small tools — ``ToolSearch`` and
``McpCall`` — instead of every schema on every request.

Driven through the production SDK host with an in-process HTTP MCP server
(the injectable ``mcp_http_post``), so the build, the composer, the policy's
call routing, the Guard and the ToolRuntime are the real ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from noeta.client.host import SdkHost
from noeta.core.fold import fold
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.runtime.governance import PreToolUseRule
from noeta.runtime.mcp import McpConfigError, McpHttpServerSpec, McpServerSpec
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.testing.fake_llm import FakeLLMProvider

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


_TOOL_COUNT = 40


def _tool_entry(i: int) -> dict[str, Any]:
    """A realistically sized MCP tool: a few described properties."""
    if i == 7:
        return {
            "name": "weather_lookup",
            "description": "Look up the weather forecast for a city.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name."},
                    "days": {"type": "integer", "description": "Days ahead."},
                },
                "required": ["city"],
            },
        }
    return {
        "name": f"ledger_op_{i:02d}",
        "description": (
            f"Ledger operation {i:02d}: reads or updates records in the "
            "accounting ledger, filtered by account, period and status. "
            "Returns the matching rows as JSON."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Account identifier, e.g. ACC-1234.",
                },
                "period": {
                    "type": "string",
                    "description": "Accounting period as YYYY-MM.",
                },
                "status": {
                    "type": "string",
                    "enum": ["open", "closed", "pending"],
                    "description": "Only rows in this status.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of rows to return.",
                },
            },
            "required": ["account"],
        },
    }


class _FakeHttpMcp:
    """An ``HttpPostFn``: ``tools/list`` serves ``count`` tools, and
    ``tools/call`` echoes the raw tool name and arguments back."""

    def __init__(self, count: int = _TOOL_COUNT) -> None:
        self.tools = [_tool_entry(i) for i in range(count)]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, req: dict[str, Any], headers: Mapping[str, str]) -> bytes:
        method = req.get("method")
        if "id" not in req:
            return b""
        params = req.get("params") or {}
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
            }
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            self.calls.append((name, args))
            result = {
                "content": [
                    {"type": "text", "text": f"ran {name} with {json.dumps(args, sort_keys=True)}"}
                ]
            }
        else:
            result = {}
        return json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()


def _spec(alias: str = "ledger", *, deferred: bool) -> McpHttpServerSpec:
    return McpHttpServerSpec(alias=alias, url=f"http://127.0.0.1/{alias}", deferred=deferred)


def _call(*uses: tuple[str, str, dict[str, Any]]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id=cid, tool_name=name, arguments=args)
            for cid, name, args in uses
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": uses[0][0]},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": text},
    )


def _host(
    tmp_path: Path,
    responses: list[LLMResponse],
    *specs: McpHttpServerSpec,
    post: _FakeHttpMcp | None = None,
    **knobs: Any,
) -> tuple[SdkHost, Any, FakeLLMProvider, _FakeHttpMcp]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    table = {s.alias: s for s in specs}
    provider = FakeLLMProvider(responses=responses)
    fake = post or _FakeHttpMcp()
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        mcp_server_resolver=table.get,
        mcp_http_post=fake,
        **knobs,
    )
    return host, make_driver(host), provider, fake


def _tool_names(provider: FakeLLMProvider, index: int = 0) -> list[str]:
    return [t["function"]["name"] for t in provider.received_requests[index].tools]


def _mcp_names(names: list[str]) -> list[str]:
    return [n for n in names if n.startswith("mcp__")]


def _results(host: SdkHost, task_id: str) -> dict[str, ToolResultBlock]:
    task = fold(host.event_log, host.content_store, task_id)
    out: dict[str, ToolResultBlock] = {}
    for message in task.runtime.messages:
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                out[block.call_id] = block
    return out


def _events(host: SdkHost, task_id: str, kind: str) -> list[Any]:
    return [e.payload for e in host.event_log.read(task_id) if e.type == kind]


# ---------------------------------------------------------------------------
# the advertised tool list
# ---------------------------------------------------------------------------


def test_a_deferred_server_advertises_two_schemas_instead_of_forty(
    tmp_path: Path,
) -> None:
    host, driver, provider, _ = _host(tmp_path, [_end()], _spec(deferred=True))
    driver.start(goal="hi", agent="main", enabled_mcp=("ledger",))
    names = _tool_names(provider)
    assert _mcp_names(names) == []
    assert names.count("ToolSearch") == 1 and names.count("McpCall") == 1
    host.shutdown_mcp()


def test_a_plain_server_advertises_all_forty_and_no_search_pair(
    tmp_path: Path,
) -> None:
    host, driver, provider, _ = _host(tmp_path, [_end()], _spec(deferred=False))
    driver.start(goal="hi", agent="main", enabled_mcp=("ledger",))
    names = _tool_names(provider)
    assert len(_mcp_names(names)) == _TOOL_COUNT
    assert "ToolSearch" not in names and "McpCall" not in names
    host.shutdown_mcp()


def test_deferred_tools_stay_registered_under_their_real_names(
    tmp_path: Path,
) -> None:
    host, driver, _, _ = _host(tmp_path, [_end()], _spec(deferred=True))
    out = driver.start(goal="hi", agent="main", enabled_mcp=("ledger",))
    engine = host.resolve_engine(fold(host.event_log, host.content_store, out.task_id))
    registered = [n for n in engine._tools if n.startswith("mcp__")]
    assert len(registered) == _TOOL_COUNT
    assert "mcp__ledger__weather_lookup" in registered
    host.shutdown_mcp()


def test_mixed_deferred_and_plain_servers(tmp_path: Path) -> None:
    host, driver, provider, _ = _host(
        tmp_path,
        [_end()],
        _spec("ledger", deferred=True),
        _spec("plain", deferred=False),
    )
    driver.start(goal="hi", agent="main", enabled_mcp=("ledger", "plain"))
    names = _tool_names(provider)
    advertised = _mcp_names(names)
    assert len(advertised) == _TOOL_COUNT
    assert all(n.startswith("mcp__plain__") for n in advertised)
    assert names.count("ToolSearch") == 1 and names.count("McpCall") == 1
    host.shutdown_mcp()


def test_the_deferred_flag_is_validated() -> None:
    with pytest.raises(McpConfigError):
        McpServerSpec(alias="x", argv=("x",), deferred="yes")  # type: ignore[arg-type]
    with pytest.raises(McpConfigError):
        McpHttpServerSpec(alias="x", url="http://x", deferred=1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ToolSearch
# ---------------------------------------------------------------------------


def test_tool_search_returns_the_schema_for_a_query(tmp_path: Path) -> None:
    host, driver, _, _ = _host(
        tmp_path,
        [_call(("s1", "ToolSearch", {"query": "weather forecast"})), _end()],
        _spec(deferred=True),
    )
    out = driver.start(goal="weather?", agent="main", enabled_mcp=("ledger",))
    result = _results(host, out.task_id)["s1"]
    assert result.success
    found = json.loads(str(result.output))
    assert found[0]["name"] == "mcp__ledger__weather_lookup"
    assert found[0]["input_schema"]["required"] == ["city"]
    assert "weather" in found[0]["description"]
    host.shutdown_mcp()


def test_tool_search_with_an_empty_query_lists_every_name(tmp_path: Path) -> None:
    host, driver, _, _ = _host(
        tmp_path,
        [_call(("s1", "ToolSearch", {"query": ""})), _end()],
        _spec(deferred=True),
    )
    out = driver.start(goal="list", agent="main", enabled_mcp=("ledger",))
    listing = str(_results(host, out.task_id)["s1"].output)
    lines = listing.splitlines()
    assert len(lines) == _TOOL_COUNT
    assert any(line.startswith("mcp__ledger__weather_lookup — ") for line in lines)
    assert "input_schema" not in listing
    host.shutdown_mcp()


def test_tool_search_caps_its_matches() -> None:
    from noeta.builtins.mcp.impl.deferred import TOOL_SEARCH_MAX_RESULTS
    from noeta.builtins.mcp.impl.tool import build_mcp_tools

    tools, clients, _ = build_mcp_tools(
        (_spec(deferred=True),), http_post=_FakeHttpMcp()
    )
    try:
        found = tools["ToolSearch"].search("ledger accounting")
        assert len(found) == TOOL_SEARCH_MAX_RESULTS
        assert [t.name for t in found] == sorted(t.name for t in found)
        assert tools["ToolSearch"].search("mcp__ledger__ledger_op_03")[0].name == (
            "mcp__ledger__ledger_op_03"
        )
    finally:
        for c in clients:
            c.shutdown()


# ---------------------------------------------------------------------------
# McpCall
# ---------------------------------------------------------------------------


def test_mcp_call_runs_the_real_tool_and_pairs_by_call_id(tmp_path: Path) -> None:
    host, driver, _, fake = _host(
        tmp_path,
        [
            _call(
                (
                    "c1",
                    "McpCall",
                    {"tool": "mcp__ledger__weather_lookup", "arguments": {"city": "Oslo"}},
                )
            ),
            _end(),
        ],
        _spec(deferred=True),
    )
    out = driver.start(goal="weather?", agent="main", enabled_mcp=("ledger",))
    assert out.status == "terminal"
    assert fake.calls == [("weather_lookup", {"city": "Oslo"})]
    result = _results(host, out.task_id)["c1"]
    assert result.success and "ran weather_lookup" in json.dumps(result.output)
    # The recorded assistant turn keeps the McpCall the provider saw …
    task = fold(host.event_log, host.content_store, out.task_id)
    uses = [
        b
        for m in task.runtime.messages
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]
    assert [(u.call_id, u.tool_name) for u in uses] == [("c1", "McpCall")]
    # … while the audit trail names the real tool.
    started = _events(host, out.task_id, "ToolCallStarted")
    assert [p.tool_name for p in started] == ["mcp__ledger__weather_lookup"]
    host.shutdown_mcp()


@pytest.mark.parametrize(
    ("arguments", "needle"),
    [
        ({"tool": "mcp__ledger__nope", "arguments": {}}, "not a deferred tool"),
        ({"tool": "Read", "arguments": {}}, "No deferred tool named"),
        ({"tool": "mcp__ledger__weather_lookup"}, "needs `arguments`"),
        ({"arguments": {}}, "needs `tool`"),
    ],
)
def test_a_bad_mcp_call_is_a_per_call_error(
    tmp_path: Path, arguments: dict[str, Any], needle: str
) -> None:
    host, driver, _, fake = _host(
        tmp_path,
        [
            _call(
                ("bad", "McpCall", arguments),
                (
                    "ok",
                    "McpCall",
                    {"tool": "mcp__ledger__ledger_op_01", "arguments": {"account": "A"}},
                ),
            ),
            _end(),
        ],
        _spec(deferred=True),
    )
    out = driver.start(goal="x", agent="main", enabled_mcp=("ledger",))
    results = _results(host, out.task_id)
    assert not results["bad"].success and needle in (results["bad"].error or "")
    assert results["ok"].success
    assert fake.calls == [("ledger_op_01", {"account": "A"})]
    host.shutdown_mcp()


def test_a_non_deferred_tool_named_through_mcp_call_is_refused(
    tmp_path: Path,
) -> None:
    host, driver, _, fake = _host(
        tmp_path,
        [
            _call(
                (
                    "c1",
                    "McpCall",
                    {"tool": "mcp__plain__weather_lookup", "arguments": {"city": "Oslo"}},
                )
            ),
            _end(),
        ],
        _spec("ledger", deferred=True),
        _spec("plain", deferred=False),
    )
    out = driver.start(goal="x", agent="main", enabled_mcp=("ledger", "plain"))
    result = _results(host, out.task_id)["c1"]
    assert not result.success and "not a deferred tool" in (result.error or "")
    assert fake.calls == []
    host.shutdown_mcp()


def test_approval_gates_the_real_name_behind_mcp_call(tmp_path: Path) -> None:
    host, driver, _, fake = _host(
        tmp_path,
        [
            _call(
                (
                    "c1",
                    "McpCall",
                    {"tool": "mcp__ledger__weather_lookup", "arguments": {"city": "Oslo"}},
                )
            ),
            _end(),
        ],
        _spec(deferred=True),
        require_approval_tools=("mcp__ledger__weather_lookup",),
    )
    out = driver.start(goal="x", agent="main", enabled_mcp=("ledger",))
    assert out.status == "suspended" and out.wake_handle == "approval-c1"
    requested = _events(host, out.task_id, "ToolCallApprovalRequested")
    assert [p.tool_name for p in requested] == ["mcp__ledger__weather_lookup"]
    assert fake.calls == []

    # A fresh host over the same stores — a restart — resumes the approval.
    restarted = SdkHost(
        event_log=host.event_log,
        content_store=host.content_store,
        dispatcher=host.dispatcher,
        provider=host.providers[host.default_provider],
        model=host.model,
        workspace_dir=host.workspace_dir,
        registry=host.registry,
        aliases=host.aliases,
        policy_wrapper=host.policy_wrapper,
        write_mode=host.write_mode,
        shell_mode=host.shell_mode,
        require_approval_tools=host.require_approval_tools,
        mcp_server_resolver=host.mcp_server_resolver,
        mcp_http_post=host.mcp_http_post,
    )
    before = fold(host.event_log, host.content_store, out.task_id).runtime.messages
    done = make_driver(restarted).approve(out.task_id, call_id="c1")
    assert done.status == "terminal"
    assert fake.calls == [("weather_lookup", {"city": "Oslo"})]
    after = fold(restarted.event_log, restarted.content_store, out.task_id)
    # The resumed history extends the recorded one, and the McpCall tool_use
    # pairs with the real tool's result by call_id.
    assert after.runtime.messages[: len(before)] == before
    assert _results(restarted, out.task_id)["c1"].success
    restarted.shutdown_mcp()
    host.shutdown_mcp()


def test_a_deny_rule_on_the_real_name_blocks_mcp_call(tmp_path: Path) -> None:
    host, driver, _, fake = _host(
        tmp_path,
        [
            _call(
                (
                    "c1",
                    "McpCall",
                    {"tool": "mcp__ledger__weather_lookup", "arguments": {"city": "Oslo"}},
                )
            ),
            _end(),
        ],
        _spec(deferred=True),
        hooks_pre_tool_use=(
            PreToolUseRule(match_tool="mcp__ledger__weather*", action="deny", reason="no weather"),
        ),
    )
    out = driver.start(goal="x", agent="main", enabled_mcp=("ledger",))
    denied = _events(host, out.task_id, "ToolCallDenied")
    assert [p.tool_name for p in denied] == ["mcp__ledger__weather_lookup"]
    assert fake.calls == []
    assert not _results(host, out.task_id)["c1"].success
    host.shutdown_mcp()


def test_mcp_call_rides_a_mixed_batch(tmp_path: Path) -> None:
    host, driver, _, fake = _host(
        tmp_path,
        [
            _call(
                ("s1", "ToolSearch", {"query": "weather"}),
                (
                    "c1",
                    "McpCall",
                    {"tool": "mcp__ledger__weather_lookup", "arguments": {"city": "Oslo"}},
                ),
            ),
            _end(),
        ],
        _spec(deferred=True),
    )
    out = driver.start(goal="x", agent="main", enabled_mcp=("ledger",))
    results = _results(host, out.task_id)
    assert results["s1"].success and results["c1"].success
    assert fake.calls == [("weather_lookup", {"city": "Oslo"})]
    host.shutdown_mcp()


def test_an_unrouted_mcp_call_refuses_to_run() -> None:
    from noeta.builtins.mcp.impl.tool import build_mcp_tools

    tools, clients, _ = build_mcp_tools((_spec(deferred=True),), http_post=_FakeHttpMcp())
    try:
        result = tools["McpCall"].invoke(
            {"tool": "mcp__ledger__weather_lookup", "arguments": {"city": "x"}},
            None,  # type: ignore[arg-type]
        )
        assert not result.success
    finally:
        for c in clients:
            c.shutdown()


# ---------------------------------------------------------------------------
# the stable prefix
# ---------------------------------------------------------------------------


def _prefix_bytes(tmp_path: Path, *, deferred: bool) -> tuple[int, int]:
    host, driver, provider, _ = _host(
        tmp_path / ("d" if deferred else "p"), [_end()], _spec(deferred=deferred)
    )
    driver.start(goal="hi", agent="main", enabled_mcp=("ledger",))
    tools = provider.received_requests[0].tools
    mcp_side = [
        t
        for t in tools
        if t["function"]["name"].startswith("mcp__")
        or t["function"]["name"] in ("ToolSearch", "McpCall")
    ]
    host.shutdown_mcp()
    return len(to_canonical_bytes(tools)), len(to_canonical_bytes(mcp_side))


def test_deferring_shrinks_the_stable_prefix(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "p").mkdir()
    deferred_all, deferred_mcp = _prefix_bytes(tmp_path, deferred=True)
    plain_all, plain_mcp = _prefix_bytes(tmp_path, deferred=False)
    print(
        f"\nprovider_tool_schemas bytes — plain: {plain_all} (MCP {plain_mcp}); "
        f"deferred: {deferred_all} (MCP {deferred_mcp})"
    )
    assert deferred_mcp * 10 < plain_mcp
    assert plain_all - deferred_all == plain_mcp - deferred_mcp
