"""Phase 4.5 F2 — MCP name mapping, wrapper result mapping, spec extraction.

Pure-unit coverage of the provider-safe name mapping + collision
fail-fast, the `McpTool.invoke` result mapping, and the R-1 spec
extraction (filter + order).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.mcp import (
    McpConfigError,
    McpServerSpec,
    build_mcp_tools,
    make_mcp_tool_name,
    parse_mcp_tool_specs,
)
from noeta.tools.mcp.tool import _result_to_tool_result


_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")


def _ctx() -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore())


# -- name mapping -----------------------------------------------------------


def test_make_name_sanitizes() -> None:
    assert make_mcp_tool_name("git", "commit") == "mcp__git__commit"
    assert make_mcp_tool_name("x", "a.b/c d") == "mcp__x__a_b_c_d"


def test_make_name_rejects_empty_and_nonstr() -> None:
    for bad in ("", None, 123):
        with pytest.raises(McpConfigError):
            make_mcp_tool_name("x", bad)


def test_unsafe_only_name_maps_to_underscores_not_boundary() -> None:
    # Unsafe chars are REPLACED with `_` (never stripped), so `safe` is
    # non-empty for any non-empty raw → no `mcp__x__` boundary name is
    # ever produced. (The `safe == ""` fail-fast guard is defensive for
    # the raw=="" case, which the non-str/empty check already rejects.)
    name = make_mcp_tool_name("x", "！！！")  # 3 unsafe chars → 3 underscores
    assert name.startswith("mcp__x__") and len(name) > len("mcp__x__")


def test_make_name_rejects_overlong() -> None:
    with pytest.raises(McpConfigError):
        make_mcp_tool_name("x", "t" * 80)


def test_server_spec_validates_alias() -> None:
    with pytest.raises(McpConfigError):
        McpServerSpec(alias="BadAlias", argv=("cmd",))
    with pytest.raises(McpConfigError):
        McpServerSpec(alias="ok", argv=())


# -- build_mcp_tools (live, against fake server) ----------------------------


def _spec(mode: str, alias: str = "fake") -> McpServerSpec:
    return McpServerSpec(alias=alias, argv=(sys.executable, "-u", _FAKE, mode))


def test_build_discovers_and_namespaces() -> None:
    tools, clients, _skipped = build_mcp_tools((_spec("echo"),))
    try:
        assert "mcp__fake__echo" in tools
        t = tools["mcp__fake__echo"]
        assert t.risk_level == "high"
        assert t.remote_tool_name == "echo"
    finally:
        for c in clients:
            c.shutdown()


def test_build_collision_fails_fast_and_reaps() -> None:
    with pytest.raises(McpConfigError):
        build_mcp_tools((_spec("collision"),))
    # (clients are reaped inside build_mcp_tools on the raising path)


def test_build_empty_name_fails_fast() -> None:
    with pytest.raises(McpConfigError):
        build_mcp_tools((_spec("empty_name"),))


def test_build_duplicate_alias_fails() -> None:
    with pytest.raises(McpConfigError):
        build_mcp_tools((_spec("echo", "dup"), _spec("echo", "dup")))


def test_build_empty_is_noop() -> None:
    tools, clients, _skipped = build_mcp_tools(())
    assert tools == {} and clients == []


# -- per-server tool subset (issue 02) -------------------------


def _multi_spec(
    *, subset: "tuple[str, ...] | None" = None, alias: str = "fake"
) -> McpServerSpec:
    return McpServerSpec(
        alias=alias,
        argv=(sys.executable, "-u", _FAKE, "multi"),
        tool_subset=subset,
    )


def test_build_subset_none_keeps_all() -> None:
    tools, clients, _skipped = build_mcp_tools((_multi_spec(subset=None),))
    try:
        assert set(tools) == {
            "mcp__fake__alpha",
            "mcp__fake__beta",
            "mcp__fake__gamma",
        }
    finally:
        for c in clients:
            c.shutdown()


def test_build_subset_keeps_only_ticked_tools() -> None:
    # Tick alpha + gamma; beta must NOT enter the tool set (never reaches model).
    tools, clients, _skipped = build_mcp_tools((_multi_spec(subset=("alpha", "gamma")),))
    try:
        assert set(tools) == {"mcp__fake__alpha", "mcp__fake__gamma"}
        assert "mcp__fake__beta" not in tools
    finally:
        for c in clients:
            c.shutdown()


def test_build_subset_empty_keeps_nothing() -> None:
    # An explicit empty subset is distinct from None: zero tools enter.
    tools, clients, _skipped = build_mcp_tools((_multi_spec(subset=()),))
    try:
        assert tools == {}
    finally:
        for c in clients:
            c.shutdown()


def test_build_subset_unknown_name_is_silently_dropped() -> None:
    # A ticked name the server no longer advertises just yields no tool —
    # never a fail-fast (the live world may have dropped it).
    tools, clients, _skipped = build_mcp_tools((_multi_spec(subset=("alpha", "ghost")),))
    try:
        assert set(tools) == {"mcp__fake__alpha"}
    finally:
        for c in clients:
            c.shutdown()


def test_build_stdio_env_reaches_spawn() -> None:
    spec = McpServerSpec(
        alias="fake",
        argv=(sys.executable, "-u", _FAKE, "envcheck"),
        env=(("FAKE_TOKEN", "sekret"),),
    )
    tools, clients, _skipped = build_mcp_tools((spec,))
    try:
        # The fixture reflects FAKE_TOKEN back as the tool description.
        assert tools["mcp__fake__seen"].description == "sekret"
    finally:
        for c in clients:
            c.shutdown()


# -- result mapping ---------------------------------------------------------


def test_result_mapping_text_ok() -> None:
    res = _result_to_tool_result(
        "mcp__x__t", {"content": [{"type": "text", "text": "hello"}]}, _ctx()
    )
    assert res.success is True
    assert res.output["text"] == "hello"


def test_result_mapping_is_error() -> None:
    res = _result_to_tool_result(
        "mcp__x__t",
        {"content": [{"type": "text", "text": "boom"}], "isError": True},
        _ctx(),
    )
    assert res.success is False


def test_result_mapping_large_text_offloads() -> None:
    big = "z" * (80 * 1024)  # over the 64 KB content budget
    res = _result_to_tool_result(
        "mcp__x__t", {"content": [{"type": "text", "text": big}]}, _ctx()
    )
    assert res.success is True
    assert res.artifacts  # offloaded to a ContentStore artifact
    assert "text_ref" in res.output  # ref handed back so the model can deref


def test_result_mapping_counts_non_text() -> None:
    res = _result_to_tool_result(
        "mcp__x__t",
        {"content": [{"type": "image", "data": "..."}]},
        _ctx(),
    )
    assert res.output.get("non_text_blocks") == 1


# -- R-1 extraction ---------------------------------------------------------


def _req_tools(*names: str) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": n, "parameters": {"k": n}}}
        for n in names
    ]


def test_parse_specs_filters_mcp_in_order() -> None:
    tools = _req_tools("read_file", "mcp__a__one", "spawn_subagent", "mcp__a__two")
    specs = parse_mcp_tool_specs(tools)
    assert [s.name for s in specs] == ["mcp__a__one", "mcp__a__two"]
    assert specs[0].input_schema == {"k": "mcp__a__one"}
