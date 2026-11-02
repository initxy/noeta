"""The background work ``TaskStop`` (the shell plugin) stops besides its own
commands, as the plugins that own it record it: the agents a task started in
the background (the subtasks plugin's ``launched`` records, folded by agent
id) and its Workflow runs (the workflow plugin's ``run`` records, folded by
run id), read from the task's folded state; and the signal that stops a run,
which the workflow plugin acts on."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["STOP_RUN", "SUBTASKS", "WORKFLOW", "BackgroundAgent", "background_agents",
           "workflow_run"]

SUBTASKS = "subtasks"
WORKFLOW = "workflow"
#: Posted to the task that started a run as ``External(STOP_RUN, {"run_id":
#: ...})``: the workflow plugin ends that run as stopped (``killed``) and
#: cancels the agents it has running.
STOP_RUN = "workflow_stop"


@dataclass(frozen=True)
class BackgroundAgent:
    """An agent a task started: its id (what the model was given), the
    child task now running it (a resumed agent runs in a new task) and the
    description it was started with."""

    agent_id: str
    task_id: str
    description: str


def background_agents(state: Mapping[str, Any]) -> dict[str, BackgroundAgent]:
    """The agents the task with this folded ``state`` started, by agent id
    and by the id of the task running each."""
    folded = state.get(SUBTASKS)
    agents = folded.get("agents") if isinstance(folded, Mapping) else None
    out: dict[str, BackgroundAgent] = {}
    for agent_id, data in (agents or {}).items():
        if not isinstance(data, Mapping) or not isinstance(data.get("task_id"), str):
            continue
        agent = BackgroundAgent(agent_id=str(agent_id), task_id=data["task_id"],
                                description=str(data.get("description") or data.get("type")
                                                or ""))
        out.setdefault(agent.agent_id, agent)
        out.setdefault(agent.task_id, agent)
    return out


def workflow_run(state: Mapping[str, Any], run_id: str) -> Mapping[str, Any] | None:
    """The Workflow run ``run_id`` the task with this folded ``state``
    started (its ``status``: ``running``, ``completed``, ``failed`` or
    ``killed``; its ``name``), or ``None``."""
    folded = state.get(WORKFLOW)
    runs = folded.get("runs") if isinstance(folded, Mapping) else None
    run = runs.get(run_id) if isinstance(runs, Mapping) else None
    return run if isinstance(run, Mapping) else None
