"""The built-in agents: Claude Code's Explore, Plan and general-purpose with
their tool sets, main's agent types from every described agent."""

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


def _tools(agent: Agent, agents: dict[str, Agent], *, child: bool = True) -> list[str]:
    state = TaskState(agent=agent.name, parent_id="p" if child else None,
                      config={"subagent_type": agent.name} if child else {})
    view = TaskView(state=state, agents=agents)
    names = [t.name for e in agent.all_extensions() for t in e.tools(view)]
    return [n for n in names if (agent.allowed_tools is None or n in agent.allowed_tools)
            and n not in agent.denied_tools]


def test_built_in_agent_files() -> None:
    names = [f.name for f in builtin_agent_files()]
    assert names == ["main", "Explore", "general-purpose", "Plan"]


def test_tool_sets() -> None:
    agents = _agents()
    gp = _tools(agents["general-purpose"], agents)
    assert {"Agent", "Bash", "Edit", "Read", "Write", "SendMessage"} <= set(gp)
    assert gp[-1] == "SubagentHandback" and "ListAgents" not in gp
    for name in ("Explore", "Plan"):
        tools = _tools(agents[name], agents)
        assert {"Bash", "Read", "SendMessage"} <= set(tools)
        assert not {"Edit", "Write", "Agent", "AskUserQuestion", "ListAgents"} & set(tools)
        assert tools[-1] == "SubagentHandback"
        assert agents[name].denied_tools == ("Agent", "ExitPlanMode", "Edit", "Write",
                                             "NotebookEdit")
    main = _tools(agents["main"], agents, child=False)
    assert {"Agent", "ListAgents", "SendMessage"} <= set(main)
    assert "SubagentHandback" not in main


def test_main_agent_types_come_from_described_agents() -> None:
    agents = _agents()
    [sub] = [e for e in agents["main"].extensions if isinstance(e, Subtasks)]
    roster = sub.roster_for(TaskView(state=TaskState(agent="main"), agents=agents))
    assert sorted(roster) == ["Explore", "Plan", "general-purpose"]
    assert roster["Explore"].tools == ("All tools except Agent, ExitPlanMode, Edit, Write, "
                                       "NotebookEdit")
    assert roster["general-purpose"].tools == "*"
    assert roster["Explore"].description.startswith("Read-only search agent")


def test_extension_sets_and_sharing() -> None:
    agents = _agents(plugins={"mcp": Mcp()})
    names = {n: {e.name for e in a.all_extensions()} for n, a in agents.items()}
    common = {"compaction", "permissions", "shell", "web", "environment", "skills", "mcp"}
    assert common | {"instructions"} <= names["general-purpose"]
    for name in ("Explore", "Plan"):
        assert common <= names[name]
        assert "instructions" not in names[name]  # Claude Code sends them no instruction files
    assert "memory" not in names["main"]  # memory is off until set
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
    [sub] = [e for e in agents["main"].extensions if isinstance(e, Subtasks)]
    roster = sub.roster_for(TaskView(state=TaskState(agent="main"), agents=agents))
    assert roster["reviewer"].tools == "Read, Grep"
