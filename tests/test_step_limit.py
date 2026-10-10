"""``HostConfig.max_steps_per_turn`` winds a turn up instead of failing it.

A worker that keeps calling tools gets, at the budget, one more model call with
tools forbidden that asks for a report of where the work got to, and the turn
finishes with that report — so the host gets an answer it can act on, not a
bare failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.protocols.messages import LLMRequest, TextBlock as _Text
from noeta.sdk import (
    Client,
    HostConfig,
    LLMResponse,
    Options,
    TextBlock,
    ToolUseBlock,
    Usage,
)


class _Busy:
    """Reads forever until tools are forbidden, then reports."""

    def __init__(self, report: str = "Read a.txt twice; next: write the fix.") -> None:
        self.report = report
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.metadata.get("tool_choice") == "none":
            content = [TextBlock(text=self.report)] if self.report else []
            return LLMResponse(
                stop_reason="end_turn", content=content, usage=Usage(uncached=1, output=1)
            )
        n = len(self.requests)
        return LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(call_id=f"c{n}", tool_name="Read", arguments={"file_path": "a.txt"})
            ],
            usage=Usage(uncached=1, output=1),
        )


def _client(tmp_path: Path, provider: _Busy, **hc: object) -> Client:
    (tmp_path / "a.txt").write_text("hello\n")
    return Client(
        Options(system_prompt="work", permission_mode="bypassPermissions"),
        provider=provider,
        workspace_dir=tmp_path,
        model="stub-model",
        multi_turn=False,
        host_config=HostConfig(**hc),  # type: ignore[arg-type]
    )


def test_the_turn_finishes_with_a_report_at_the_limit(tmp_path: Path) -> None:
    provider = _Busy()
    client = _client(tmp_path, provider, max_steps_per_turn=3)
    try:
        outcome = client.start(goal="fix it")
        events = client.events(outcome.task_id)
        answer = client.task_answer(outcome.task_id)
    finally:
        client.shutdown()
    assert events[-1].type == "TaskCompleted"
    assert answer == (
        "Stopped at the 3-step limit before finishing.\n\n"
        "Read a.txt twice; next: write the fix."
    )
    # Three working steps, then the one tool-free report call.
    assert len(provider.requests) == 4
    assert sum(1 for e in events if e.type == "ToolCallStarted") == 3
    last = provider.requests[-1]
    assert last.metadata["tool_choice"] == "none"
    assert last.tools  # the history holds tool blocks, so the schemas ride
    tail = last.messages[-1]
    assert tail.role == "user"
    assert isinstance(tail.content[0], _Text)
    assert "limit of 3 steps" in tail.content[0].text


def test_a_silent_report_still_finishes(tmp_path: Path) -> None:
    provider = _Busy(report="")
    client = _client(tmp_path, provider, max_steps_per_turn=1)
    try:
        outcome = client.start(goal="fix it")
        answer = client.task_answer(outcome.task_id)
        last_type = client.events(outcome.task_id)[-1].type
    finally:
        client.shutdown()
    assert last_type == "TaskCompleted"
    assert answer == "Stopped at the 1-step limit before finishing."


def test_unset_means_no_budget(tmp_path: Path) -> None:
    class _Short(_Busy):
        def complete(self, request: LLMRequest) -> LLMResponse:
            if len(self.requests) >= 5:
                self.requests.append(request)
                return LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="done")],
                    usage=Usage(uncached=1, output=1),
                )
            return super().complete(request)

    provider = _Short()
    client = _client(tmp_path, provider)
    try:
        outcome = client.start(goal="fix it")
        answer = client.task_answer(outcome.task_id)
    finally:
        client.shutdown()
    assert answer == "done"
    assert all(r.metadata.get("tool_choice") != "none" for r in provider.requests)


@pytest.mark.parametrize("bad", [0, -1, True, 2.5])
def test_a_meaningless_budget_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="max_steps_per_turn"):
        HostConfig(max_steps_per_turn=bad)  # type: ignore[arg-type]
