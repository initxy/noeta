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

from types import SimpleNamespace

from noeta.policies.control_semantics import RUN_WORKFLOW_TOOL
from noeta.builtins.react.impl import STRUCTURED_OUTPUT_TOOL
from noeta.builtins.react.impl.orchestration import (
    MAX_STRUCTURED_OUTPUT_NUDGES,
    STRUCTURED_OUTPUT_NUDGE,
    StructuredOutputPolicy,
)
from noeta.protocols.decisions import (
    FailDecision,
    StatePatchDecision,
    ToolCall,
    ToolCallsDecision,
)
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests._sdk_session import (
    coding_replay_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)


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


def _session(ws: Path, provider: FakeLLMProvider):
    """A one-shot SDK host with workflow enabled (host ``workflow_allowed=True``
    + delegation on the main spec) that may delegate to ``explore`` — the same
    assembly the shipping backend drives via ``noeta.sdk.Client``. Returns
    ``(host, driver)``."""
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    host = make_host(
        make_registry(main, preset_spec("explore")),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        workflow_allowed=True,
        budget=coding_replay_budget(3),
    )
    return host, make_driver(host)


def _child_ids(host, parent_id: str) -> list[str]:
    return [
        str(e.payload.subtask_id)
        for e in host.event_log.read(parent_id)
        if e.type == "SubtaskSpawned"
    ]


def _answer(host, task_id: str):
    done = [e for e in host.event_log.read(task_id) if e.type == "TaskCompleted"]
    return done[-1].payload.answer if done else None


def _has_structured_output(req: LLMRequest) -> bool:
    return any(
        t.get("function", {}).get("name") == STRUCTURED_OUTPUT_TOOL
        for t in req.tools
    )


def test_agent_schema_returns_structured_object(tmp_path: Path) -> None:
    provider = FakeLLMProvider(
        responses=[
            _run_workflow('return agent("extract", agent="explore", schema={"type":"object"})\n'),
            _structured_call({"title": "Report", "count": 3}),
            _end("fin"),
        ]
    )
    host, driver = _session(_ws(tmp_path), provider)
    out = driver.start(goal="structured workflow", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    # agent(schema=...) returned the structured_output call's arguments.
    assert _answer(host, orch_id) == {"title": "Report", "count": 3}


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
    host, driver = _session(_ws(tmp_path), provider)
    out = driver.start(goal="structured workflow", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    worker_id = _child_ids(host, orch_id)[0]
    assert _answer(host, orch_id) == {"ok": True}
    # The helper completed (not failed) after the nudges.
    wtypes = [e.type for e in host.event_log.read(worker_id)]
    assert "TaskCompleted" in wtypes and "TaskFailed" not in wtypes


def test_two_nudges_then_helper_fails(tmp_path: Path) -> None:
    # Helper never calls structured_output → after MAX nudges it fails. A failed
    # helper HALTS the workflow loudly, surfacing the child's
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
    host, driver = _session(_ws(tmp_path), provider)
    out = driver.start(goal="structured workflow", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    worker_id = _child_ids(host, orch_id)[0]
    wfailed = [
        e for e in host.event_log.read(worker_id) if e.type == "TaskFailed"
    ]
    assert wfailed, "helper should have failed after exhausting nudges"
    assert "structured_output" in wfailed[-1].payload.reason
    assert str(MAX_STRUCTURED_OUTPUT_NUDGES) in wfailed[-1].payload.reason
    # The failed helper halts the workflow, with the child's reason surfaced.
    ofailed = [
        e for e in host.event_log.read(orch_id) if e.type == "TaskFailed"
    ]
    assert ofailed, "a failed helper must halt the workflow"
    assert "workflow halted" in ofailed[-1].payload.reason
    assert "structured_output" in ofailed[-1].payload.reason


def test_no_schema_helper_unchanged(tmp_path: Path) -> None:
    provider = FakeLLMProvider(
        responses=[
            _run_workflow('return agent("plain task", agent="explore")\n'),
            _end("plain text answer"),
            _end("fin"),
        ]
    )
    host, driver = _session(_ws(tmp_path), provider)
    out = driver.start(goal="structured workflow", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    # Plain agent() returns the end_turn text; no structured_output anywhere.
    assert _answer(host, orch_id) == "plain text answer"
    assert not any(_has_structured_output(r) for r in provider.received_requests)


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
    host, driver = _session(_ws(tmp_path), provider)
    out = driver.start(goal="structured workflow", agent="main")
    assert out.status == "terminal"
    # Exactly ONE request (the schema helper's) carried structured_output;
    # the parent's and the plain helper's requests did not.
    carried = [r for r in provider.received_requests if _has_structured_output(r)]
    assert len(carried) == 1


# ---------------------------------------------------------------------------
# Payload validation: a call that does not match the schema is not an answer.
#
# The provider-side tool ``parameters`` only steer the model. Without a check,
# a call missing a required field completes the helper and the
# ``agent(goal, schema=...)`` caller parses a payload that was never the shape
# it declared — a KeyError far from its cause, against a ledger that says the
# task succeeded.
# ---------------------------------------------------------------------------

_TITLED = (
    '{"type":"object","required":["title"],'
    '"properties":{"title":{"type":"string"}}}'
)


def _request_texts(provider: FakeLLMProvider) -> str:
    """Every text / tool_result body the provider was ever sent, concatenated."""
    parts: list[str] = []
    for req in provider.received_requests:
        for msg in req.messages:
            for block in msg.content:
                for attr in ("text", "output"):
                    value = getattr(block, attr, None)
                    if value is not None:
                        parts.append(value if isinstance(value, str) else str(value))
    return "\n".join(parts)


def test_invalid_payload_is_rejected_then_corrected(tmp_path: Path) -> None:
    # First call misses the required "title" → rejected with the violation, not
    # accepted as the answer; the corrected second call completes the helper.
    provider = FakeLLMProvider(
        responses=[
            _run_workflow(
                f'return agent("extract", agent="explore", schema={_TITLED})\n'
            ),
            _structured_call({"count": 3}),  # no "title"
            _structured_call({"title": "Report"}),  # corrected
            _end("fin"),
        ]
    )
    host, driver = _session(_ws(tmp_path), provider)
    out = driver.start(goal="structured workflow", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    worker_id = _child_ids(host, orch_id)[0]
    # The rejected payload never became the answer.
    assert _answer(host, orch_id) == {"title": "Report"}
    wtypes = [e.type for e in host.event_log.read(worker_id)]
    assert "TaskCompleted" in wtypes and "TaskFailed" not in wtypes
    # The model was told what was wrong, by path, so the retry is actionable.
    sent = _request_texts(provider)
    assert "structured_output rejected" in sent
    assert "$.title" in sent and "required property is missing" in sent


def test_invalid_payload_exhausts_the_budget_and_fails(tmp_path: Path) -> None:
    # Three bad payloads: reject, reject, fail — the same ceiling the
    # never-called path uses, and the reason names the violation.
    provider = FakeLLMProvider(
        responses=[
            _run_workflow(
                f'return agent("extract", agent="explore", schema={_TITLED})\n'
            ),
            _structured_call({"count": 1}),
            _structured_call({"count": 2}),
            _structured_call({"count": 3}),
            _end("fin"),
        ]
    )
    host, driver = _session(_ws(tmp_path), provider)
    out = driver.start(goal="structured workflow", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    worker_id = _child_ids(host, orch_id)[0]
    wfailed = [e for e in host.event_log.read(worker_id) if e.type == "TaskFailed"]
    assert wfailed, "helper should fail once the retry budget is spent"
    reason = wfailed[-1].payload.reason
    assert "structured_output" in reason
    assert str(MAX_STRUCTURED_OUTPUT_NUDGES) in reason
    assert "$.title" in reason


def test_nudge_and_rejection_share_one_budget(tmp_path: Path) -> None:
    # A model alternating between "no call" and "bad call" must not retry for
    # free: end_turn (nudge 1) + bad payload (retry 2) → the next bad payload
    # is terminal.
    provider = FakeLLMProvider(
        responses=[
            _run_workflow(
                f'return agent("extract", agent="explore", schema={_TITLED})\n'
            ),
            _end("prose instead of a call"),  # spends 1
            _structured_call({"count": 1}),  # spends 2
            _structured_call({"count": 2}),  # budget gone → fail
            _end("fin"),
        ]
    )
    host, driver = _session(_ws(tmp_path), provider)
    out = driver.start(goal="structured workflow", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    worker_id = _child_ids(host, orch_id)[0]
    assert [e.type for e in host.event_log.read(worker_id)].count("TaskFailed") == 1


def test_rejected_call_is_answered_by_a_tool_result(tmp_path: Path) -> None:
    # The rejection records assistant tool_use + a failed tool_result. A
    # continuation request carrying a tool_use with nothing answering it is
    # exactly what providers reject, so the pairing is the contract, not polish.
    provider = FakeLLMProvider(
        responses=[
            _run_workflow(
                f'return agent("extract", agent="explore", schema={_TITLED})\n'
            ),
            _structured_call({"count": 3}),
            _structured_call({"title": "ok"}),
            _end("fin"),
        ]
    )
    host, driver = _session(_ws(tmp_path), provider)
    driver.start(goal="structured workflow", agent="main")
    retry = [
        r
        for r in provider.received_requests
        if any(
            getattr(b, "tool_name", None) == STRUCTURED_OUTPUT_TOOL
            for m in r.messages
            for b in m.content
        )
    ]
    assert retry, "the retry request should replay the rejected call"
    for req in retry:
        used = {
            b.call_id
            for m in req.messages
            for b in m.content
            if getattr(b, "tool_name", None) == STRUCTURED_OUTPUT_TOOL
        }
        answered = {
            b.call_id
            for m in req.messages
            for b in m.content
            if type(b).__name__ == "ToolResultBlock"
        }
        assert used <= answered, "every rejected tool_use must have a tool_result"


def test_unmodelled_schema_keywords_never_reject(tmp_path: Path) -> None:
    # Conservative by construction: a `$ref` subtree is skipped rather than
    # guessed at, so a payload the checker cannot judge is still accepted —
    # while the `required` it *can* judge keeps working.
    schema = (
        '{"type":"object","required":["x"],'
        '"properties":{"x":{"$ref":"#/$defs/Whatever"}}}'
    )
    provider = FakeLLMProvider(
        responses=[
            _run_workflow(
                f'return agent("extract", agent="explore", schema={schema})\n'
            ),
            _structured_call({"x": 12345}),
            _end("fin"),
        ]
    )
    host, driver = _session(_ws(tmp_path), provider)
    out = driver.start(goal="structured workflow", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    assert _answer(host, orch_id) == {"x": 12345}


class _BadCallPolicy:
    """An inner Policy that calls ``structured_output`` with no assistant turn."""

    def decide(self, ctx, view):  # noqa: ANN001, ARG002
        return ToolCallsDecision(
            calls=[
                ToolCall(
                    tool_name=STRUCTURED_OUTPUT_TOOL, arguments={}, call_id="c1"
                )
            ],
            assistant_message=None,
        )


def test_rejection_without_an_assistant_turn_still_spends_the_budget() -> None:
    # Unreachable from ReActPolicy (which always carries its turn), but the
    # wrapper accepts any Policy. With no assistant message there is no tool_use
    # to answer, so the rejection degrades to a user nudge — and it must carry
    # the marker the counter reads, or the retry is free and the helper loops.
    policy = StructuredOutputPolicy(
        inner=_BadCallPolicy(),
        schema={"type": "object", "required": ["title"], "properties": {}},
    )
    decision = policy.decide(SimpleNamespace(), SimpleNamespace(rolling_history=[]))
    assert isinstance(decision, StatePatchDecision)
    assert decision.messages_before == ()
    nudge = decision.messages_after[0]
    assert STRUCTURED_OUTPUT_NUDGE in nudge.content[0].text
    assert "$.title" in nudge.content[0].text

    # Replaying that nudge back is what spends the budget — after MAX of them
    # the same call is terminal instead of looping.
    spent = SimpleNamespace(rolling_history=[nudge] * MAX_STRUCTURED_OUTPUT_NUDGES)
    assert isinstance(policy.decide(SimpleNamespace(), spent), FailDecision)
