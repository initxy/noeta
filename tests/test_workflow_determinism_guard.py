"""Determinism guard for orchestration scripts (controlled namespace + AST ban on non-determinism).

Proves:
* Scripts using time/random/datetime, importing a blacklisted module, or doing
  external IO are rejected by the AST guard, with the error pointing at the
  offending line;
* A pure deterministic script passes;
* The run namespace's controlled builtins (SAFE_BUILTINS) exclude
  open/eval/exec/__import__ and include the common deterministic builtins;
* The guard runs at workflow startup (translation time): an offending script is
  turned back by an ack right at ``run_workflow`` translation, producing **no
  SpawnSubtaskDecision / no half-run subtask**.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.policies._control_translate import ControlToggles, translate_control_tool
from noeta.policies._workflow_sandbox import SAFE_BUILTINS, check_workflow_script
from noeta.policies.control_tools import RUN_WORKFLOW_TOOL
from noeta.protocols.decisions import SpawnSubtaskDecision, StatePatchDecision
from noeta.protocols.messages import (
    LLMResponse,
    Message,
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


# ---------------------------------------------------------------------------
# AST guard — rejections (each points at a line) + the deterministic pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "import time\nreturn time.time()\n",
        "import random\nreturn random.random()\n",
        "import datetime\nreturn 1\n",
        "from os import getcwd\nreturn getcwd()\n",
        "return open('/etc/passwd').read()\n",
        "return eval('1+1')\n",
        "return __import__('os').getcwd()\n",
        "return time.time()\n",  # bare reference, no import
        "return random.random()\n",
        "return ().__class__.__bases__\n",  # reflection escape
    ],
)
def test_nondeterministic_scripts_rejected_with_location(script: str) -> None:
    err = check_workflow_script(script)
    assert err is not None, f"expected rejection for: {script!r}"
    assert "line" in err  # error points at the offending node


def test_pure_deterministic_script_passes() -> None:
    script = (
        "total = 0\n"
        "for i in range(3):\n"
        "    r = agent('step ' + str(i))\n"
        "    total += len(r)\n"
        "return {'count': total, 'first': args.get('x')}\n"
    )
    assert check_workflow_script(script) is None


def test_module_name_as_local_variable_is_allowed() -> None:
    # ``os`` here is a plain local variable, not the stdlib module — allowed
    # (the guard only rejects a non-deterministic name that is never assigned).
    assert check_workflow_script("os = agent('x')\nreturn os\n") is None


def test_syntax_error_rejected_at_guard() -> None:
    err = check_workflow_script("return (1 +\n")
    assert err is not None and "syntax error" in err


# ---------------------------------------------------------------------------
# Controlled namespace — SAFE_BUILTINS excludes the dangerous, keeps the useful
# ---------------------------------------------------------------------------


def test_safe_builtins_exclude_nondeterministic_and_io() -> None:
    for forbidden in ("open", "eval", "exec", "compile", "__import__", "input", "print"):
        assert forbidden not in SAFE_BUILTINS, forbidden


def test_safe_builtins_keep_common_deterministic_helpers() -> None:
    for ok in ("len", "range", "sorted", "dict", "list", "str", "int", "sum", "enumerate"):
        assert ok in SAFE_BUILTINS, ok


# ---------------------------------------------------------------------------
# Startup-time guard: a bad script is rejected at run_workflow TRANSLATION,
# producing a recoverable ack and NO SpawnSubtaskDecision (no half-run subtask).
# ---------------------------------------------------------------------------


def _run_workflow_response(script: str) -> LLMResponse:
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


def test_bad_script_translates_to_ack_not_spawn() -> None:
    resp = _run_workflow_response("import time\nreturn time.time()\n")
    assistant = Message(role="assistant", content=list(resp.content))
    decision = translate_control_tool(
        resp, assistant, toggles=ControlToggles(workflow=True)
    )
    # Recoverable ack (no patch), NOT a spawn — no orchestration subtask created.
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is None
    assert not isinstance(decision, SpawnSubtaskDecision)
    ack = decision.messages_after[0]
    assert ack.role == "tool"
    assert ack.content[0].success is False
    assert "forbids" in ack.content[0].output


def test_good_script_translates_to_spawn() -> None:
    resp = _run_workflow_response('return agent("do it")\n')
    assistant = Message(role="assistant", content=list(resp.content))
    decision = translate_control_tool(
        resp, assistant, toggles=ControlToggles(workflow=True)
    )
    assert isinstance(decision, SpawnSubtaskDecision)


# ---------------------------------------------------------------------------
# E2E: a bad script never spawns a subtask (no half-run orchestration child).
# ---------------------------------------------------------------------------


def test_bad_script_e2e_spawns_no_subtask(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    # workflow_enabled=True → host workflow_allowed=True AND delegation on the
    # main spec (the SDK host exposes run_workflow only when both hold). The
    # reserved __workflow__ orchestration child is built by the host itself.
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    host = make_host(
        make_registry(main, preset_spec("explore")),
        workspace_dir=ws,
        provider=FakeLLMProvider(
            responses=[
                _run_workflow_response("import os\nreturn os.getcwd()\n"),
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="ok, giving up on the script")],
                    usage=Usage(uncached=1, output=1),
                    raw={"id": "end"},
                ),
            ]
        ),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        workflow_allowed=True,
        budget=coding_replay_budget(3),
    )
    driver = make_driver(host)
    out = driver.start(goal="try a bad workflow", agent="main")
    assert out.status == "terminal"
    types = [e.type for e in host.event_log.read(out.task_id)]
    # The bad script was rejected at translation → recoverable ack, then the
    # model finished. No subtask (orchestration or otherwise) was ever spawned.
    assert "SubtaskSpawned" not in types
