"""MCP tools' old names (``noeta.core.mcp_names``): settings written under
noeta's earlier naming (alias lower-cased and cut to 32 characters, names
cut to 64 with a hash) keep reaching the tool they named."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from noeta.core import mcp_names
from noeta.core.extension import Extension, TaskView, offer_tools
from noeta.core.loop.state import TaskState
from noeta.core.mcp_names import (
    allowed_in,
    legacy_index,
    legacy_names,
    named_in,
    old_name,
    remember,
    short_name,
)
from noeta.plugins.mcp.config import McpServer
from noeta.plugins.mcp.extension import _server_entry


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def test_the_old_forms_of_a_name() -> None:
    assert legacy_names("mcp__GitHub__delete_repo") == ("mcp__github__delete_repo",)
    assert legacy_names("mcp__github__delete_repo") == ()  # unchanged
    assert legacy_names("Bash") == ()
    alias = "A" * 40
    assert legacy_names(f"mcp__{alias}__t") == (f"mcp__{'a' * 32}__t",)
    raw = "q" * 70
    assert legacy_names(f"mcp__gh__{raw}") == (f"mcp__gh__{raw[:46]}_{_sha(raw)}",)
    # An alias holding "__": each split is tried.
    assert "mcp__x__y__t" in legacy_names("mcp__X__Y__t")


def test_a_long_name_whose_raw_form_had_unsafe_characters() -> None:
    # The old hash was of the raw name: the MCP plugin computes that form,
    # records it with the tool, and the process learns it when offering.
    raw = "x" * 60 + ".y"
    entry = _server_entry(McpServer(alias="GitHub", command=["x"]), [{"name": raw}])
    tool = entry["tools"][0]
    old = f"mcp__github__{('x' * 60 + '_y')[:42]}_{_sha(raw)}"
    assert tool["old_name"] == old == old_name("GitHub", "x" * 60 + "_y", raw)
    remember(tool["name"], old)
    assert old in legacy_names(tool["name"])


def test_named_in_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    names = {"mcp__srv__a"}
    assert named_in(names, "mcp__Srv__a", "test list")
    assert named_in(names, "mcp__Srv__a", "test list")
    assert not named_in(names, "mcp__Srv__b", "test list")
    assert caplog.text.count("only by its old name") == 1


def test_allowed_in_takes_the_current_name_only(caplog: pytest.LogCaptureFixture) -> None:
    assert allowed_in({"mcp__srv__a"}, "mcp__srv__a", "test list")
    assert not allowed_in({"mcp__srv__a"}, "mcp__Srv__a", "test list")
    assert "never lets a tool through" in caplog.text


def test_an_old_name_two_tools_share_picks_neither() -> None:
    """``mcp__GitHub__x`` and ``mcp__GITHUB__x`` both were ``mcp__github__x``:
    whatever their order, that old name resolves to no tool; an old name
    that is another tool's current name resolves to none either."""
    for names in (["mcp__GitHub__x", "mcp__GITHUB__x"], ["mcp__GITHUB__x", "mcp__GitHub__x"]):
        assert "mcp__github__x" not in legacy_index(names)
    assert legacy_index(["mcp__GitHub__x"]) == {"mcp__github__x": "mcp__GitHub__x"}
    assert legacy_index(["mcp__GitHub__x", "mcp__github__x"]) == {}


def test_remembered_old_names_accumulate_and_age_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two servers giving one current name to tools of different raw names
    keep both old forms (each only narrows); past the cap the least
    recently offered goes first."""
    monkeypatch.setattr(mcp_names, "_remembered", type(mcp_names._remembered)())
    monkeypatch.setattr(mcp_names, "_REMEMBER_MAX", 2)
    remember("mcp__a__t", "old1")
    remember("mcp__a__t", "old2")
    assert {"old1", "old2"} <= set(legacy_names("mcp__a__t"))
    remember("mcp__b__t", "oldb")
    remember("mcp__a__t", "old1")  # offered again: kept
    remember("mcp__c__t", "oldc")
    assert "oldb" not in legacy_names("mcp__b__t")
    assert "old1" in legacy_names("mcp__a__t")


def test_the_short_name_on_a_64_character_wire() -> None:
    long = "mcp__s__" + "x" * 80
    assert short_name("Bash") == "Bash"
    assert len(short_name(long)) == 64 and short_name(long).endswith(_sha(long))


def test_an_agents_tool_lists_by_an_old_name_refuse_never_offer() -> None:
    class Fake:
        description = "d"
        input_schema: dict[str, Any] = {"type": "object"}

        def __init__(self, name: str) -> None:
            self.name = name

        async def run(self, input: dict[str, Any], ctx: Any) -> Any:
            raise AssertionError

    class Tools(Extension):
        name = "t"

        def tools(self, view: TaskView) -> list[Any]:
            return [Fake("Read"), Fake("mcp__GitHub__delete_repo"), Fake("mcp__GitHub__list")]

    view = TaskView(state=TaskState(), extensions=[Tools()])
    denied = offer_tools([Tools()], view, None, ["mcp__github__delete_repo"])
    assert [t.name for t in denied.tools] == ["Read", "mcp__GitHub__list"]
    # An agent's tools: list by an old name offers nothing: it may name a
    # second server's tool.
    allowed = offer_tools([Tools()], view, ["Read", "mcp__github__list"], [])
    assert [t.name for t in allowed.tools] == ["Read"]
    listed = TaskView(state=TaskState(), extensions=[Tools()],
                      allowed_tools=["mcp__github__list"])
    assert listed.find_tool("mcp__GitHub__list") is None


def test_a_terminal_mcp_tool_named_the_old_way(monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.plugins.mcp.tools import McpTool

    async def invoke(*_a: Any) -> Any:
        raise AssertionError

    monkeypatch.setenv("NOETA_TERMINAL_MCP_TOOLS", "mcp__github__done")
    tool = McpTool(name="mcp__GitHub__done", alias="GitHub", remote="done", description="d",
                   input_schema={}, invoke=invoke)
    assert tool.silent_reply_after == "success"


def test_a_skills_allowed_tools_grant_takes_the_current_name_only() -> None:
    from noeta.core.extension import Deny
    from noeta.plugins.skills.gates import allowed_tools_gate

    active = {"s": {"allowed_tools": ["mcp__github"]}}
    assert allowed_tools_gate("mcp__github__x", active, "deny") is None
    assert isinstance(allowed_tools_gate("mcp__GitHub__x", active, "deny"), Deny)
