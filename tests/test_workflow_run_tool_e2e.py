"""`run_workflow` end to end (full
chain: control tool -> translation -> orchestration subtask -> real subtask ->
result fold-back -> EventLog), single `agent()`.

The main agent calls ``run_workflow(script=...)``; a single ``agent("...")`` in
the script spawns exactly one real subtask (its own EventLog); the subtask
runs, its result is folded back, the orchestration script ends -> workflow
completes and the final result folds back to the main agent.

LLM call order (FakeLLMProvider is shared globally, consumed in complete() call
order):
1. main agent -> ``run_workflow`` tool_use;
2. (orchestration Policy issues no LLM call) worker(explore) -> end_turn with the result;
3. (orchestration Policy issues no LLM call) main agent -> end_turn to finish.
"""

from __future__ import annotations

from pathlib import Path

from noeta.core.fold import fold
from noeta.policies.control_tools import RUN_WORKFLOW_TOOL, WORKFLOW_AGENT_NAME
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    coding_replay_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)


WORKFLOW_CALL_ID = "wf-call"
WORKER_ANSWER = "explored: 3 issues found"
FINAL_ANSWER = "workflow done"
SCRIPT = 'return agent("scan the repo", agent="explore")\n'


def _run_workflow_call() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=WORKFLOW_CALL_ID,
                tool_name=RUN_WORKFLOW_TOOL,
                arguments={"script": SCRIPT},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": WORKFLOW_CALL_ID},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    return ws


def _session(ws: Path):
    """A one-shot SDK host with workflow enabled (host ``workflow_allowed=True``
    + delegation on the main spec) that may delegate to ``explore``. The
    reserved ``__workflow__`` orchestration child is built by the host itself.
    Returns ``(host, driver)``."""
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    host = make_host(
        make_registry(main, preset_spec("explore")),
        workspace_dir=ws,
        provider=FakeLLMProvider(
            responses=[
                _run_workflow_call(),
                _end(WORKER_ANSWER),
                _end(FINAL_ANSWER),
            ]
        ),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        workflow_allowed=True,
        budget=coding_replay_budget(3),
    )
    return host, make_driver(host)


def _events_of(host, task_id: str) -> list[str]:
    return [e.type for e in host.event_log.read(task_id)]


def _child_ids(host, parent_id: str) -> list[str]:
    return [
        str(e.payload.subtask_id)
        for e in host.event_log.read(parent_id)
        if e.type == "SubtaskSpawned"
    ]


def test_run_workflow_single_agent_end_to_end(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver = _session(ws)
    out = driver.start(goal="orchestrate a scan", agent="main")
    assert out.status == "terminal"
    main_id = out.task_id

    # Main spawned exactly the orchestration subtask (agent=__workflow__).
    orch_ids = _child_ids(host, main_id)
    assert len(orch_ids) == 1
    orch_id = orch_ids[0]
    orch_created = next(
        e for e in host.event_log.read(orch_id) if e.type == "TaskCreated"
    )
    assert orch_created.payload.agent_name == WORKFLOW_AGENT_NAME
    # The script + args rode inputs (durable, replay-safe).
    assert orch_created.payload.inputs.get("script") == SCRIPT

    # Orchestration subtask spawned exactly one real worker (the agent()).
    worker_ids = _child_ids(host, orch_id)
    assert len(worker_ids) == 1
    worker_id = worker_ids[0]
    worker_created = next(
        e for e in host.event_log.read(worker_id) if e.type == "TaskCreated"
    )
    assert worker_created.payload.agent_name == "explore"

    # Each stream is independent and complete.
    assert "SubtaskSpawned" in _events_of(host, main_id)
    assert "SubtaskSpawned" in _events_of(host, orch_id)
    assert "SubtaskSpawned" not in _events_of(host, worker_id)
    for tid in (main_id, orch_id, worker_id):
        assert "TaskCompleted" in _events_of(host, tid), tid

    # The workflow's answer is the worker's output, folded back to the
    # main agent as the run_workflow tool_result.
    orch_done = [
        e for e in host.event_log.read(orch_id) if e.type == "TaskCompleted"
    ][-1]
    assert orch_done.payload.answer == WORKER_ANSWER
    main_task = fold(host.event_log, host.content_store, main_id)
    paired = [
        b
        for m in main_task.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.call_id == WORKFLOW_CALL_ID
    ]
    assert paired and paired[0].output == WORKER_ANSWER
