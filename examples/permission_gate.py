"""SDK example — gate tool calls with a permission callback.

Demonstrated SDK capability
---------------------------
``Options.permission_mode`` + ``Options.can_use_tool``. Under the ``default``
permission mode, high-risk built-in tools (``write`` / ``edit`` /
``apply_patch`` / ``shell_run``) are *gated*: before one runs, the SDK calls
your ``can_use_tool(tool_name, arguments) -> bool`` callback. Return ``True`` to
let it through, ``False`` to deny it. A denied call never runs, but the agent
loop continues — the model sees the denial and can react.

This is the programmatic, in-process equivalent of a human clicking
"approve / deny": the callback is your policy hook for auto-approving safe
calls and blocking dangerous ones without a UI.

Here the model is scripted to attempt a ``write``; the callback denies it. The
example proves the denial by showing the ``ToolCallApprovalResolved`` envelope
(``approved=False``, ``resolver="can_use_tool"``) and the absence of any
``ToolResultRecorded`` — the file was never written.

Running it
----------
Offline by default (:class:`FakeLLMProvider`, no API key). To drive a real
model, swap ``_demo_provider()`` for ``OpenAICompatProvider`` /
``AnthropicProvider`` (see ``minimal_agent.py``).

    python examples/permission_gate.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.protocols.events import ToolCallApprovalResolvedPayload
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.sdk import Options, query
from noeta.testing.fake_llm import FakeLLMProvider


def _demo_provider() -> FakeLLMProvider:
    """Scripted: attempt a high-risk ``write``, then finish after the deny."""
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="w1",
                        tool_name="write",
                        arguments={"path": "secret.txt", "content": "oops\n"},
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
    """Permission policy: block every ``write``, allow everything else."""
    return tool_name != "write"


def run(*, provider=None, workspace_dir: Path, model: str = "stub-model"):
    """Drive one turn against a denying callback.

    Returns ``(approved, resolver, wrote_file)`` where ``approved`` is the
    callback's decision recorded on the ``ToolCallApprovalResolved`` envelope
    and ``wrote_file`` is whether the gated tool actually ran.
    """
    options = Options(
        system_prompt="You may write files when asked.",
        name="gated",
        allowed_tools=("write",),
        # `default` gates high-risk tools; the callback decides each one.
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
