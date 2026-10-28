"""The built-in agents: read-only tool sets for explore and plan, no
delegation below main, main's roster from the sub-agents' descriptions."""

from __future__ import annotations

from typing import Any

from noeta.agents import builtin_agent_files, default_agents
from noeta.agents.agent_files import parse_agent_file
from noeta.core.agent import Agent
from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.plugins.mcp import Mcp
from noeta.plugins.subtasks import Subtasks


def _agents(**kw: Any) -> dict[str, Agent]:
    return {a.name: a for a in default_agents("m", **kw)}


def _tools(agent: Agent, agents: dict[str, Agent]) -> set[str]:
    view = TaskView(state=TaskState(), agents=agents)
    return {t.name for e in agent.all_extensions() for t in e.tools(view)}


def test_built_in_agent_files() -> None:
    names = [f.name for f in builtin_agent_files()]
    assert names == ["main", "explore", "general-purpose", "plan"]


def test_tool_sets_and_no_delegation() -> None:
    agents = _agents()
    assert {"Edit", "Glob", "Grep", "Read", "Write", "Bash"} <= _tools(
        agents["general-purpose"], agents)
    for name in ("explore", "plan"):
        tools = _tools(agents[name], agents)
        assert {"Bash", "Glob", "Grep", "Read"} <= tools
        assert not {"Edit", "Write", "Task"} & tools
    for name in ("general-purpose", "explore", "plan"):
        a = agents[name]
        assert a.one_shot
        assert any(isinstance(e, Subtasks) and not e.roster and not e.agents
                   for e in a.extensions)
    assert agents["explore"].system_prompt.startswith("You are a read-only scout")


def test_main_roster_schema() -> None:
    agents = _agents()
    view = TaskView(state=TaskState(), agents=agents)
    [task] = [t for e in agents["main"].all_extensions() for t in e.tools(view)
              if t.name == "Task"]
    prop = task.input_schema["properties"]["subagent_type"]
    assert prop["enum"] == ["explore", "general-purpose", "plan"]
    assert prop["description"] == (
        "The type of specialized agent to use for this task. Available: "
        "explore — Read-only scout: fans out (glob/grep/read + read-only shell) to investigate "
        "the workspace and report facts (never edits).; "
        "general-purpose — General-purpose worker for self-contained coding tasks: search, "
        "read, write, edit, run shell commands, then return the result.; "
        "plan — Architect: reads the code and returns a concrete ordered implementation plan "
        "(read-only — never writes any file).")


def test_extension_sets_and_sharing() -> None:
    agents = _agents(plugins={"mcp": Mcp()})
    names = {n: {e.name for e in a.all_extensions()} for n, a in agents.items()}
    common = {"compaction", "permissions", "shell", "web", "instructions", "environment"}
    assert common | {"skills", "mcp"} <= names["general-purpose"]
    assert common | {"skills"} <= names["explore"]
    assert "mcp" not in names["explore"]
    assert common | {"ask_user"} <= names["plan"]
    assert not {"mcp", "skills"} & names["plan"]
    assert "memory" not in names["main"]  # memory is off until set
    # One instance per plugin and arguments, shared by every agent naming it.
    shells = {id(e) for a in agents.values() for e in a.extensions if e.name == "shell"}
    assert len(shells) == 1


def test_main_kw_and_extra_files() -> None:
    reviewer = parse_agent_file(
        "---\nname: reviewer\ndescription: Reviews diffs.\ntools: Read, Grep\n---\nReview.\n")
    agents = {a.name: a for a in default_agents("m", extra_files=[reviewer],
                                               system_prompt="Short.\n")}
    assert agents["main"].system_prompt == "Short.\n"
    r = agents["reviewer"]
    assert r.system_prompt == "Review.\n" and r.allowed_tools == ("Read", "Grep")
    assert {e.name for e in r.extensions} >= {"fs", "shell", "subtasks"}
