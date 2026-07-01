"""Unit tests for :class:`noeta.testing.stub_provider.StubProvider`.

The stub provider is the offline two-turn smoke double (relocated to
``noeta.testing.stub_provider`` in the library-SDK refactor). These tests pin its
deterministic two-turn behavior so the quickstart smoke test does not
silently drift.
"""

from __future__ import annotations

from noeta.testing.stub_provider import StubProvider
from noeta.protocols.messages import (
    LLMRequest,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def _build_request(messages: list[Message]) -> LLMRequest:
    return LLMRequest(
        model="stub-model",
        system=Message(role="system", content=[TextBlock(text="you are a smoke test")]),
        messages=messages,
        tools=[],
    )


def test_stub_first_turn_returns_tool_use_echo() -> None:
    provider = StubProvider()
    request = _build_request(
        messages=[Message(role="user", content=[TextBlock(text="smoke")])]
    )

    response = provider.complete(request)

    assert response.stop_reason == "tool_use"
    assert len(response.content) == 1
    block = response.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.tool_name == "echo"
    assert block.arguments == {"text": "hello"}
    assert block.call_id == "stub-call-1"


def test_stub_second_turn_returns_end_turn_after_tool_result() -> None:
    provider = StubProvider()
    request = _build_request(
        messages=[
            Message(role="user", content=[TextBlock(text="smoke")]),
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        call_id="stub-call-1",
                        tool_name="echo",
                        arguments={"text": "hello"},
                    )
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        call_id="stub-call-1",
                        output="hello",
                        success=True,
                    )
                ],
            ),
        ]
    )

    response = provider.complete(request)

    assert response.stop_reason == "end_turn"
    assert len(response.content) == 1
    block = response.content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "ok smoke"


def test_stub_is_deterministic_across_repeated_calls() -> None:
    provider = StubProvider()
    request_first = _build_request(
        messages=[Message(role="user", content=[TextBlock(text="smoke")])]
    )

    response_a = provider.complete(request_first)
    response_b = provider.complete(request_first)

    assert response_a == response_b
