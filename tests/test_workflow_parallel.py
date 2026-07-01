"""`parallel()` batch fan-out
(reuses the subtask fan-out group barrier).

``parallel([...])`` in a script lays out a batch of workers at once -> one
N-way ``SpawnSubtasksDecision`` (group all-of
barrier) -> wake once all members terminate, results returned to
the script **in spawn order**.

Proves:
* ``parallel([a,b,c])`` spawns a 3-member group on the orchestration subtask,
  results ordered by spawn;
* a single ``agent()`` and ``parallel()`` mixed in one script don't interfere;
* a partial member failure still completes the group (wait-all-terminate); a
  failed member **halts the whole workflow loudly**, and a script may ``try/except``
  to tolerate it.

v1 drains sequentially (not wall-clock parallel -- true concurrency is a
follow-on, see ADR D7/D8).
"""

from __future__ import annotations

from pathlib import Path

from noeta.policies.control_tools import RUN_WORKFLOW_TOOL
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
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


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _error() -> LLMResponse:
    return LLMResponse(
        stop_reason="error",
        content=[],
        usage=Usage(uncached=1, output=1),
        raw={"id": "err", "category": "fatal"},
    )


def _ws(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir(parents=True)
    return ws


def _session(ws: Path, responses: list[LLMResponse]):
    """A one-shot SDK host with workflow enabled (host ``workflow_allowed=True``
    + delegation on the main spec) that may delegate to ``explore``. The
    reserved ``__workflow__`` orchestration child is built by the host itself.
    Returns ``(host, driver)``."""
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    host = make_host(
        make_registry(main, preset_spec("explore")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=responses),
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
    done = [
        e for e in host.event_log.read(task_id) if e.type == "TaskCompleted"
    ]
    return done[-1].payload.answer if done else None


PARALLEL_SCRIPT = (
    'rs = parallel(["scan a", "scan b", "scan c"], agent="explore")\n'
    'return "|".join(rs)\n'
)


def test_parallel_three_members_ordered(tmp_path: Path) -> None:
    host, driver = _session(
        _ws(tmp_path),
        [_run_workflow(PARALLEL_SCRIPT), _end("ra"), _end("rb"), _end("rc"), _end("fin")],
    )
    out = driver.start(goal="parallel scan", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    # The parallel() emitted ONE 3-member group (3 SubtaskSpawned).
    worker_ids = _child_ids(host, orch_id)
    assert len(worker_ids) == 3
    # Suspended on a group join, not three single joins.
    orch_types = [e.type for e in host.event_log.read(orch_id)]
    assert orch_types.count("TaskSuspended") == 1
    susp = next(
        e for e in host.event_log.read(orch_id) if e.type == "TaskSuspended"
    )
    assert susp.payload.reason == "waiting_subtask_group"
    # Results come back in SPAWN order (a,b,c) — decoupled from drain order.
    assert _answer(host, orch_id) == "ra|rb|rc"


def test_single_and_parallel_mixed(tmp_path: Path) -> None:
    script = (
        'first = agent("classify", agent="explore")\n'
        'rs = parallel(["a", "b"], agent="explore")\n'
        'last = agent("summarize", agent="explore")\n'
        'return first + ":" + "|".join(rs) + ":" + last\n'
    )
    # call order: main wf; worker(first); group workers a,b; worker(last); main end
    host, driver = _session(
        _ws(tmp_path),
        [
            _run_workflow(script),
            _end("F"),
            _end("A"),
            _end("B"),
            _end("L"),
            _end("fin"),
        ],
    )
    out = driver.start(goal="parallel scan", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    # 1 single + 2 group = 4 worker spawns total.
    assert len(_child_ids(host, orch_id)) == 4
    assert _answer(host, orch_id) == "F:A|B:L"


def test_parallel_member_failure_halts_workflow(tmp_path: Path) -> None:
    # Member b fails (stop_reason=error). The group still waits-all-terminate
    # (all three spawn), but a failed member now HALTS the workflow loudly
    # instead of flowing an empty value back into the script.
    script = (
        'rs = parallel(["a", "b", "c"], agent="explore")\n'
        'return "|".join(rs)\n'
    )
    host, driver = _session(
        _ws(tmp_path),
        [_run_workflow(script), _end("ra"), _error(), _end("rc"), _end("fin")],
    )
    out = driver.start(goal="parallel scan", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    worker_ids = _child_ids(host, orch_id)
    assert len(worker_ids) == 3  # wait-all-terminate: all members still spawned
    failed = [e for e in host.event_log.read(orch_id) if e.type == "TaskFailed"]
    assert failed, "a failed member must halt the workflow, not yield ''"
    assert "workflow halted" in failed[-1].payload.reason


def test_parallel_member_failure_can_be_tolerated(tmp_path: Path) -> None:
    # A script that WANTS to tolerate a failed member wraps parallel() in
    # try/except — agent()/parallel() raise an ordinary Exception on failure, so
    # the workflow recovers. The spawn-suspend is a BaseException and is never
    # swallowed, so all members still get spawned on the first pass.
    script = (
        "try:\n"
        '    rs = parallel(["a", "b", "c"], agent="explore")\n'
        '    out = "|".join(rs)\n'
        "except Exception:\n"
        '    out = "TOLERATED"\n'
        "return out\n"
    )
    host, driver = _session(
        _ws(tmp_path),
        [_run_workflow(script), _end("ra"), _error(), _end("rc"), _end("fin")],
    )
    out = driver.start(goal="parallel scan", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    assert len(_child_ids(host, orch_id)) == 3
    assert _answer(host, orch_id) == "TOLERATED"


def test_large_worker_answer_spills_to_contentstore_and_flows_back(
    tmp_path: Path,
) -> None:
    # A worker answer bigger than EVENT_PAYLOAD_MAX_BYTES must NOT crash the
    # drain (spill to ContentStore) — and the FULL text must still
    # reach the script. Regression for the PayloadTooLarge hang.
    from noeta.protocols.events import answer_from_payload

    big = "X" * 9000  # > 4 KB event payload cap
    host, driver = _session(
        _ws(tmp_path),
        [_run_workflow('return agent("scan", agent="explore")\n'), _end(big), _end("fin")],
    )
    out = driver.start(goal="parallel scan", agent="main")
    assert out.status == "terminal"
    orch_id = _child_ids(host, out.task_id)[0]
    worker_id = _child_ids(host, orch_id)[0]

    # Worker's TaskCompleted spilled the answer to a ref (nothing inline).
    wc = [e for e in host.event_log.read(worker_id) if e.type == "TaskCompleted"]
    assert wc, "worker must complete (not crash on PayloadTooLarge)"
    assert wc[-1].payload.answer is None
    assert wc[-1].payload.answer_ref is not None

    # The workflow received the FULL answer back (deref'd), not truncated.
    oc = [e for e in host.event_log.read(orch_id) if e.type == "TaskCompleted"]
    assert oc and answer_from_payload(oc[-1].payload, host.content_store) == big
