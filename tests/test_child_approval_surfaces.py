"""A foreground sub-agent waiting on a tool approval is visible to the host.

The delegation drain parks the tree when a child suspends on an approval; the
root rests suspended on its sub-agent barrier. Before, the root's outcome said
nothing (``wake_handle=None``), ``can_use_tool`` was never consulted for the
child's request, and ``query()`` failed with "no terminal event". Now the root
outcome names the child's approval handle, ``can_use_tool`` resolves it, a
host can approve through the root, and ``query()`` says what is pending.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.sdk import (
    AgentDefinition,
    Client,
    LLMResponse,
    Options,
    TextBlock,
    ToolUseBlock,
    Usage,
    query,
)
from noeta.sdk.testing import FakeLLMProvider


CHILD_GOAL = "CHILDGOAL write the file"
PARENT_GOAL = "PARENTGOAL delegate it"


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _tool(name: str, args: dict[str, Any], call_id: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _answered(req: Any, call_id: str) -> bool:
    return any(
        getattr(block, "call_id", None) == call_id
        for message in req.messages
        if message.role == "tool"
        for block in message.content
    )


def _provider(ws: Path) -> FakeLLMProvider:
    def respond(req: Any) -> LLMResponse:
        text = "\n".join(
            getattr(block, "text", "") or ""
            for message in req.messages
            for block in message.content
        )
        if CHILD_GOAL in text and PARENT_GOAL not in text:
            if _answered(req, "cw1"):
                return _end("child done")
            return _tool("Write", {"file_path": str(ws / "c.txt"), "content": "x"}, "cw1")
        if _answered(req, "sp1"):
            return _end("parent done")
        return _tool("Task", {"agent": "worker", "goal": CHILD_GOAL}, "sp1")

    return FakeLLMProvider(responder=respond)


def _options(**kw: Any) -> Options:
    return Options(
        system_prompt="x",
        agents={"worker": AgentDefinition(description="d", prompt="p")},
        **kw,
    )


def _child_of(client: Client, root_id: str) -> str:
    return next(s.task_id for s in client.task_streams() if s.task_id != root_id)


def test_root_outcome_names_the_childs_approval_handle(tmp_path: Path) -> None:
    with Client(
        _options(), provider=_provider(tmp_path), workspace_dir=tmp_path, model="sonnet"
    ) as client:
        out = client.start(goal=PARENT_GOAL)
        child_id = _child_of(client, out.task_id)
        assert out.status == "suspended"
        assert out.wake_handle == "approval-cw1"
        # The child's own status carries the same handle.
        assert client.task_status(child_id).wake_handle == "approval-cw1"


def test_approve_through_the_root_resumes_the_tree(tmp_path: Path) -> None:
    with Client(
        _options(), provider=_provider(tmp_path), workspace_dir=tmp_path, model="sonnet"
    ) as client:
        out = client.start(goal=PARENT_GOAL)
        child_id = _child_of(client, out.task_id)
        resumed = client.approve(out.task_id, call_id="cw1")
        assert resumed.task_id == out.task_id
        assert resumed.status == "suspended"
        assert resumed.wake_handle == "noeta-code-next-goal"
        assert client.task_status(child_id).status == "terminal"
        assert client.task_answer(out.task_id) == "parent done"


def test_can_use_tool_resolves_a_childs_approval(tmp_path: Path) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def can_use_tool(name: str, args: dict[str, Any]) -> bool:
        seen.append((name, args))
        return True

    with Client(
        _options(can_use_tool=can_use_tool),
        provider=_provider(tmp_path),
        workspace_dir=tmp_path,
        model="sonnet",
    ) as client:
        out = client.start(goal=PARENT_GOAL)
        assert [name for name, _ in seen] == ["Write"]
        assert out.wake_handle == "noeta-code-next-goal"
        assert client.task_answer(out.task_id) == "parent done"


def test_query_resolves_a_childs_approval_via_can_use_tool(tmp_path: Path) -> None:
    result = query(
        _options(can_use_tool=lambda name, args: True),
        PARENT_GOAL,
        provider=_provider(tmp_path),
        workspace_dir=tmp_path,
        model="sonnet",
    )
    assert result.answer() == "parent done"


def test_query_names_the_pending_child_approval(tmp_path: Path) -> None:
    from noeta.sdk import QueryFailedError

    result = query(
        _options(),
        PARENT_GOAL,
        provider=_provider(tmp_path),
        workspace_dir=tmp_path,
        model="sonnet",
    )
    with pytest.raises(QueryFailedError) as info:
        result.answer()
    assert "approval-cw1" in info.value.reason
    assert "sub-agent" in info.value.reason
    assert "no terminal event" not in info.value.reason
