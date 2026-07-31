"""SDK example — drive an agent through ``Client`` and read the message view.

Demonstrated SDK capability
---------------------------
:class:`noeta.sdk.Client`, the stateful counterpart to :func:`query`. It hands
back a task id and stays open, which is what makes the projections addressable:
:meth:`Client.messages` folds that task's envelopes into the human-readable
view (``as_messages``) so an application never parses the ledger itself.

An open ``Client`` owns workers and observer subscriptions, so ``shutdown``
belongs in a ``finally`` — it is idempotent, and skipping it leaks them.

The provider is scripted so the example needs no API key; pass a live provider
from ``noeta.sdk.providers`` to :func:`run` to drive a real model.

    python examples/sdk_minimal.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.sdk import (
    Client,
    LLMResponse,
    Options,
    TextBlock,
    ToolContext,
    ToolResult,
    ToolUseBlock,
    Usage,
    tool,
)

# A scripted provider is test scaffolding, not part of the recipe surface, so
# it lives in ``noeta.sdk.testing`` rather than alongside the real adapters.
from noeta.sdk.testing import FakeLLMProvider


_GREET_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}


@tool(name="greet", version="1", risk_level="low", input_schema=_GREET_SCHEMA)
def greet(arguments: dict, ctx: ToolContext) -> ToolResult:
    who = str(arguments.get("name", "world"))
    return ToolResult(success=True, output=f"Hello, {who}!")


def _demo_provider() -> FakeLLMProvider:
    """A network-free provider scripted to call ``greet`` once, then finish."""
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="g-1",
                        tool_name="greet",
                        arguments={"name": "Noeta"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="Said hello.")],
                usage=Usage(uncached=1, output=1),
            ),
        ]
    )


def run(*, provider=None, workspace_dir: Path, model: str = "stub-model"):
    """Drive one turn and return the projected message view."""
    options = Options(
        system_prompt="You greet people when asked.",
        name="greeter",
        allowed_tools=(greet,),
        permission_mode="bypassPermissions",
    )
    client = Client(
        options,
        provider=provider if provider is not None else _demo_provider(),
        workspace_dir=workspace_dir,
        model=model,
        # The default (``True``) parks the task on a next-goal suspend so a
        # conversation can continue; one goal in, one terminal out is what this
        # example wants.
        multi_turn=False,
    )
    try:
        outcome = client.start(goal="Please greet Noeta.")
        return client.messages(outcome.task_id)
    finally:
        client.shutdown()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="noeta-sdk-minimal-") as tmp:
        messages = run(workspace_dir=Path(tmp))
    print(f"projected {len(messages)} message(s) from the pure-SDK run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
