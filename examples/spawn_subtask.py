"""SDK example — delegate to a sub-agent.

Demonstrated SDK capability
---------------------------
Sub-agent delegation. Declare child agents in ``Options.agents`` as
:class:`noeta.client.AgentDefinition` recipes and open the parent's
``delegation`` capability; the SDK exposes a model-visible
``spawn_subagent(agent, goal)`` control surface. When the parent model
calls it, the runtime spawns a child Task built from the named child's
*own* config (its own prompt, tools, model), runs it to terminal, folds
its result back, and resumes the parent — all on one in-process stack.

This example uses a multi-turn :class:`noeta.client.Client` (it needs the
parent's task id to find the spawned child) and a scripted offline
provider whose turns are: parent spawns ``researcher`` → child finishes →
parent finishes. It then proves a distinct child Task stream exists.

Running it
----------
Offline by default (:class:`FakeLLMProvider`, no API key). Against a real
model you would not script the spawn — the live model decides when to
delegate. Swap the provider as shown in ``minimal_agent.py``.

    python examples/spawn_subtask.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.client import AgentDefinition, Client, Options
from noeta.policies.react import SPAWN_SUBAGENT_TOOL
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider


def _spawn_call(agent: str, goal: str) -> LLMResponse:
    """A parent turn that calls the ``spawn_subagent`` control surface."""
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="spawn-1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": agent, "goal": goal},
            )
        ],
        usage=Usage(uncached=1, output=1),
    )


def _finish(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _demo_provider() -> FakeLLMProvider:
    """Scripted three-turn flow: spawn → child finishes → parent finishes.

    A real model is handed the same ``spawn_subagent`` surface and decides
    on its own when to delegate.
    """
    return FakeLLMProvider(
        responses=[
            _spawn_call("researcher", "find the answer"),
            _finish("researcher: the answer is 42"),
            _finish("Done — the researcher reported 42."),
        ]
    )


def run(*, provider=None, workspace_dir: Path, model: str = "stub-model"):
    """Drive the parent, return ``(parent_id, child_id)``."""
    main = Options(
        system_prompt="Delegate research to your sub-agent, then summarise.",
        name="main",
        agents={
            "researcher": AgentDefinition(
                description="Read-only researcher that returns a finding.",
                prompt="You are a researcher. Investigate and report back.",
            ),
        },
        permission_mode="bypassPermissions",
    )
    client = Client(
        main,
        provider=provider if provider is not None else _demo_provider(),
        workspace_dir=workspace_dir,
        model=model,
        multi_turn=False,
    )
    try:
        outcome = client.start(goal="Find the answer and tell me.")
        parent_id = outcome.task_id
        parent_events = client.events(parent_id)
        spawned = [e for e in parent_events if e.type == "SubtaskSpawned"]
        child_id = spawned[0].payload.subtask_id if spawned else None
        return parent_id, child_id
    finally:
        client.shutdown()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="noeta-spawn-") as tmp:
        parent_id, child_id = run(workspace_dir=Path(tmp))
    print(f"parent task: {parent_id}")
    print(f"spawned child task: {child_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
