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
        # Claude Code's omitClaudeMd: only the machine's managed files.
        [instructions] = [e for e in agents[name].all_extensions() if e.name == "instructions"]
        assert getattr(instructions, "managed_only", False)
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


def test_explore_runs_on_opus_under_a_fable_caller() -> None:
    """Claude Code's Explore ``inheritCap: "opus"``: a caller on a family
    above Opus (Fable) gets its Explore on Opus; only the built-in file."""
    from noeta.plugins.subtasks.roster import MODEL_CAP_KEY, capped_model

    agents = _agents()
    assert agents["Explore"].extra["inheritCap"] == "opus"
    assert "inheritCap" not in agents["Plan"].extra
    assert capped_model("claude-fable-5-1", "opus") == "claude-opus-5-5"
    assert capped_model("fable", "opus") == "claude-opus-5-5"
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "anthropic.claude-fable-5-1",
                  "gpt-5", ""):
        assert capped_model(model, "opus") is None
    sub = Subtasks()
    state = TaskState(agent="main", config={"model": "claude-fable-5-1"})
    ctx: Any = None
    capped = sub.child_config(state, ctx, "Explore", {MODEL_CAP_KEY: "opus"}, "Explore", {},
                              fork=False)
    assert capped["model"] == "claude-opus-5-5" and MODEL_CAP_KEY not in capped
    asked = sub.child_config(state, ctx, "Explore", {MODEL_CAP_KEY: "opus"}, "Explore",
                             {"model": "haiku"}, fork=False)
    assert asked["model"] == "claude-haiku-5-5"
    plain = sub.child_config(TaskState(agent="main"), ctx, "Explore", {MODEL_CAP_KEY: "opus"},
                             "Explore", {}, fork=False, parent_model="claude-fable-5-1")
    assert plain["model"] == "claude-opus-5-5"
    user_file = parse_agent_file("---\nname: Explore\ndescription: mine\n---\nlook")
    explores = [a for a in default_agents("m", extra_files=[user_file]) if a.name == "Explore"]
    assert explores and all(a.description != "mine" or "inheritCap" not in a.extra
                            for a in explores)
    assert any(a.description == "mine" for a in explores)


def test_the_plan_agents_prompt_looks_for_similar_features_and_ends_read_only() -> None:
    """Claude Code's Plan prompt: the exploring step looks for similar
    features to use as a reference, and a closing reminder after the report
    format repeats that it only explores and plans."""
    prompt = str(_agents()["Plan"].system_prompt)
    explore = next(line for line in prompt.splitlines() if line.startswith("2. Explore"))
    assert "similar features" in explore
    critical = prompt.index("3-5 files most critical")
    closing = prompt.index("Remember: you only explore and plan.")
    assert critical < closing
    assert "must not write, edit or change any file" in prompt[closing:]


def test_a_child_on_the_parents_chosen_model_takes_its_route() -> None:
    from noeta.core.loop.state import MODEL_ROUTE
    from noeta.plugins.subtasks import Subtasks

    route = {"model": "gw-big", "provider": "gw", "context_window": 300_000}
    state = TaskState(agent="main", config={"model": "gw-big", MODEL_ROUTE: route})
    ctx: Any = None
    sub = Subtasks()
    same = sub.child_config(state, ctx, "general-purpose", {}, "general-purpose", {},
                            fork=False, parent_model="gw-big")
    assert same[MODEL_ROUTE] == route
    other = sub.child_config(state, ctx, "general-purpose", {}, "general-purpose",
                             {"model": "haiku"}, fork=False)
    assert MODEL_ROUTE not in other
