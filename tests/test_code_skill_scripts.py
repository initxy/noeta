"""Phase 4.5 Issue E — skill-bundled script execution through `noeta code`.

A skill bundles `run.sh`; with `--allow-skill-scripts` the model can call
`run_skill_script(skill, relpath)`, which is **always** gated by the
PermissionGuard E precheck + human approval (Issue A) and only then
executes via an allowlisted interpreter. Default off ⇒ the tool does not
exist.

Live recording uses a **fake `subprocess.run`** (so the test does not
depend on a real `bash`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from noeta.execution.skills import build_skill_script_wiring, load_workspace_skills
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import WorkspaceRoot
from noeta.tools.fs.skill_script import SKILL_SCRIPT_TOOL_NAME
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


RUN_CALL_ID = "rs1"


def _fake_run(*_a: object, **_k: object) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.CompletedProcess(args=["bash"], returncode=0, stdout=b"ran", stderr=b"")


def _boom(*_a: object, **_k: object) -> None:
    raise AssertionError("a denied/unrun script must not spawn a subprocess")


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    skill = ws / ".noeta" / "skills" / "scripted"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: scripted\ndescription: bundles a script\n---\nUse run.sh.\n",
        encoding="utf-8",
    )
    (skill / "run.sh").write_text("echo hi\n", encoding="utf-8")
    return ws


def _run_script_call(skill: str = "scripted", relpath: str = "run.sh") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=RUN_CALL_ID,
                tool_name=SKILL_SCRIPT_TOOL_NAME,
                arguments={"skill": skill, "relpath": relpath},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": RUN_CALL_ID},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _session(ws: Path, responses: list[LLMResponse], *, enabled: bool):
    """A one-shot SDK host that may enable skill-bundled scripts.

    ``allow_skill_scripts=enabled`` is the host knob; ``extra_skills=("scripted",)``
    maps to the driver's pre-loop ``activations``. Returns ``(host, driver,
    provider)`` — the shared ``FakeLLMProvider`` carries ``received_requests`` for
    the white-box schema assertions."""
    provider = FakeLLMProvider(responses=responses)
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        allow_skill_scripts=enabled,
    )
    return host, make_driver(host), provider


# ---------------------------------------------------------------------------
# default off
# ---------------------------------------------------------------------------


def test_default_off_tool_absent(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, _driver, _provider = _session(ws, [_end("done")], enabled=False)
    engine = host.resolve_engine_for_agent("main", model="gpt-test")
    assert SKILL_SCRIPT_TOOL_NAME not in engine._tools  # type: ignore[union-attr]


def test_enabled_tool_present(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, _driver, _provider = _session(ws, [_end("done")], enabled=True)
    engine = host.resolve_engine_for_agent("main", model="gpt-test")
    assert SKILL_SCRIPT_TOOL_NAME in engine._tools  # type: ignore[union-attr]


_NO_REF_SKILL = (
    "---\nname: scripted\ndescription: bundles a script\n---\nNo references.\n"
)


def _make_ws_no_ref(tmp_path: Path) -> Path:
    """A skill whose body does NOT name run.sh — so Issue D does not
    inline it; isolates Issue E's effect on the tool schema / stable hash."""
    ws = tmp_path / "ws"
    skill = ws / ".noeta" / "skills" / "scripted"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(_NO_REF_SKILL, encoding="utf-8")
    (skill / "run.sh").write_text("echo hi\n", encoding="utf-8")
    return ws


def _first_request_tools_and_hash(host, provider, out) -> tuple[list[str], str]:
    import json

    tool_names = [t["function"]["name"] for t in provider.received_requests[0].tools]
    events = host.event_log.read(out.task_id)
    plan_ref = next(
        e.payload.plan_ref for e in events if e.type == "ContextPlanComposed"
    )
    plan = json.loads(host.content_store.get(plan_ref).decode("utf-8"))
    return tool_names, plan["segment_hashes"]["stable_prefix"]


def test_default_off_schema_and_stable_hash_unchanged(tmp_path: Path) -> None:
    """default off: `run_skill_script` is not in the provider tool schema;
    enabling it adds the schema AND rotates the stable_prefix hash."""
    off_host, off_driver, off_provider = _session(
        _make_ws_no_ref(tmp_path / "off"), [_end("done")], enabled=False
    )
    on_host, on_driver, on_provider = _session(
        _make_ws_no_ref(tmp_path / "on"), [_end("done")], enabled=True
    )
    off_out = off_driver.start(
        goal="run the script", agent="main", activations=("scripted",)
    )
    on_out = on_driver.start(
        goal="run the script", agent="main", activations=("scripted",)
    )
    off_tools, off_hash = _first_request_tools_and_hash(off_host, off_provider, off_out)
    on_tools, on_hash = _first_request_tools_and_hash(on_host, on_provider, on_out)
    assert SKILL_SCRIPT_TOOL_NAME not in off_tools
    assert SKILL_SCRIPT_TOOL_NAME in on_tools
    assert on_hash != off_hash  # tool surface rotates the stable hash


def test_child_runtime_never_enables_skill_scripts(tmp_path: Path) -> None:
    # build_skill_script_wiring(enabled=False) — the child path — yields
    # no tool + empty guard fields regardless of the registry.
    ws = _make_ws(tmp_path)
    registry = load_workspace_skills(ws, override_skills_dir=ws / ".noeta" / "skills")
    tool, sst, ss = build_skill_script_wiring(
        registry, WorkspaceRoot.from_path(ws), enabled=False
    )
    assert tool is None and sst == frozenset() and ss == frozenset()


# ---------------------------------------------------------------------------
# approve → runs (fake subprocess); deny → not run; both: no spawn on replay
# ---------------------------------------------------------------------------


def test_approve_runs_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _make_ws(tmp_path)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    host, driver, _provider = _session(ws, [_run_script_call(), _end("done")], enabled=True)
    out = driver.start(goal="run the script", agent="main", activations=("scripted",))
    assert out.status == "suspended"  # gated for approval, not run yet
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "ToolCallApprovalRequested" in types
    assert "ToolResultRecorded" not in types

    result = driver.approve(out.task_id, call_id=RUN_CALL_ID)
    assert result.status == "terminal"
    events = host.event_log.read(out.task_id)
    types = [e.type for e in events]
    assert "ToolResultRecorded" in types  # the approved script ran


def test_deny_does_not_run_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _make_ws(tmp_path)
    monkeypatch.setattr(subprocess, "run", _boom)  # nothing should spawn
    host, driver, _provider = _session(ws, [_run_script_call(), _end("done")], enabled=True)
    out = driver.start(goal="run the script", agent="main", activations=("scripted",))
    result = driver.deny(out.task_id, call_id=RUN_CALL_ID, reason="no")
    assert result.status == "terminal"
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "ToolResultRecorded" not in types  # never executed


def test_undiscovered_relpath_denied_no_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _make_ws(tmp_path)
    monkeypatch.setattr(subprocess, "run", _boom)
    host, driver, _provider = _session(
        ws, [_run_script_call(relpath="ghost.sh"), _end("done")], enabled=True
    )
    out = driver.start(goal="run the script", agent="main", activations=("scripted",))
    types = [e.type for e in host.event_log.read(out.task_id)]
    # guard E precheck denies an undiscovered script → ToolCallDenied,
    # no approval suspend, no subprocess.
    assert "ToolCallDenied" in types
    assert "ToolCallApprovalRequested" not in types
    assert "ToolResultRecorded" not in types
