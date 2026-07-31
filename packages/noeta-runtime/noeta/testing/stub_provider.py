"""Deterministic two-turn ``LLMProvider`` double for smoke runs.

Turn 1 asks the runtime to call ``echo(text="hello")``, turn 2 ends the turn
with ``"ok smoke"`` — the smallest script that drives a ReAct loop through both
its tool-call and its finish branch. ``echo`` is the one tool a minimal runtime
always wires, so the script never depends on a tool pack.
"""

from __future__ import annotations

from typing import Any

from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)


__all__ = ["StubProvider"]


_FIRST_TURN_CALL_ID = "stub-call-1"


class StubProvider:
    """Deterministic two-turn LLM provider.

    Which turn it is is inferred from the request alone — a history already
    carrying a tool result means turn 2 — so the provider stays stateless and
    replay-safe. Anything richer is out of scope.
    """

    def complete(self, request: LLMRequest) -> LLMResponse:
        if _looks_like_first_turn(request):
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id=_FIRST_TURN_CALL_ID,
                        tool_name="echo",
                        arguments={"text": "hello"},
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
        for block in _blocks(msg):
            if _is_tool_result(block):
                return False
    return True


def _blocks(msg: Any) -> list[Any]:
    content = getattr(msg, "content", None)
    if content is None:
        return []
    return list(content)


def _is_tool_result(block: Any) -> bool:
    return type(block).__name__ == "ToolResultBlock"
