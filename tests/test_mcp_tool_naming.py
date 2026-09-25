"""MCP tool naming: shorten, disambiguate, and refuse only across servers.

An over-long raw name, or two raw names of one server that sanitize alike,
used to drop the whole server. Now they get a stable hash suffix under the
``mcp__<alias>__`` prefix; only a collision between two servers (which an
alias rename fixes) is a build-time ``McpConfigError``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from noeta.builtins.mcp.impl import build_mcp_tools, make_mcp_tool_name
from noeta.builtins.mcp.impl.tool import _map_server_names
from noeta.runtime.mcp import McpConfigError, McpServerSpec

_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")


def test_overlong_name_keeps_prefix_and_fits() -> None:
    alias = "a" * 32
    name = make_mcp_tool_name(alias, "tool-" + "z" * 100)
    assert len(name) == 64
    assert name.startswith(f"mcp__{alias}__tool-z")
    assert name[-9] == "_" and all(ch in "0123456789abcdef" for ch in name[-8:])


def test_short_name_unchanged() -> None:
    assert make_mcp_tool_name("git", "commit") == "mcp__git__commit"


def test_intra_server_collision_keeps_plain_name_and_suffixes_the_rest() -> None:
    names = _map_server_names("s", ["x.y", "x_y", "x/y"])
    assert names["x_y"] == "mcp__s__x_y"
    assert names["x.y"].startswith("mcp__s__x_y_") and names["x/y"].startswith("mcp__s__x_y_")
    assert len(set(names.values())) == 3
    # Order-independent.
    assert _map_server_names("s", ["x/y", "x_y", "x.y"]) == names


def test_overlong_tool_does_not_skip_the_server() -> None:
    spec = McpServerSpec(alias="fake", argv=(sys.executable, "-u", _FAKE, "longname"))
    tools, clients, skipped = build_mcp_tools((spec,), skip_on_failure=True)
    try:
        assert skipped == []
        assert "mcp__fake__ok" in tools and len(tools) == 2
        assert {t.remote_tool_name for t in tools.values()} == {"ok", "x" * 60}
    finally:
        for c in clients:
            c.shutdown()


class _Client:
    def __init__(self, names: list[str]) -> None:
        self._names = names
        self.shutdowns = 0

    def start(self) -> None:
        pass

    def list_tools(self) -> list[dict[str, object]]:
        return [{"name": n} for n in self._names]

    def shutdown(self) -> None:
        self.shutdowns += 1


@pytest.mark.parametrize("skip_on_failure", [False, True])
def test_cross_server_collision_raises_naming_both(
    monkeypatch: pytest.MonkeyPatch, skip_on_failure: bool
) -> None:
    from noeta.builtins.mcp.impl import tool as tool_mod

    made: list[_Client] = []

    def fake_connect(spec, **_kw):  # type: ignore[no-untyped-def]
        c = _Client(["b__t1"] if spec.alias == "a" else ["t1"])
        made.append(c)
        return c

    monkeypatch.setattr(tool_mod, "_connect_client", fake_connect)
    specs = (
        McpServerSpec(alias="a", argv=("srv",)),
        McpServerSpec(alias="a__b", argv=("srv",)),
    )
    with pytest.raises(McpConfigError) as info:
        build_mcp_tools(specs, skip_on_failure=skip_on_failure)
    msg = str(info.value)
    assert "mcp__a__b__t1" in msg and "'a'" in msg and "'a__b'" in msg
    assert [c.shutdowns for c in made] == [1, 1]
