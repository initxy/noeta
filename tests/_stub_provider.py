"""Code-CLI-private stub LLM provider — `noeta code --provider stub`.

The generic CLI stub (``noeta.cli._stub_provider``) calls ``echo``,
which is not in the coding agent's fs tool pack — pairing it with
``noeta code`` produces a denied-tool loop and no ``files_changed``
(I4 rev1 P1 #2). This module is the **coding-session** counterpart: a
deterministic two-turn provider that exercises a real fs tool so the
``--provider stub`` smoke path produces a meaningful summary.

Behavior:
* **Turn 1** — ``glob(pattern="*")`` (read-only, present in every fs
  pack). Produces a real ``ToolResultRecorded`` whose payload the
  coding session summary can surface as the first observable artifact.
* **Turn 2** — ``end_turn`` with text ``"ok smoke"``.

The stub is registered as the coding-CLI's ``--provider stub`` choice
by ``noeta.cli.commands.code``; the generic ``noeta.cli._stub_provider``
remains untouched so ``noeta run --provider stub`` keeps working.
"""

from __future__ import annotations

from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)


__all__ = ["CodeStubProvider"]


_FIRST_TURN_CALL_ID = "code-stub-1"


class CodeStubProvider:
    """Deterministic two-turn LLM provider for ``noeta code`` smoke runs.

    Turn 1: ``tool_use`` asking for ``glob(pattern="*")``. The default
    coding-Agent allows ``glob`` (read-only);
    the tool runs against the workspace and the resulting
    ``ToolResultRecorded`` lands in the EventLog.

    Turn 2: ``end_turn`` with text ``"ok smoke"``.

    Turn detection mirrors ``noeta.cli._stub_provider`` — if any prior
    message has a ``ToolResultBlock`` we are in turn 2+; otherwise turn 1.
    """

    def complete(self, request: LLMRequest) -> LLMResponse:
        if _looks_like_first_turn(request):
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id=_FIRST_TURN_CALL_ID,
                        tool_name="glob",
                        arguments={"pattern": "*"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
            )
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="ok smoke")],
            usage=Usage(uncached=1, output=1),
        )


def _looks_like_first_turn(request: LLMRequest) -> bool:
    for msg in request.messages:
        content = getattr(msg, "content", None) or []
        for block in content:
            if type(block).__name__ == "ToolResultBlock":
                return False
    return True
