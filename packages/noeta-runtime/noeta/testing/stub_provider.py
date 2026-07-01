"""Deterministic stub LLM provider — test-support.

:class:`StubProvider` is the two-turn double relocated from
``noeta.cli._stub_provider`` (the generic ``noeta run --provider stub``
smoke double). It is **behaviourally distinct** from
:class:`noeta.agent.observe._stub_provider.CodeStubProvider`: this stub drives the
runtime to call ``echo(text="hello")`` on turn 1, whereas the coding
counterpart calls ``glob(pattern="*")`` (a read-only fs tool present in
the coding pack). Two distinct scripted turns ⇒ relocated verbatim
here rather than re-exported.

This module is **NOT** in :mod:`noeta.providers` — that package is
reserved for real provider adapters (OpenAI-compat, Anthropic).
``noeta.testing`` is test-support and gives the stub a cli-free home so
tests can import it without reaching into ``noeta.cli``.

Behaviour is fixed: the first turn asks the runtime to call ``echo``
with text ``"hello"``; the second turn ends the turn with ``"ok smoke"``.
Two turns is enough for ``noeta run --provider stub --goal "smoke"`` to
exercise the ReAct loop's tool-call + finish branches.
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
    """Deterministic two-turn LLM provider for CLI smoke tests.

    Turn 1: returns a ``tool_use`` response asking for ``echo(text="hello")``.
    Turn 2: returns an ``end_turn`` response with text ``"ok smoke"``.

    The "turn" is detected by inspecting the request's message history
    length — odd-or-empty user-only history → turn 1; longer history
    (after a tool result) → turn 2. This is sufficient for the
    ``noeta run --provider stub`` golden path; richer state tracking is
    explicitly out of scope.
    """

    def complete(self, request: LLMRequest) -> LLMResponse:
        # Keep the LLMResponse / ToolUseBlock / usage shape below in sync
        # with noeta.agent.observe._stub_provider.CodeStubProvider: the two stubs are
        # behaviourally distinct (different tool call) but cannot share code
        # across the runtime/code layering boundary, so the response shape is
        # maintained by hand in both places.
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
