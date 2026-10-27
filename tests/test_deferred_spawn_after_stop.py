"""A ``Task`` call deferred behind ordinary calls never outlives its turn.

A response carrying ``Task`` plus ordinary calls runs the ordinary calls first
and spawns afterwards, without a model call. When a stop lands in between, the
turn settles with the ``Task`` call answered as not run, and the human's next
goal goes to the model — the stale spawn never fires after the new user
message (which would split a tool_use from its result).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from noeta.builtins.delegation.impl import translate_spawn_subagent
from noeta.builtins.react.impl import ReActPolicy
from noeta.core.fold import fold
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL, ControlToolSpec
from noeta.protocols.decisions import SpawnSubtaskDecision
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.testing.fake_llm import FakeLLMProvider

from tests._sdk_session import (
    default_coding_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)
from tests.test_code_subagent_delegation import _OneShotLLM, _view

SPAWN_CALL_ID = "spawn-1"
READ_CALL_ID = "read-1"


def _mixed(read_path: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=SPAWN_CALL_ID,
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": "explore", "goal": "review x.py"},
            ),
            ToolUseBlock(
                call_id=READ_CALL_ID, tool_name="Read", arguments={"file_path": read_path}
            ),
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "mixed"},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def test_deferred_spawn_does_not_fire_past_a_new_user_message() -> None:
    policy = ReActPolicy(
        llm=_OneShotLLM(_mixed("x.py")),
        tools={},
        system_prompt="p",
        model="m",
        control_translate_specs=(
            ControlToolSpec(SPAWN_SUBAGENT_TOOL, translate_spawn_subagent),
        ),
    )
    ctx = StepContext(task_id="t", lease_id="l", trace_id="tr")
    first = policy.decide(ctx, _view())
    answered = [
        Message(role="user", content=[TextBlock(text="go")]),
        first.assistant_message,
        Message(
            role="tool",
            content=[ToolResultBlock(call_id=READ_CALL_ID, output="x", success=True)],
        ),
    ]
    # Only tool results after the turn: the deferred spawn runs.
    deferred = policy.decide(
        ctx, dataclasses.replace(_view(), rolling_history=answered)
    )
    assert isinstance(deferred, SpawnSubtaskDecision)
    # A new user message after it: the spawn is no longer that turn's to run.
    later = policy.decide(
        ctx,
        dataclasses.replace(
            _view(),
            rolling_history=[
                *answered,
                Message(role="user", content=[TextBlock(text="never mind")]),
            ],
        ),
    )
    assert not isinstance(later, SpawnSubtaskDecision)


def test_stop_after_the_ordinary_calls_answers_the_deferred_task(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.py").write_text("foo\n")
    requests: list[LLMRequest] = []

    def responder(request: LLMRequest) -> LLMResponse:
        requests.append(request)
        return _mixed(str(ws / "x.py")) if len(requests) == 1 else _end("ok")

    provider = FakeLLMProvider(responder=responder)
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    host = make_host(
        make_registry(main, preset_spec("explore")),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=True,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        budget=default_coding_budget(),
    )
    driver = make_driver(host)
    task_ref: list[str] = []

    def stop_after_read(env: Any) -> None:
        if env.type == "ToolCallFinished" and env.payload.call_id == READ_CALL_ID:
            driver.interrupt(task_ref[0], reason="user pressed stop")

    host.event_log.subscribe(stop_after_read)
    seeded = driver.seed_start(goal="review it", agent="main")
    task_ref.append(seeded.task_id)
    out = driver.drive_seeded(seeded)

    assert out.status == "suspended"
    task = fold(host.event_log, host.content_store, seeded.task_id)
    assert task.wake_on.handle == NEXT_GOAL_WAKE_HANDLE
    results = {
        b.call_id: b
        for m in task.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }
    assert results[READ_CALL_ID].success
    assert results[SPAWN_CALL_ID].success is False
    assert "interrupted" in (results[SPAWN_CALL_ID].error or "")

    resumed = driver.send_goal(seeded.task_id, goal="never mind, stop there")

    assert resumed.status == "suspended"
    # The next goal went to the model; no subagent was ever spawned.
    assert len(requests) == 2
    types = [e.type for e in host.event_log.read(seeded.task_id)]
    assert "SubtaskSpawned" not in types
    # Both calls are answered before the new user message.
    roles = [m.role for m in requests[1].messages]
    last_user = len(roles) - 1 - roles[::-1].index("user")
    answered_before = {
        b.call_id
        for m in requests[1].messages[:last_user]
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }
    assert {READ_CALL_ID, SPAWN_CALL_ID} <= answered_before
