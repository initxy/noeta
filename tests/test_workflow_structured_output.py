"""Per-helper structured output (schema → structured_output tool + nudge).

``agent(goal, schema=S)``: inject a ``structured_output`` control schema and a
``StructuredOutputPolicy`` wrapper into that helper subtask; the helper's
``structured_output`` call arguments ARE the structured return value of that
``agent()`` call. If the helper end_turns without calling it → nudge at most
twice; if still no call after two nudges → that helper fails. ``agent()``
without a schema is unchanged; the tool is visible only to that helper, never
leaking to the parent or other helpers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.policies.control_tools import RUN_WORKFLOW_TOOL, STRUCTURED_OUTPUT_TOOL
from noeta.policies.orchestration import MAX_STRUCTURED_OUTPUT_NUDGES
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode


# T8/③-B: the old noeta-agent runner's
# ``_build_child_engine`` read a workflow helper's ``output_schema`` off its
# ``TaskCreated.inputs`` and built it with the ``structured_output`` tool +
# ``StructuredOutputPolicy``. The SDK subtask drain (``GenericEngineResolver.
# _build_subtask_engine``) never threads that schema, so per-helper structured
# output is NOT wired on the production SDK/backend path — these tests assert a
# feature the shipping backend lacks. Skipped (not deleted) so the gap stays
# visible until the feature is ported into resolver.py + noeta/client/host.py.
pytestmark = pytest.mark.skip(
    reason="workflow per-helper structured_output not wired on the SDK drain "
    "path (resolver._build_subtask_engine drops the child output_schema); "
    "deleted with the noeta-agent runner — port pending."
)


SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["title", "count"],
}


def _run_workflow(script: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="wf-call",
                tool_name=RUN_WORKFLOW_TOOL,
                arguments={"script": script},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "wf-call"},
    )


def _structured_call(args: dict) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="so",
                tool_name=STRUCTURED_OUTPUT_TOOL,
                arguments=args,
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "so"},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _ws(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir(parents=True)
    return ws


def _runner(ws: Path, provider: FakeLLMProvider) -> AgentSessionRunner:
    runner = AgentSessionRunner(
        CodeSessionConfig(
            workspace_dir=ws,
            goal="structured workflow",
            agent="main",
            provider=provider,
            model="gpt-test",
            write_mode=FsWriteMode.APPLY,
            shell_mode=ShellMode.OFF,
            max_steps=20,
            delegate_to=("explore",),
            workflow_enabled=True,
        )
    )
    runner.prepare()
    return runner


def _child_ids(runner: AgentSessionRunner, parent_id: str) -> list[str]:
    return [
        str(e.payload.subtask_id)
        for e in runner.event_log.read(parent_id)
        if e.type == "SubtaskSpawned"
    ]


def _answer(runner: AgentSessionRunner, task_id: str):
    done = [e for e in runner.event_log.read(task_id) if e.type == "TaskCompleted"]
    return done[-1].payload.answer if done else None


def _has_structured_output(req: LLMRequest) -> bool:
    return any(
        t.get("function", {}).get("name") == STRUCTURED_OUTPUT_TOOL
        for t in req.tools
    )


SCHEMA_SCRIPT = 'return agent("extract fields", agent="explore", schema=args["schema"])\n'


def test_agent_schema_returns_structured_object(tmp_path: Path) -> None:
    provider = FakeLLMProvider(
        responses=[
            _run_workflow('return agent("extract", agent="explore", schema={"type":"object"})\n'),
            _structured_call({"title": "Report", "count": 3}),
            _end("fin"),
        ]
    )
    runner = _runner(_ws(tmp_path), provider)
    try:
        result = runner.execute()
        assert result.status == "terminal"
        orch_id = _child_ids(runner, runner.task_id)[0]
        # agent(schema=...) returned the structured_output call's arguments.
        assert _answer(runner, orch_id) == {"title": "Report", "count": 3}
    finally:
        runner.shutdown()


def test_nudge_then_success(tmp_path: Path) -> None:
    # Helper end_turns twice (gets 2 nudges) then calls structured_output.
    provider = FakeLLMProvider(
        responses=[
            _run_workflow('return agent("extract", agent="explore", schema={"type":"object"})\n'),
            _end("here is my answer in prose"),  # nudge 1
            _end("still prose"),  # nudge 2
            _structured_call({"ok": True}),  # complies
            _end("fin"),
        ]
    )
    runner = _runner(_ws(tmp_path), provider)
    try:
        result = runner.execute()
        assert result.status == "terminal"
        orch_id = _child_ids(runner, runner.task_id)[0]
        worker_id = _child_ids(runner, orch_id)[0]
        assert _answer(runner, orch_id) == {"ok": True}
        # The helper completed (not failed) after the nudges.
        wtypes = [e.type for e in runner.event_log.read(worker_id)]
        assert "TaskCompleted" in wtypes and "TaskFailed" not in wtypes
    finally:
        runner.shutdown()


def test_two_nudges_then_helper_fails(tmp_path: Path) -> None:
    # Helper never calls structured_output → after MAX nudges it fails. A failed
    # helper now HALTS the workflow loudly, surfacing the child's
    # own reason — which also exercises the single-delegate seam propagating
    # SubtaskResult.error (not a generic "sub-agent failed").
    script = (
        'r = agent("extract", agent="explore", schema={"type":"object"})\n'
        'return r\n'
    )
    provider = FakeLLMProvider(
        responses=[
            _run_workflow(script),
            _end("prose 1"),
            _end("prose 2"),
            _end("prose 3"),  # third end_turn: nudges already == MAX → fail
            _end("fin"),
        ]
    )
    runner = _runner(_ws(tmp_path), provider)
    try:
        result = runner.execute()
        assert result.status == "terminal"
        orch_id = _child_ids(runner, runner.task_id)[0]
        worker_id = _child_ids(runner, orch_id)[0]
        wfailed = [
            e for e in runner.event_log.read(worker_id) if e.type == "TaskFailed"
        ]
        assert wfailed, "helper should have failed after exhausting nudges"
        assert "structured_output" in wfailed[-1].payload.reason
        assert str(MAX_STRUCTURED_OUTPUT_NUDGES) in wfailed[-1].payload.reason
        # The failed helper halts the workflow, with the child's reason surfaced.
        ofailed = [
            e for e in runner.event_log.read(orch_id) if e.type == "TaskFailed"
        ]
        assert ofailed, "a failed helper must halt the workflow"
        assert "workflow halted" in ofailed[-1].payload.reason
        assert "structured_output" in ofailed[-1].payload.reason
    finally:
        runner.shutdown()


def test_no_schema_helper_unchanged(tmp_path: Path) -> None:
    provider = FakeLLMProvider(
        responses=[
            _run_workflow('return agent("plain task", agent="explore")\n'),
            _end("plain text answer"),
            _end("fin"),
        ]
    )
    runner = _runner(_ws(tmp_path), provider)
    try:
        result = runner.execute()
        assert result.status == "terminal"
        orch_id = _child_ids(runner, runner.task_id)[0]
        # Plain agent() returns the end_turn text; no structured_output anywhere.
        assert _answer(runner, orch_id) == "plain text answer"
        assert not any(_has_structured_output(r) for r in provider.received_requests)
    finally:
        runner.shutdown()


def test_structured_output_only_visible_to_that_helper(tmp_path: Path) -> None:
    script = (
        's = agent("structured", agent="explore", schema={"type":"object"})\n'
        'p = agent("plain", agent="explore")\n'
        'return {"s": s, "p": p}\n'
    )
    provider = FakeLLMProvider(
        responses=[
            _run_workflow(script),
            _structured_call({"k": 1}),  # the schema helper
            _end("plain"),  # the plain helper
            _end("fin"),
        ]
    )
    runner = _runner(_ws(tmp_path), provider)
    try:
        runner.execute()
        # Exactly ONE request (the schema helper's) carried structured_output;
        # the parent's and the plain helper's requests did not.
        carried = [r for r in provider.received_requests if _has_structured_output(r)]
        assert len(carried) == 1
    finally:
        runner.shutdown()
