"""Agent files: the frontmatter, the body kept byte for byte, plugins by
entry-point name (a third-party one the same as a built-in), sharing."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest

from noeta.agents.agent_files import (
    AgentFileError,
    build_agents,
    parse_agent_file,
    read_agent_dir,
)
from noeta.core import registry
from noeta.core.extension import Extension

FULL = """---
name: reviewer
description: Reviews a diff.
model: some-model
one_shot: true
effort: high
tools: [Read, Grep]
plugins:
  - fs: {tools: [Read, Grep]}
  - instructions
color: blue
---
You review.

Twice.
"""


def test_parse_keeps_the_body_and_reads_every_field() -> None:
    f = parse_agent_file(FULL, source="x")
    assert f.name == "reviewer" and f.description == "Reviews a diff."
    assert f.prompt == "You review.\n\nTwice.\n"
    assert f.model == "some-model" and f.one_shot and f.effort == "high"
    assert f.tools == ("Read", "Grep")
    assert f.plugins == (("fs", {"tools": ["Read", "Grep"]}), ("instructions", {}))
    assert f.extra == {"color": "blue"}


def test_claude_code_agent_file() -> None:
    f = parse_agent_file("---\nname: helper\ndescription: Helps.\ntools: Read, Bash\n"
                         "model: sonnet\n---\nHelp.\n")
    assert f.tools == ("Read", "Bash") and f.model == "sonnet" and f.plugins is None


def test_identity_line_defaults_and_goes_first_as_its_own_cached_block() -> None:
    from noeta.agents.agent_files import DEFAULT_IDENTITY
    from noeta.core.loop.request import build_request
    from noeta.core.loop.state import TaskState

    own = parse_agent_file("---\nname: a\nidentity: You are A.\nplugins: []\n---\nBody.\n")
    plain = parse_agent_file("---\nname: b\nplugins: []\n---\nBody.\n")
    a, b = build_agents([own, plain], model="m")
    assert own.identity == "You are A." and a.identity == "You are A."
    assert b.identity == DEFAULT_IDENTITY
    request = build_request(TaskState(), model="m", system_prompt=b.system_prompt,
                            identity=b.identity, parts=[], tools=[])
    assert [(p.text, p.cache) for p in request.system] == [
        (DEFAULT_IDENTITY, True), ("Body.\n", True)]


@pytest.mark.parametrize("text", [
    "no frontmatter\n",
    "---\nname: x\n",
    "---\n: [\n---\nbody",
    "---\n- a\n---\nbody",
    "---\ndescription: d\n---\nbody",
    "---\nname: x\nplugins: fs\n---\n",
    "---\nname: x\nplugins: [{fs: [1]}]\n---\n",
    "---\nname: x\none_shot: maybe\n---\n",
])
def test_bad_files(text: str) -> None:
    with pytest.raises(AgentFileError):
        parse_agent_file(text)


def test_read_agent_dir_skips_bad_files(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text("---\ndescription: d\n---\nP\n")
    (tmp_path / "bad.md").write_text("nothing\n")
    [f] = read_agent_dir(tmp_path)
    assert f.name == "good"  # the file name when the frontmatter has none
    assert read_agent_dir(tmp_path / "missing") == []


class Greeter(Extension):
    name = "greeter"

    def __init__(self, greeting: str = "hi") -> None:
        self.greeting = greeting


def test_a_third_party_plugin_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    eps = {"greeter": EntryPoint("greeter", f"{__name__}:Greeter", registry.PLUGINS)}
    real = registry._group

    def group(name: str) -> dict[str, Any]:
        return eps if name == registry.PLUGINS else real(name)

    monkeypatch.setattr(registry, "_group", group)
    files = [parse_agent_file("---\nname: a\nplugins: [{greeter: {greeting: hello}}]\n---\nP\n"),
             parse_agent_file("---\nname: b\nplugins: [{greeter: {greeting: hello}}]\n---\nP\n"),
             parse_agent_file("---\nname: c\nplugins: [greeter]\n---\nP\n")]
    a, b, c = build_agents(files, model="m", plugins={"greeter": {"greeting": "hey"},
                                                      "tool_search": False})
    assert a.extensions[0] is b.extensions[0]  # same arguments, one instance
    assert a.extensions[0].greeting == "hello"  # type: ignore[attr-defined]
    assert c.extensions[0].greeting == "hey"  # type: ignore[attr-defined]
    with pytest.raises(registry.UnknownName, match="available: greeter"):
        build_agents([parse_agent_file("---\nname: d\nplugins: [nope]\n---\n")], model="m")


def test_settings_defaults_additions_and_models() -> None:
    shared = Greeter()
    files = [parse_agent_file("---\nname: a\nplugins: [greeter, fs]\n---\nP\n"),
             parse_agent_file("---\nname: b\nmodel: other\n---\nQ\n")]
    a, b = build_agents(files, model="m", plugins={"greeter": shared, "fs": False,
                                                   "tool_search": False},
                        default_plugins=[("greeter", {})])
    assert list(a.extensions) == [shared] and a.model == "m"
    assert list(b.extensions) == [shared] and b.model == "other"
    a, b = build_agents(files, model="m", plugins={"fs": {"enabled": False}, "greeter": shared,
                                                   "tool_search": False},
                        add={"b": [("instructions", {})], "*": [("greeter", {})]})
    assert [e.name for e in a.extensions] == ["greeter"]  # not added twice
    assert [e.name for e in b.extensions] == ["instructions", "greeter"]
    with pytest.raises(ValueError, match="enabled"):
        build_agents(files, model="m", plugins={"fs": {"enabled": "no"}})
    with pytest.raises(TypeError):
        build_agents(files, model="m", plugins={"fs": 3})  # type: ignore[dict-item]


def test_claude_code_fields_map_onto_the_agent(caplog: pytest.LogCaptureFixture) -> None:
    f = parse_agent_file(
        "---\nname: fixer\ndescription: Fixes.\ntools: Read, Bash(npm:*), Edit\n"
        "disallowedTools: [Write]\nmodel: haiku\npermissionMode: acceptEdits\nmaxTurns: 7\n"
        "omitClaudeMd: true\nisolation: worktree\nskills: [a]\nflavour: x\n---\nFix.\n",
        source="f.md")
    assert f.tools == ("Read", "Bash", "Edit") and f.denied_tools == ("Write",)
    assert f.permission_mode == "acceptEdits" and f.max_turns == 7 and f.omit_instructions
    assert f.extra == {"isolation": "worktree", "skills": ["a"], "flavour": "x"}
    logged = caplog.text
    assert "'flavour' is ignored" in logged and "'skills'" not in logged
    assert "isolation" not in logged
    [agent] = build_agents([f], model="host", plugins={"skills": False, "tool_search": False},
                           default_plugins=[("instructions", {})])
    assert agent.model == "claude-haiku-5-5" and agent.denied_tools == ("Write",)
    assert agent.step_budget == 7 and agent.extensions == ()
    # What only the sub-agent plugin acts on stays in the file's fields.
    assert agent.extra["permissionMode"] == "acceptEdits"
    assert agent.extra["model"] == "haiku"
    assert agent.extra["isolation"] == "worktree"
    [plain] = build_agents([parse_agent_file("---\nname: p\ndescription: d\n---\nx\n")],
                           model="host", default_plugins=[])
    assert plain.model == "host" and plain.extra == {"model": "inherit"}


def test_tools_star_and_inherit() -> None:
    f = parse_agent_file("---\nname: a\ntools: '*'\nmodel: inherit\n---\n")
    assert f.tools is None and f.model is None
    [agent] = build_agents([f], model="host")
    assert agent.model == "host" and agent.step_budget == 1_000


@pytest.mark.parametrize("text", [
    "---\nname: a:b\n---\n",
    "---\nname: a\nmaxTurns: 0\n---\n",
    "---\nname: a\nomitClaudeMd: sometimes\n---\n",
    "---\nname: a\npermissionMode: [x]\n---\n",
])
def test_bad_claude_code_fields(text: str) -> None:
    with pytest.raises(AgentFileError):
        parse_agent_file(text)


def test_claude_code_fields_become_plugin_arguments(caplog: pytest.LogCaptureFixture) -> None:
    from noeta.agents.agent_files import plugin_overlays

    f = parse_agent_file(
        "---\nname: rev\nskills: a, b\nmemory: project\nautoCompactWindow: 120k\n"
        "mcpServers:\n  - github\n  - local: {command: srv}\n"
        "hooks: {Stop: [{hooks: [{type: command, command: 'true'}]}]}\n"
        "initialPrompt: Start here.\nbackground: true\ncacheTtl: 1h\n---\nP\n",
        source="overlay-rev.md")
    stop = {"hooks": [{"type": "command", "command": "true"}]}
    assert plugin_overlays(f) == [
        ("skills", {"preload": ["a", "b"]}),
        ("mcp", {"agent_servers": {"local": {"command": "srv"}}}),
        ("hooks", {"hooks": {"Stop": [stop], "SubagentStop": [stop]}}),
        ("memory", {"agent_scope": "project", "agent_name": "rev"}),
        ("compaction", {"auto_compact_window": 120_000}),
    ]
    assert "'cacheTtl' is not applied" in caplog.text
    assert "initialPrompt" not in caplog.text and "background" not in caplog.text
    caplog.clear()
    parse_agent_file("---\nname: rev\ncacheTtl: 1h\n---\nP\n", source="overlay-rev.md")
    assert "cacheTtl" not in caplog.text  # once per file


def test_overlays_add_to_the_hosts_arguments_and_the_plugin() -> None:
    host_hooks = {"Stop": [{"hooks": [{"type": "command", "command": "host"}]}]}
    f = parse_agent_file(
        "---\nname: rev\nskills: [a]\n"
        "hooks: {Stop: [{hooks: [{type: command, command: own}]}]}\n---\nP\n")
    [agent] = build_agents([f], model="m", plugins={"hooks": {"hooks": host_hooks}},
                           default_plugins=[("hooks", {}), ("compaction", {})])
    assert [e.name for e in agent.extensions] == ["hooks", "tool_search", "skills", "compaction"]
    hooks = agent.extensions[0]
    assert [h.command for m in hooks.matchers["Stop"]  # type: ignore[attr-defined]
            for h in m.hooks] == ["host", "own"]
    assert agent.extensions[2].preload == ["a"]  # type: ignore[attr-defined]
    [off] = build_agents([f], model="m", plugins={"skills": False, "tool_search": False},
                         default_plugins=[("hooks", {})])
    assert [e.name for e in off.extensions] == ["hooks"]


def test_agent_mcp_servers_come_first() -> None:
    from noeta.plugins.mcp import Mcp

    f = parse_agent_file("---\nname: rev\nmcpServers:\n  - local: {command: own}\n---\nP\n")
    [agent] = build_agents([f], model="m", plugins={"mcp": {"servers": {
        "local": {"command": "host"}, "other": {"command": "x"}}}, "tool_search": False},
        default_plugins=[])
    [mcp] = agent.extensions
    assert isinstance(mcp, Mcp)
    assert [(s.alias, s.command) for s in mcp._agent_servers] == [("local", ["own"])]


@pytest.mark.parametrize("text", [
    "---\nname: a\nmemory: team\n---\n",
    "---\nname: a\nmcpServers: x\n---\n",
    "---\nname: a\nhooks: [x]\n---\n",
    "---\nname: a\ninitialPrompt: [x]\n---\n",
    "---\nname: a\nautoCompactWindow: lots\n---\n",
])
def test_bad_applied_fields(text: str) -> None:
    with pytest.raises(AgentFileError):
        parse_agent_file(text)


def test_every_agent_gets_tool_search_unless_the_host_turns_it_off() -> None:
    f = parse_agent_file("---\nname: a\nplugins: [fs, compaction]\n---\nP\n")
    [agent] = build_agents([f], model="m")
    assert [e.name for e in agent.extensions] == ["fs", "tool_search", "compaction"]
    [off] = build_agents([f], model="m", plugins={"tool_search": False})
    assert [e.name for e in off.extensions] == ["fs", "compaction"]
