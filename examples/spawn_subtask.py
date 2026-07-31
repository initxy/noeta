"""SDK example — delegate to a sub-agent.

Demonstrated SDK capability
---------------------------
Sub-agent delegation. Naming child recipes in ``Options.agents`` is the whole
opt-in: a non-empty roster makes the compiler derive the parent's
``delegation`` activation, which mounts the model-visible
``spawn_subagent(agent, goal)`` control tool. A spawn builds a child Task from
the named :class:`noeta.sdk.AgentDefinition`'s own prompt, tools and model,
runs it to terminal, folds the result back and resumes the parent, all on one
in-process stack.

The roster is flat — a deep tree is expressed by declaring every agent at the
top level, not by nesting. The child is its own Task stream, which is why this
uses :class:`Client` rather than :func:`query`: the parent's task id is what
the ``SubtaskSpawned`` envelope is read from.

The provider is scripted so the example needs no API key; a real model is
handed the same control tool and decides for itself when to delegate.

    python examples/spawn_subtask.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.sdk import (
    AgentDefinition,
    Client,
    LLMResponse,
    Options,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.sdk.testing import FakeLLMProvider

# The one import outside ``noeta.sdk``: ``spawn_subagent`` is a control tool
# whose wire name is runtime vocabulary, not recipe surface, so it has no
# ``noeta.sdk`` home. Only a script faking a model's tool call needs the name
# at all, and importing the constant beats a hardcoded string that would drift
# silently.
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL


def _spawn_call(agent: str, goal: str) -> LLMResponse:
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
    """A network-free provider: parent spawns, child finishes, parent finishes.

    Three responses, not two: the child Task consumes one of its own, so the
    parent's closing turn is the third.
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
            # ``description`` is required and cannot be blank: it is the child's
            # advertised purpose, so an unnamed capability is a compile error
            # rather than an agent nobody knows when to call.
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
