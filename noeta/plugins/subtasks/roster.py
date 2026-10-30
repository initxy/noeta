"""The agent types a task may start (its roster): the specs a host gives,
plus the worker's agents named by agent, and the list the model reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from noeta.core import models
from noeta.core.agent import Agent
from noeta.core.extension import TaskView
from noeta.plugins.subtasks import texts
from noeta.toolkit.deltas import AMBIENT_NOTE, changed

#: ``agents=["*"]``: every agent of the worker that has a description.
ALL_AGENTS = "*"
#: Rosters resolved (one per set of named agents a worker saw) kept at most.
RESOLVED_KEEP = 64


@dataclass(frozen=True)
class SubagentSpec:
    """One agent type the model may start.

    ``name`` is what the model passes as ``subagent_type``; ``description``
    its line in the agent list. ``agent`` names the registered ``Agent`` the
    child runs (default: ``name``), whose definition decides the child's
    model, prompt and tools. ``config`` is merged into the child's
    ``TaskCreated.config``; ``queue`` routes the child (default: the
    extension's ``queue``). ``tools`` is the agent list's tools column.
    """

    name: str
    description: str
    agent: str = ""
    config: Mapping[str, Any] = field(default_factory=dict)
    queue: str | None = None
    tools: str = "*"


def check_roster(roster: Mapping[str, SubagentSpec] | Sequence[SubagentSpec]
                 ) -> dict[str, SubagentSpec]:
    """``roster`` by name; a blank description or a repeated name is an error."""
    specs = list(roster.values()) if isinstance(roster, Mapping) else list(roster)
    out: dict[str, SubagentSpec] = {}
    for spec in specs:
        if not spec.description.strip():
            raise ValueError(f"sub-agent {spec.name!r} needs a non-blank description")
        if spec.name in out:
            raise ValueError(f"duplicate sub-agent name {spec.name!r}")
        out[spec.name] = spec
    return out


class Roster:
    """The given specs and the agents named by agent (``"*"``: every agent
    with a description but the task's own), resolved per task."""

    def __init__(self, specs: dict[str, SubagentSpec], agents: Sequence[str]) -> None:
        self.specs = specs
        self.agents = tuple(agents)
        self._resolved: dict[tuple[Any, ...], dict[str, SubagentSpec]] = {}

    def for_view(self, view: TaskView) -> dict[str, SubagentSpec]:
        """The agent types: the given specs, plus each named agent the worker
        has (a name it lacks is left out)."""
        if not self.agents:
            return self.specs
        names = [n for n in view.agents if n != view.state.agent] \
            if ALL_AGENTS in self.agents else [n for n in self.agents if n in view.agents]
        named = tuple((n, view.agents[n].description, _tools_column(view.agents[n]),
                       _freeze(_definition_config(view.agents[n])))
                      for n in names if view.agents[n].description.strip())
        roster = self._resolved.pop(named, None)
        if roster is None:
            roster = check_roster([*self.specs.values(),
                                   *(SubagentSpec(name=n, description=d, tools=t, config=dict(c))
                                     for n, d, t, c in named if n not in self.specs)])
            while len(self._resolved) >= RESOLVED_KEEP:
                del self._resolved[next(iter(self._resolved))]
        self._resolved[named] = roster  # newest last
        return roster


def agent_list(roster: Mapping[str, SubagentSpec]) -> str | None:
    """The agent types a task can start, one line each with its tools."""
    if not roster:
        return None
    lines = [agent_line(s) for s in sorted(roster.values(), key=lambda s: s.name.lower())]
    return "\n".join([texts.AGENT_LIST_HEAD, *lines, "", texts.AGENT_LIST_TAIL])


def agent_line(spec: SubagentSpec) -> str:
    return f"- {spec.name}: {spec.description} (Tools: {spec.tools})"


def agent_list_delta(before: Sequence[str], roster: Mapping[str, SubagentSpec]) -> str:
    """The note on agent types that joined or left since ``before`` was
    listed (``""`` when none did)."""
    joined, left = changed(before, sorted(roster, key=str.lower))
    parts: list[str] = []
    if joined:
        parts.append("\n".join([texts.AGENT_LIST_ADDED, *(agent_line(roster[n]) for n in joined)]))
    if left:
        parts.append("\n".join([texts.AGENT_LIST_REMOVED, *(f"- {n}" for n in left)]))
        parts.append(AMBIENT_NOTE)
    return "\n".join(parts)


def _tools_column(agent: Any) -> str:
    allowed = getattr(agent, "allowed_tools", None)
    denied = list(getattr(agent, "denied_tools", ()) or ())
    if allowed is not None:
        return ", ".join(allowed)
    if denied:
        return "All tools except " + ", ".join(denied)
    return "*"


def _definition_config(agent: Agent) -> dict[str, Any]:
    """Child config an agent's definition asks for: its own model and effort
    (so the parent's are not inherited over them) and permission mode, from
    its agent file's fields (``Agent.extra``): ``model: inherit`` runs on the
    caller's model."""
    out: dict[str, Any] = {}
    if agent.extra.get("model") != "inherit":
        out["model"] = models.resolve_alias(agent.model)
    if agent.effort:
        out["effort"] = agent.effort
    if agent.extra.get("permissionMode"):
        out["permission_mode"] = str(agent.extra["permissionMode"])
    if agent.extra.get("isolation") == "worktree":
        out["isolation"] = "worktree"
    return out


def _freeze(value: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted(value.items()))
