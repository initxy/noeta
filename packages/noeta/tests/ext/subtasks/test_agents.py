"""The official sub-agents: 0.6 prompts and roster lines byte for byte,
read-only tool sets for explore and plan, no delegation."""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.ext.base import TaskView
from noeta.ext.subtasks import Subtasks
from noeta.ext.subtasks.agents import (
    EXPLORE_PROMPT,
    GENERAL_PURPOSE_PROMPT,
    OFFICIAL_ROSTER,
    PLAN_PROMPT,
    official_subagents,
)
from noeta.ext.subtasks.tool import task_schema
from noeta.loop.state import TaskState

PROMPTS = Path(__file__).resolve().parents[4] / "noeta-sdk" / "noeta" / "presets" / "prompts"


@pytest.mark.parametrize("name,text", [("general-purpose", GENERAL_PURPOSE_PROMPT),
                                       ("explore", EXPLORE_PROMPT), ("plan", PLAN_PROMPT)])
def test_prompts_are_0_6_byte_for_byte(name: str, text: str) -> None:
    assert text == (PROMPTS / f"{name}.md").read_text("utf-8")


def test_tool_sets_and_no_delegation() -> None:
    view = TaskView(state=TaskState())
    agents = {a.name: a for a in official_subagents("m")}
    assert list(agents) == ["general-purpose", "explore", "plan"]
    tools = {name: sorted(t.name for e in a.all_extensions() for t in e.tools(view))
             for name, a in agents.items()}
    assert tools["general-purpose"] == ["Bash", "Edit", "Glob", "Grep", "Read", "Write"]
    assert tools["explore"] == tools["plan"] == ["Bash", "Glob", "Grep", "Read"]
    for a in agents.values():
        assert a.one_shot
        assert any(isinstance(e, Subtasks) and not e.roster for e in a.extensions)
    assert agents["explore"].system_prompt == EXPLORE_PROMPT


def test_main_roster_schema() -> None:
    prop = task_schema(OFFICIAL_ROSTER)["properties"]["subagent_type"]
    assert prop["enum"] == ["explore", "general-purpose", "plan"]
    assert prop["description"] == (
        "The type of specialized agent to use for this task. Available: "
        "explore — Read-only scout: fans out (glob/grep/read + read-only shell) to investigate "
        "the workspace and report facts (never edits).; "
        "general-purpose — General-purpose worker for self-contained coding tasks: search, "
        "read, write, edit, run shell commands, then return the result.; "
        "plan — Architect: reads the code and returns a concrete ordered implementation plan "
        "(read-only — never writes any file).")


def test_default_sub_agents_carry_0_6_extension_sets() -> None:
    from noeta.ext.mcp import Mcp
    from noeta.presets import default_agents

    agents = {a.name: {e.name for e in a.all_extensions()}
              for a in default_agents("m", mcp=Mcp())}
    common = {"compaction", "permissions", "background_shell", "web", "instructions",
              "workspace"}
    assert common | {"skills", "mcp"} <= agents["general-purpose"]
    assert common | {"skills"} <= agents["explore"]
    assert "mcp" not in agents["explore"]
    assert common | {"ask_user"} <= agents["plan"]
    assert not {"mcp", "skills"} & agents["plan"]
