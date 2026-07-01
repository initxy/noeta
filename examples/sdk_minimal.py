"""SDK example — run an agent in-process using only ``noeta.sdk``.

Demonstrated SDK capability
---------------------------
The ``noeta.sdk`` public surface as the **single** import home.
Everything used here —
``query``, ``Options``, ``tool``, ``as_messages`` — comes from ``noeta.sdk``;
the example never imports ``noeta.client`` or any noeta-runtime internal. This is
the "pure SDK path": like claude-agent-sdk / LangChain, you import the SDK and
drive an agent in the same process, with no app and no HTTP.

The model is scripted (offline :class:`FakeLLMProvider`) to call one custom
``greet`` tool and then finish; the example folds the resulting envelope stream
into a human-readable message view with ``Client.messages`` (the public
projection over ``as_messages``).

Running it
----------
Offline by default (no API key). To drive a real model, pass a live provider
to ``run`` (see ``minimal_agent.py`` / ``swap_provider.py``).

    python examples/sdk_minimal.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.sdk import Client, Options, tool

# The scripted provider + the ToolContext/ToolResult tool types are runtime
# building blocks, not part of the user-facing recipe surface — a real
# deployment supplies a live provider and never scripts one. noeta.testing is
# the public home for the offline fake.
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.testing.fake_llm import FakeLLMProvider


_GREET_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}


@tool(name="greet", version="1", risk_level="low", input_schema=_GREET_SCHEMA)
def greet(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Return a greeting for ``arguments['name']``."""
    who = str(arguments.get("name", "world"))
    return ToolResult(success=True, output=f"Hello, {who}!")


def _demo_provider() -> FakeLLMProvider:
    """Scripted: call ``greet`` once, then finish."""
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
