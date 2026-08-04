"""Real-LLM subagent orchestration, end to end (live marker).

Three product-path loops through ``SdkHost`` + ``InteractionDriver`` + presets
main — the real shipping path — proving a real model can drive multi-agent
orchestration, not just a single delegation (which
``test_live_anthropic_e2e.py`` already covers):

1. **parallel fan-out** — the model issues more than one ``Task`` call and both
   children run and fold their results back onto the parent. Turn-shape is left
   to the model (it may batch into one turn or serialize into two); the surviving
   invariant asserted here is "two children spawned and two results folded back",
   never a single group barrier a real model cannot be forced into.
2. **workflow (single agent)** — the model calls ``run_workflow`` with a
   deterministic script whose one ``agent()`` spawns a real ``explore`` worker on
   its own event stream, and the worker's answer folds back as the ``run_workflow``
   tool_result.
3. **workflow (parallel)** — a script whose ``parallel([...])`` lays out a batch
   of workers; the orchestration stream spawns >= 2 of them and the combined
   answer folds back. This is the deterministic parallel evidence — the fan-out
   is script-driven, so it does not depend on the model batching Task calls into
   one turn.

Config comes from a git-ignored ``.env`` via ``tests._live_env`` (copy
``.env.example``). Missing base/key/model auto-skips; CI never runs these.
Assertions watch **structural** invariants (spawn counts, agent names, folded
tool_result present), never verbatim non-deterministic content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.core.fold import fold
from noeta.policies.control_semantics import RUN_WORKFLOW_TOOL, WORKFLOW_AGENT_NAME
from noeta.protocols.messages import ToolResultBlock, ToolUseBlock
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests import _live_env
from tests._sdk_session import (
    coding_replay_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)

pytestmark = pytest.mark.live

requires_live = _live_env.requires_live


def _model() -> str:
    return _live_env.live_model() or ""


def _seed_ws(tmp_path: Path) -> Path:
    """A workspace with two code-word files for the workers to read."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "alpha.txt").write_text("The alpha code is ALPHA.\n", encoding="utf-8")
    (ws / "beta.txt").write_text("The beta code is BETA.\n", encoding="utf-8")
    return ws


def _delegating_host(ws: Path, *, workflow_allowed: bool = False):
    """A one-shot host whose ``main`` may delegate to / orchestrate ``explore``.

    ``delegation=True`` + ``spawnable=("explore",)`` puts ``Task`` in main's tool
    set; ``workflow_allowed`` opts the host into ``run_workflow`` (the reserved
    ``__workflow__`` orchestration child is built by the host itself).
    """
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    children = [preset_spec(n) for n in ("explore", "general-purpose", "plan")]
    host = make_host(
        make_registry(main, *children),
        workspace_dir=ws,
        provider=_live_env.build_anthropic_provider(),
        model=_model(),
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        workflow_allowed=workflow_allowed,
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


def _run_workflow_result(main_task) -> list[ToolResultBlock]:
    """The tool_result paired to the model's own ``run_workflow`` call.

    The call_id is chosen by the model, so match it structurally: find the
    ``run_workflow`` ``ToolUseBlock`` in the assistant turns, then the
    ``ToolResultBlock`` sharing its call_id. (The ``wf-`` prefix belongs to the
    script's internal ``agent()`` calls, not this outer call.)
    """
    call_ids = {
        b.call_id
        for m in main_task.runtime.messages
        if m.role == "assistant"
        for b in m.content
        if isinstance(b, ToolUseBlock) and b.tool_name == RUN_WORKFLOW_TOOL
    }
    return [
        b
        for m in main_task.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.call_id in call_ids
    ]


# ---------------------------------------------------------------------------
# Loop 1 — parallel subagent fan-out (two Task calls, two folded results)
# ---------------------------------------------------------------------------


@requires_live
def test_live_parallel_subagent_fanout(tmp_path: Path) -> None:
    ws = _seed_ws(tmp_path)
    host, driver = _delegating_host(ws)
    out = driver.start(
        goal=(
            "In a single turn, issue TWO Task tool calls at once. The first asks "
            "the 'explore' subagent to read alpha.txt in the workspace and report "
            "its code word. The second asks the 'explore' subagent to read "
            "beta.txt and report its code word. Do not wait between them. After "
            "both subagents report back, reply with both code words."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    parent = fold(host.event_log, host.content_store, out.task_id)
    # Two children ran and folded their results back — the invariant that
    # survives whether the model batched into one turn or serialized into two.
    assert len(parent.governance.subtask_results) >= 2, (
        "expected >= 2 folded subtask results, got "
        f"{len(parent.governance.subtask_results)}"
    )
    spawned = _events_of(host, out.task_id).count("SubtaskSpawned")
    assert spawned >= 2, f"expected >= 2 SubtaskSpawned, got {spawned}"
    # Every spawned child is a real explore worker on its own completed stream.
    for cid in _child_ids(host, out.task_id):
        assert "TaskCompleted" in _events_of(host, cid), cid


# ---------------------------------------------------------------------------
# Loop 2 — workflow: run_workflow drives a single-agent orchestration script
# ---------------------------------------------------------------------------


@requires_live
def test_live_workflow_single_agent(tmp_path: Path) -> None:
    ws = _seed_ws(tmp_path)
    host, driver = _delegating_host(ws, workflow_allowed=True)
    out = driver.start(
        goal=(
            "Use the run_workflow tool. Pass this exact script as the `script` "
            "argument and nothing else:\n\n"
            'return agent("read alpha.txt and report only its code word", '
            'agent="explore")\n\n'
            "The script must be deterministic: no imports, no time or random, no "
            "direct file or network access. After the workflow returns, reply "
            "with the code word it produced."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    main_id = out.task_id

    # Main spawned exactly the reserved orchestration subtask.
    orch_ids = _child_ids(host, main_id)
    assert len(orch_ids) == 1, f"expected 1 __workflow__ child, got {orch_ids}"
    orch_id = orch_ids[0]
    orch_created = next(
        e for e in host.event_log.read(orch_id) if e.type == "TaskCreated"
    )
    assert orch_created.payload.agent_name == WORKFLOW_AGENT_NAME

    # The orchestration script spawned >= 1 real worker, each on its own
    # completed stream.
    worker_ids = _child_ids(host, orch_id)
    assert worker_ids, "orchestration script spawned no worker"
    for wid in worker_ids:
        assert "TaskCompleted" in _events_of(host, wid), wid

    # The workflow's answer folded back to main as the run_workflow tool_result.
    main_task = fold(host.event_log, host.content_store, main_id)
    paired = _run_workflow_result(main_task)
    assert paired, "no run_workflow tool_result folded back to main"
    assert paired[0].output, "workflow answer was empty"


# ---------------------------------------------------------------------------
# Loop 3 — workflow: parallel() fans a batch out (deterministic parallel proof)
# ---------------------------------------------------------------------------


@requires_live
def test_live_workflow_parallel(tmp_path: Path) -> None:
    ws = _seed_ws(tmp_path)
    host, driver = _delegating_host(ws, workflow_allowed=True)
    out = driver.start(
        goal=(
            "Use the run_workflow tool. Pass this exact script as the `script` "
            "argument and nothing else:\n\n"
            'results = parallel(["read alpha.txt and report only its code word", '
            '"read beta.txt and report only its code word"], agent="explore")\n'
            'return "|".join(results)\n\n'
            "The script must be deterministic: no imports, no time or random, no "
            "direct file or network access. After the workflow returns, reply "
            "with its result."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    main_id = out.task_id

    orch_ids = _child_ids(host, main_id)
    assert len(orch_ids) == 1, f"expected 1 __workflow__ child, got {orch_ids}"
    orch_id = orch_ids[0]

    # parallel([...]) laid out a batch: the orchestration stream spawned >= 2
    # workers. This is script-driven, so it holds regardless of model turn-shape.
    worker_ids = _child_ids(host, orch_id)
    assert len(worker_ids) >= 2, (
        f"expected >= 2 workers from parallel(), got {len(worker_ids)}"
    )
    for wid in worker_ids:
        assert "TaskCompleted" in _events_of(host, wid), wid

    # The combined answer ("a|b") folded back to main.
    main_task = fold(host.event_log, host.content_store, main_id)
    paired = _run_workflow_result(main_task)
    assert paired, "no run_workflow tool_result folded back to main"
    assert "|" in paired[0].output, (
        f"expected joined parallel result, got {paired[0].output!r}"
    )
