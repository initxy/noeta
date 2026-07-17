"""Phase 4 I6 — named coding-Agents + deterministic general-purpose full loop.

Two acceptance layers wired here:

* **Read-only Agents are provably write-free** —
  ``test_read_only_agent_runner_drops_write_tools_from_pack`` packs the
  ``explore``/``plan`` agents through the runner with a goal that
  explicitly tempts a write tool. The runner filters the pack
  (defence layer 1) AND the PermissionGuard denies anything the
  filter would somehow miss (defence layer 2). Both layers must hold.

* **Deterministic fake-LLM general-purpose full loop** — copies the
  ``tests/fixtures/bugfix_repo/`` tree into ``tmp_path``, drives the
  ``general-purpose`` agent through a scripted FakeLLM that mimics the
  Skill-guided plan (pytest → grep → read → edit →
  pytest), asserts the workspace edit lands AND the skill body
  reached ``ContextPlan.selected_skills``.

The real-LLM acceptance gate (gpt-5.5) is the env-gated live suite
(``tests/test_live_context_supply_e2e.py``) and the shipping backend
(``NOETA_AGENT_CONFIG=… python -m noeta.agent``); it is intentionally
**not** in this file because it depends on the live endpoint.
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
from noeta.client.parts import BUILTIN_TOOL_CLASSES
from noeta.presets import official_specs
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "bugfix_repo"


def _result(host, out) -> CodeSessionResult:
    """Project the deleted ``CodeSessionRunner._build_result`` shape off the
    durable EventLog of a one-shot ``driver.start`` outcome — the same read-model
    helpers the noeta-agent backend uses for its CLI render."""
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

# Helper: tools frozenset from spec
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
    # instructions carry the role + workflow rules; the tool catalog now lives
    # in each tool's structured description, not restated here.
    # (plan uses a "Process:" workflow heading after the CC alignment; the
    # others use "Rules:".)
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
    """Even with APPLY + ARBITRARY shell modes, explore's live pack physically
    excludes the WRITE FAMILY (edit/write/apply_patch). The SDK host filters the
    pack via the spec's tool list BEFORE the Engine sees it. After the CC
    alignment explore DOES carry read-only shell (shell_run); its read-only
    guarantee is prompt-enforced, not by removing shell."""
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
    # The write family is physically absent.
    assert "edit" not in engine_tools
    assert "write" not in engine_tools
    assert "apply_patch" not in engine_tools
    # The scout tools (incl. read-only shell + webfetch) are present.
    for present in ("read", "glob", "grep", "shell_run", "shell_poll", "webfetch"):
        assert present in engine_tools


def test_plan_runner_pack_is_readonly_scout_no_write(
    tmp_path: Path,
) -> None:
    """CC alignment: plan's live pack is the same read-mostly scout set as
    explore — read/grep/glob + shell triplet + webfetch — and NO write family
    at all. The old restricted plans/*.md write was dropped; plan returns the
    plan as its message. Physical isolation: plan can never call any editor."""
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
    # The whole write family is physically absent — including write.
    for absent in ("edit", "write", "apply_patch"):
        assert absent not in engine_tools
    # The scout tools (incl. read-only shell + webfetch) are present.
    for present in ("read", "glob", "grep", "shell_run", "shell_poll", "webfetch"):
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
                    tool_name="edit",
                    arguments={
                        "path": "src/math_ops.py",
                        "old": "return a - b",
                        "new": "return a + b",
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
    """CC alignment: general-purpose mirrors CC's general-purpose agent — the
    full built-in tool surface (same as main), so grep/glob/apply_patch/webfetch
    are present rather than dropped."""
    gp_tools = _tools(GENERAL_PURPOSE_SPEC)
    assert gp_tools == frozenset(BUILTIN_TOOL_CLASSES)
    # The previously-dropped search/patch/web tools are now present.
    assert {"grep", "glob", "apply_patch", "webfetch"} <= gp_tools


def test_main_and_general_purpose_tools_now_equal() -> None:
    """CC alignment: general-purpose's tool surface now equals main's — both
    are the full built-in catalog (gp is no longer a strict subset)."""
    assert _tools(GENERAL_PURPOSE_SPEC) == _tools(MAIN_SPEC)
    assert _tools(MAIN_SPEC) == frozenset(BUILTIN_TOOL_CLASSES)


def test_explore_is_read_only() -> None:
    # CC alignment: explore physically excludes the write family; shell_run is
    # now present (prompt-restricted to read-only commands).
    ex_tools = _tools(EXPLORE_SPEC)
    for mutating in ("edit", "write", "apply_patch"):
        assert mutating not in ex_tools
    assert "shell_run" in ex_tools


def test_plan_whitelist_and_capabilities() -> None:
    # CC alignment: plan's whitelist is the read-mostly scout set (same as
    # explore) — read/grep/glob + shell triplet + webfetch — and NO write family
    # at all. Capabilities opens ONLY ask_user_question (no todo_write).
    plan_tools = _tools(PLAN_SPEC)
    for mutating in ("edit", "write", "apply_patch"):
        assert mutating not in plan_tools
    assert plan_tools == frozenset(
        {"read", "grep", "glob", "shell_run", "shell_poll", "shell_kill", "webfetch"}
    )
    assert PLAN_SPEC.capabilities.todo_write is False
    assert PLAN_SPEC.capabilities.ask_user_question is True
    assert PLAN_SPEC.capabilities.skill_invocation is False


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
    """The 5-turn fake-LLM script that mirrors the Skill body."""
    return [
        # Turn 1: run pytest (sees the failure)
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="bf-1",
                    tool_name="shell_run",
                    arguments={"command": "pytest -q"},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "bf-1"},
        ),
        # Turn 2: search for the offending function. (general-purpose now also
        # has the grep/glob tools after the CC alignment, but this scripted run
        # searches via shell_run grep — still in its whitelist — to keep the
        # recording stable.)
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="bf-2",
                    tool_name="shell_run",
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
                    tool_name="read",
                    arguments={"path": "src/math_ops.py"},
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
                    tool_name="edit",
                    arguments={
                        "path": "src/math_ops.py",
                        "old": "return a - b",
                        "new": "return a + b",
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
                    tool_name="shell_run",
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
    """Tier-1 CI gate: deterministic fake-LLM run flips the workspace
    bug + passes pytest + activates the workspace skill."""
    workspace = _copy_fixture(tmp_path)
    target = workspace / "src" / "math_ops.py"
    assert "return a - b" in target.read_text()

    monkeypatch.setattr(
        "noeta.tools.fs._subprocess._default_run",
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
        # The old CodeSessionConfig applied edits without approval; the SDK host
        # default permission_mode="default" gates the write family, so disable it
        # explicitly to keep the byte-for-byte one-shot apply behaviour.
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
    edit_changes = [c for c in result.files_changed if c["tool"] == "edit"]
    assert len(edit_changes) == 1
    assert edit_changes[0]["applied"] is True
    assert edit_changes[0]["path"] == "src/math_ops.py"
    # Last shell shows the green pytest run.
    assert result.last_shell is not None
    assert result.last_shell["returncode"] == 0
    assert result.last_shell["command"].startswith("pytest")
    # The workspace skill was activated (B17 durable) and the
    # ContextPlan recorded it.
    assert "fix-python-test" in result.selected_skills


# ---------------------------------------------------------------------------
# Fixture sanity (lives in tests/fixtures/bugfix_repo/)
# ---------------------------------------------------------------------------


def test_fixture_starts_with_known_failure(tmp_path: Path) -> None:
    """A meta-regression: if someone accidentally fixes the fixture
    the I6 bug-fixer test becomes a no-op. Pin the bug bytes."""
    src = (_FIXTURE_ROOT / "src" / "math_ops.py").read_text()
    assert "return a - b" in src
    skill = (
        _FIXTURE_ROOT / ".noeta" / "skills" / "fix-python-test" / "SKILL.md"
    ).read_text()
    assert "fix-python-test" in skill
