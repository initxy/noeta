"""The named agent presets, and what each one is physically able to do.

``explore`` and ``plan`` are read-only by construction, and two independent
layers have to hold: the host filters the write family out of the pack before
the Engine sees it, and the PermissionGuard denies anything the filter would
miss. A goal that explicitly tempts a write tool must therefore leave the
workspace byte-identical. The scripted bug-fixer loop pins the other end — a
whole pytest → grep → read → edit → pytest run over the fixture repo lands its
edit and records the activated skill.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._read_models.result import (
    CodeSessionResult,
    _collect_failed_edits,
    _collect_files_changed,
    _last_selected_skills,
    _last_shell_result,
)
from noeta.agent.spec import agent_activates
from noeta.client.parts import builtin_tool_classes
from noeta.presets import official_specs
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "bugfix_repo"


def _result(host, out) -> CodeSessionResult:
    """Project the ``CodeSessionResult`` read model off the durable EventLog of
    a one-shot ``driver.start`` outcome."""
    events = host.event_log.read(out.task_id)
    cs = host.content_store
    return CodeSessionResult(
        task_id=out.task_id,
        status=out.status,
        events=len(events),
        selected_skills=_last_selected_skills(events, cs),
        files_changed=_collect_files_changed(events, cs),
        failed_edits=_collect_failed_edits(events, cs),
        last_shell=_last_shell_result(events, cs),
    )

# Canonical specs (local aliases for readability)
_SPECS = official_specs()
MAIN_SPEC = _SPECS["main"]
EXPLORE_SPEC = _SPECS["explore"]
PLAN_SPEC = _SPECS["plan"]
GENERAL_PURPOSE_SPEC = _SPECS["general-purpose"]

def _tools(spec):
    return frozenset(r.name for r in spec.tools)


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["main", "general-purpose", "explore", "plan"],
)
def test_named_agents_resolve(name: str) -> None:
    spec = _SPECS[name]
    # provider-safe lowercase + hyphen
    assert name == name.lower()
    # Instructions carry the role and the workflow rules; the tool catalog
    # lives in each tool's structured description, not restated here. `plan`
    # heads its workflow "Process:", the others "Rules:".
    assert spec.instructions.strip()
    assert "Rules:" in spec.instructions or "Process:" in spec.instructions


def test_official_specs_has_exact_four_canonical() -> None:
    """official_specs() exposes exactly 4 canonical names (no default alias)."""
    assert set(_SPECS.keys()) == {
        "main",
        "general-purpose",
        "explore",
        "plan",
    }


# ---------------------------------------------------------------------------
# Read-only agents are provably write-free
# ---------------------------------------------------------------------------


def test_explore_runner_drops_write_tools_from_pack(
    tmp_path: Path,
) -> None:
    """Even with APPLY + ARBITRARY modes, explore's live pack physically
    excludes the write family (edit/write/apply_patch): the host filters the
    pack against the spec's tool list before the Engine sees it. Explore does
    carry ``shell_run`` — read-only there is prompt-enforced, not achieved by
    taking the tool away."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    host = make_host(
        make_registry(runner_main_spec("explore")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=_end_turn_immediately()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ARBITRARY,
    )
    # The Engine's tool dict is filtered to the agent's allow-list.
    engine_tools = host.resolve_engine_for_agent("explore", model="gpt-test")._tools  # type: ignore[union-attr]
    assert "Edit" not in engine_tools
    assert "Write" not in engine_tools
    assert "apply_patch" not in engine_tools
    # The scout tools (incl. read-only shell + webfetch) are present.
    for present in ("Read", "Glob", "Grep", "Bash", "BashOutput", "webfetch"):
        assert present in engine_tools


def test_plan_runner_pack_is_readonly_scout_no_write(
    tmp_path: Path,
) -> None:
    """``plan`` gets the same read-mostly scout set as explore — read/grep/glob
    + the shell triplet + webfetch — and no write family at all. It returns its
    plan as a message, so physically it can never reach an editor."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    host = make_host(
        make_registry(runner_main_spec("plan")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=_end_turn_immediately()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ARBITRARY,
    )
    engine_tools = host.resolve_engine_for_agent("plan", model="gpt-test")._tools  # type: ignore[union-attr]
    for absent in ("Edit", "Write", "apply_patch"):
        assert absent not in engine_tools
    # The scout tools (incl. read-only shell + webfetch) are present.
    for present in ("Read", "Glob", "Grep", "Bash", "BashOutput", "webfetch"):
        assert present in engine_tools


def _edit_tempted_response() -> list[LLMResponse]:
    """A write-tempting script: the LLM asks for `edit`. A
    read-only agent should refuse via PermissionGuard (denied tool),
    not by silently no-oping."""
    return [
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="tempt-1",
                    tool_name="Edit",
                    arguments={
                        "file_path": "src/math_ops.py",
                        "old_string": "return a - b",
                        "new_string": "return a + b",
                    },
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "tempt-1"},
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="OK, I cannot edit. Here is my review.")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "tempt-2"},
        ),
    ]


def test_read_only_agent_write_tempting_goal_results_in_no_write(
    tmp_path: Path,
) -> None:
    """End-to-end regression: even with a goal that prompts the
    fake-LLM to ask for `edit`, the workspace is byte-identical
    after the run AND a ``ToolCallDenied`` event lands in the EventLog."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "x.py"
    target.write_text("return a - b\n")
    original = target.read_bytes()

    host = make_host(
        make_registry(runner_main_spec("explore")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=_edit_tempted_response()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ARBITRARY,
    )
    out = make_driver(host).start(
        goal="please change a - b to a + b", agent="explore"
    )
    result = _result(host, out)

    assert result.status == "terminal"
    assert target.read_bytes() == original
    assert result.files_changed == ()
    # PermissionGuard denial recorded.
    types = [env.type for env in host.event_log.read(out.task_id)]
    assert "ToolCallDenied" in types


def _end_turn_immediately() -> list[LLMResponse]:
    return [
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="no work needed")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "end-1"},
        ),
    ]


# ---------------------------------------------------------------------------
# Named Agents — allow-list shape
# ---------------------------------------------------------------------------


def test_general_purpose_has_full_builtin_set() -> None:
    """``general-purpose`` carries the whole built-in tool surface."""
    gp_tools = _tools(GENERAL_PURPOSE_SPEC)
    assert gp_tools == frozenset(builtin_tool_classes())
    assert {"Grep", "Glob", "webfetch"} <= gp_tools


def test_main_and_general_purpose_tools_now_equal() -> None:
    """``general-purpose`` and ``main`` share one surface: the full built-in
    catalog."""
    assert _tools(GENERAL_PURPOSE_SPEC) == _tools(MAIN_SPEC)
    assert _tools(MAIN_SPEC) == frozenset(builtin_tool_classes())


def test_explore_is_read_only() -> None:
    # Explore excludes the write family; shell_run stays, prompt-restricted to
    # read-only commands.
    ex_tools = _tools(EXPLORE_SPEC)
    for mutating in ("Edit", "Write", "apply_patch"):
        assert mutating not in ex_tools
    assert "Bash" in ex_tools


def test_plan_whitelist_and_capabilities() -> None:
    # Plan's whitelist is the read-mostly scout set (same as explore) —
    # read/grep/glob + shell triplet + webfetch — and NO write family at all.
    # Activation opens ONLY ask_user_question (no todo_write).
    plan_tools = _tools(PLAN_SPEC)
    for mutating in ("Edit", "Write", "apply_patch"):
        assert mutating not in plan_tools
    assert plan_tools == frozenset(
        {"Read", "Grep", "Glob", "Bash", "BashOutput", "KillShell", "webfetch"}
    )
    assert agent_activates(PLAN_SPEC, "todo_write") is False
    assert agent_activates(PLAN_SPEC, "ask_user_question") is True
    assert agent_activates(PLAN_SPEC, "skill_invocation") is False


# ---------------------------------------------------------------------------
# Deterministic bug-fixer full loop on the fixture
# ---------------------------------------------------------------------------


_PYTEST_FAIL_TAIL = (
    b"_______________________ test_add_returns_sum _______________________\n"
    b"\n"
    b"    def test_add_returns_sum() -> None:\n"
    b">       assert add(2, 3) == 5\n"
    b"E       assert -1 == 5\n"
    b"E        +  where -1 = add(2, 3)\n"
    b"\n"
    b"tests/test_add.py:13: AssertionError\n"
    b"==================== 1 failed, 1 passed in 0.05s ====================\n"
)

_PYTEST_PASS_TAIL = b"==================== 2 passed in 0.04s ====================\n"


def _bug_fixer_script() -> list[LLMResponse]:
    """The scripted turns that mirror the skill body: pytest → grep → read →
    edit → pytest → summary."""
    return [
        # Turn 1: run pytest (sees the failure)
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="bf-1",
                    tool_name="Bash",
                    arguments={"command": "pytest -q"},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "bf-1"},
        ),
        # Turn 2: search for the offending function. The script goes through
        # `shell_run grep` rather than the `grep` tool to keep the recording
        # stable.
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="bf-2",
                    tool_name="Bash",
                    arguments={"command": "grep -rn 'def add' src/"},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "bf-2"},
        ),
        # Turn 3: read math_ops.py
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="bf-3",
                    tool_name="Read",
                    arguments={"file_path": "src/math_ops.py"},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "bf-3"},
        ),
        # Turn 4: minimal edit (the actual fix)
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="bf-4",
                    tool_name="Edit",
                    arguments={
                        "file_path": "src/math_ops.py",
                        "old_string": "return a - b",
                        "new_string": "return a + b",
                    },
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "bf-4"},
        ),
        # Turn 5: rerun pytest (now green)
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="bf-5",
                    tool_name="Bash",
                    arguments={"command": "pytest -q"},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "bf-5"},
        ),
        # Turn 6: end turn with summary
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="bug fixed; suite passes")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "bf-6"},
        ),
    ]


def _make_subprocess_runner(
    workspace: Path,
) -> Any:
    """A subprocess.run stub that returns:
    * the failing pytest tail when math_ops.py still has `a - b`.
    * the passing pytest tail when math_ops.py has `a + b`.
    Any other argv returns a benign 0-exit empty result so the test
    isn't coupled to commands the bug-fixer doesn't actually issue.
    """

    def runner(
        argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        program = argv[0] if argv else ""
        if program == "pytest":
            current = (workspace / "src" / "math_ops.py").read_text()
            if "return a + b" in current:
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=0,
                    stdout=_PYTEST_PASS_TAIL,
                    stderr=b"",
                )
            return subprocess.CompletedProcess(
                args=argv,
                returncode=1,
                stdout=_PYTEST_FAIL_TAIL,
                stderr=b"",
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    return runner


def _copy_fixture(dst_root: Path) -> Path:
    """Copy ``tests/fixtures/bugfix_repo`` into ``dst_root`` and return
    the workspace path. Tests must work against the copy so the source
    tree stays at the known-failing state."""
    workspace = dst_root / "bugfix"
    shutil.copytree(_FIXTURE_ROOT, workspace)
    return workspace


def test_bug_fixer_fake_llm_full_loop_fixes_failing_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deterministic scripted run flips the workspace bug, turns pytest
    green, and activates the workspace skill."""
    workspace = _copy_fixture(tmp_path)
    target = workspace / "src" / "math_ops.py"
    assert "return a - b" in target.read_text()

    monkeypatch.setattr(
        "noeta.runtime.subproc._default_run",
        _make_subprocess_runner(workspace),
    )
    host = make_host(
        make_registry(runner_main_spec("general-purpose")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=_bug_fixer_script()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        # The host's default permission mode gates the write family; disable
        # approval so the scripted edit applies in one shot.
        require_approval_tools=(),
    )
    out = make_driver(host).start(
        goal="fix the failing test",
        agent="general-purpose",
        activations=("fix-python-test",),
    )
    result = _result(host, out)

    assert result.status == "terminal"
    # The actual fix landed.
    assert "return a + b" in target.read_text()
    # Files-changed surfaces the single edit application.
    edit_changes = [c for c in result.files_changed if c["tool"] == "Edit"]
    assert len(edit_changes) == 1
    assert edit_changes[0]["applied"] is True
    assert edit_changes[0]["path"] == "src/math_ops.py"
    # Last shell shows the green pytest run.
    assert result.last_shell is not None
    assert result.last_shell["returncode"] == 0
    assert result.last_shell["command"].startswith("pytest")
    # The workspace skill was activated durably and the ContextPlan recorded it.
    assert "fix-python-test" in result.selected_skills


# ---------------------------------------------------------------------------
# Fixture sanity (lives in tests/fixtures/bugfix_repo/)
# ---------------------------------------------------------------------------


def test_fixture_starts_with_known_failure(tmp_path: Path) -> None:
    """A meta-regression: if someone accidentally fixes the fixture, the
    bug-fixer test above silently becomes a no-op. Pin the bug bytes."""
    src = (_FIXTURE_ROOT / "src" / "math_ops.py").read_text()
    assert "return a - b" in src
    skill = (
        _FIXTURE_ROOT / ".noeta" / "skills" / "fix-python-test" / "SKILL.md"
    ).read_text()
    assert "fix-python-test" in skill
