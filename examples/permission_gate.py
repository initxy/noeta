"""SDK example — gate tool calls with a permission callback.

Demonstrated SDK capability
---------------------------
``Options.permission_mode`` + ``Options.can_use_tool``. The mode picks the
gated set — under ``default`` that is every tool whose declared ``risk_level``
is not ``"low"`` — and the callback decides each gated call. Denial stops the
call, not the turn: the model sees the refusal and can react, so a policy can
be strict without stranding the agent.

The callback is the in-process stand-in for a human clicking approve/deny.
Leaving it unset is the other half of the same capability: the task suspends on
the gated call and waits for a host to resolve it, and either route records the
same ``ToolCallApprovalResolved`` envelope so the audit trail does not care
which one answered.

The provider is scripted so the example needs no API key; pass a live provider
from ``noeta.sdk.providers`` to :func:`run` to drive a real model.

    python examples/permission_gate.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.sdk import LLMResponse, TextBlock, ToolUseBlock, Usage

# The one import outside ``noeta.sdk``: event payload types are the durable
# ledger's vocabulary, not the recipe surface, so they have no ``noeta.sdk``
# home. Reading the ledger is the point here; an application that only wants
# the conversation reads ``QueryResult.messages()`` and never sees a payload.
from noeta.protocols.events import ToolCallApprovalResolvedPayload
from noeta.sdk import Options, query
from noeta.sdk.testing import FakeLLMProvider


def _demo_provider() -> FakeLLMProvider:
    """A network-free provider scripted to attempt a ``write``, then give up."""
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="w1",
                        tool_name="Write",
                        arguments={"file_path": "secret.txt", "content": "oops\n"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="Understood — I won't write that file.")],
                usage=Usage(uncached=1, output=1),
            ),
        ]
    )


def _deny_writes(tool_name: str, arguments: dict) -> bool:
    return tool_name != "Write"


def run(*, provider=None, workspace_dir: Path, model: str = "stub-model"):
    """Drive one turn against a denying callback.

    Returns ``(approved, resolver, wrote_file)``; ``wrote_file`` is the load-
    bearing one — a denial that still let the tool run would be silent
    otherwise.
    """
    options = Options(
        system_prompt="You may write files when asked.",
        name="gated",
        allowed_tools=("Write",),
        # ``bypassPermissions`` would gate nothing and the callback would never
        # be consulted.
        permission_mode="default",
        can_use_tool=_deny_writes,
    )
    envelopes = query(
        options,
        goal="Write 'oops' to secret.txt.",
        provider=provider if provider is not None else _demo_provider(),
        workspace_dir=workspace_dir,
        model=model,
    )
    resolved = [
        e.payload
        for e in envelopes
        if e.type == "ToolCallApprovalResolved"
        and isinstance(e.payload, ToolCallApprovalResolvedPayload)
    ]
    wrote_file = any(e.type == "ToolResultRecorded" for e in envelopes)
    approved = resolved[0].approved if resolved else None
    resolver = resolved[0].resolver if resolved else None
    return approved, resolver, wrote_file


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="noeta-permission-") as tmp:
        approved, resolver, wrote_file = run(workspace_dir=Path(tmp))
    print(
        f"write approved={approved} (resolver={resolver}); "
        f"file written={wrote_file}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
