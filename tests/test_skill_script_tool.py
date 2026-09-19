"""`RunSkillScriptTool` invoke-level boundaries.

Directly exercises the tool's invoke: the happy path's audit fields +
the boundary branches (unknown suffix, symlink escape, shell-meta arg,
arg count/length, size cap, interpreter/OSError) must each yield
``ToolResult(success=False)`` WITHOUT spawning — the injected runner is
asserted never called on the failure branches.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.runtime.workspace import WorkspaceRoot
from noeta.builtins.skills.impl.script import RunSkillScriptTool


def _ws(tmp_path: Path) -> WorkspaceRoot:
    (tmp_path / "ws").mkdir()
    return WorkspaceRoot.from_path(tmp_path / "ws")


def _ctx() -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore())


def _ran_runner(out: bytes = b"ok") -> Any:
    def runner(argv: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[bytes]":
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr=b"")
    return runner


def _boom_runner() -> Any:
    def runner(argv: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[bytes]":
        raise AssertionError("runner must not be called on this branch")
    return runner


def _skill_root(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "skroot"
    root.mkdir()
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root.resolve()


# ---------------------------------------------------------------------------
# happy path + audit fields
# ---------------------------------------------------------------------------


def test_happy_path_runs_and_audits(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"run.sh": "echo hi\n"})
    raw = (root / "run.sh").read_bytes()
    tool = RunSkillScriptTool(
        workspace=_ws(tmp_path),
        scripts=(("s", "run.sh", root),),
        runner=_ran_runner(b"out"),
    )
    res = tool.invoke({"skill": "s", "relpath": "run.sh", "args": ["--x"]}, _ctx())
    assert res.success is True
    out = res.output
    assert out["skill"] == "s" and out["relpath"] == "run.sh"
    assert out["interpreter"] == "bash"
    assert out["argv"][0] == "bash"
    assert out["argv"][1] == str(root / "run.sh")  # resolved script path
    assert out["argv"][2] == "--x"
    assert out["cwd"] == str((tmp_path / "ws").resolve())
    assert out["exit_code"] == 0
    assert out["resource_hash"] == hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# boundary branches — success=False, runner NEVER called
# ---------------------------------------------------------------------------


def test_undiscovered_script_no_spawn(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"run.sh": "x\n"})
    tool = RunSkillScriptTool(workspace=_ws(tmp_path), scripts=(("s", "run.sh", root),), runner=_boom_runner())
    res = tool.invoke({"skill": "s", "relpath": "ghost.sh"}, _ctx())
    assert res.success is False


def test_unknown_suffix_no_spawn(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"data.bin": "x\n"})
    # even if it's in the map, an unknown suffix has no interpreter.
    tool = RunSkillScriptTool(workspace=_ws(tmp_path), scripts=(("s", "data.bin", root),), runner=_boom_runner())
    res = tool.invoke({"skill": "s", "relpath": "data.bin"}, _ctx())
    assert res.success is False


def test_symlink_escape_no_spawn(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {})
    outside = tmp_path / "secret.sh"
    outside.write_text("echo secret\n", encoding="utf-8")
    os.symlink(outside, root / "link.sh")
    tool = RunSkillScriptTool(workspace=_ws(tmp_path), scripts=(("s", "link.sh", root),), runner=_boom_runner())
    res = tool.invoke({"skill": "s", "relpath": "link.sh"}, _ctx())
    assert res.success is False


def test_shell_meta_arg_no_spawn(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"run.sh": "x\n"})
    tool = RunSkillScriptTool(workspace=_ws(tmp_path), scripts=(("s", "run.sh", root),), runner=_boom_runner())
    res = tool.invoke({"skill": "s", "relpath": "run.sh", "args": ["a; rm -rf /"]}, _ctx())
    assert res.success is False


def test_too_many_args_no_spawn(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"run.sh": "x\n"})
    tool = RunSkillScriptTool(workspace=_ws(tmp_path), scripts=(("s", "run.sh", root),), runner=_boom_runner())
    res = tool.invoke({"skill": "s", "relpath": "run.sh", "args": ["a"] * 17}, _ctx())
    assert res.success is False


def test_overlong_arg_no_spawn(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"run.sh": "x\n"})
    tool = RunSkillScriptTool(workspace=_ws(tmp_path), scripts=(("s", "run.sh", root),), runner=_boom_runner())
    res = tool.invoke({"skill": "s", "relpath": "run.sh", "args": ["x" * 5000]}, _ctx())
    assert res.success is False


def test_empty_arg_no_spawn(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"run.sh": "x\n"})
    tool = RunSkillScriptTool(workspace=_ws(tmp_path), scripts=(("s", "run.sh", root),), runner=_boom_runner())
    res = tool.invoke({"skill": "s", "relpath": "run.sh", "args": [""]}, _ctx())
    assert res.success is False


def test_size_cap_no_spawn(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"big.sh": "x" * (64 * 1024 + 1)})
    tool = RunSkillScriptTool(workspace=_ws(tmp_path), scripts=(("s", "big.sh", root),), runner=_boom_runner())
    res = tool.invoke({"skill": "s", "relpath": "big.sh"}, _ctx())
    assert res.success is False


# ---------------------------------------------------------------------------
# interpreter / OSError → success=False (not a half-enveloped raise)
# ---------------------------------------------------------------------------


def test_oserror_runner_yields_failure_result(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"run.sh": "x\n"})

    def oserr_runner(argv: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[bytes]":
        raise FileNotFoundError("bash not found")

    tool = RunSkillScriptTool(
        workspace=_ws(tmp_path), scripts=(("s", "run.sh", root),), runner=oserr_runner
    )
    res = tool.invoke({"skill": "s", "relpath": "run.sh"}, _ctx())
    assert res.success is False  # typed failure, not a propagated OSError


# ---------------------------------------------------------------------------
# teach at the point of failure: schema descriptions + an actionable refusal
# ---------------------------------------------------------------------------


def test_schema_properties_are_described(tmp_path: Path) -> None:
    """All three properties carry a description, and ``relpath``'s says what it
    is relative to.

    The only path the model is ever shown is the skill's ABSOLUTE base
    directory, so an undescribed ``relpath`` is a guess between an absolute
    path, a workspace-relative one and a skill-relative one.
    """
    tool = RunSkillScriptTool(workspace=_ws(tmp_path))
    props = tool.input_schema["properties"]
    assert set(props) == {"skill", "relpath", "args"}
    for name in props:
        assert props[name]["description"].strip(), name
    assert "relative to that skill's own directory" in props["relpath"]["description"]


def test_undiscovered_script_error_names_the_form_and_the_real_scripts(
    tmp_path: Path,
) -> None:
    root = _skill_root(tmp_path, {"run.sh": "x\n", "scripts/check.sh": "x\n"})
    tool = RunSkillScriptTool(
        workspace=_ws(tmp_path),
        scripts=(("s", "run.sh", root), ("s", "scripts/check.sh", root)),
        runner=_boom_runner(),
    )
    res = tool.invoke({"skill": "s", "relpath": str(root / "run.sh")}, _ctx())
    assert res.success is False
    assert "is not a discovered script of skill 's'" in res.summary
    assert "'relpath' must be relative to the skill's own directory" in res.summary
    assert "discovered scripts for it: run.sh, scripts/check.sh" in res.summary


def test_undiscovered_script_error_when_the_skill_bundles_none(
    tmp_path: Path,
) -> None:
    tool = RunSkillScriptTool(
        workspace=_ws(tmp_path), scripts=(), runner=_boom_runner()
    )
    res = tool.invoke({"skill": "s", "relpath": "run.sh"}, _ctx())
    assert res.success is False
    assert "skill 's' bundles no scripts" in res.summary


# ---------------------------------------------------------------------------
# a run that failed says so, the way Bash does
# ---------------------------------------------------------------------------


def _exit_runner(code: int, out: bytes = b"", err: bytes = b"") -> Any:
    def runner(argv: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[bytes]":
        return subprocess.CompletedProcess(
            args=argv, returncode=code, stdout=out, stderr=err
        )
    return runner


def test_nonzero_exit_is_a_failed_result(tmp_path: Path) -> None:
    """A script that exited 3 did not do what it was asked.

    It used to come back ``success=True`` with the exit code buried in an
    output field, so the model read a failed run as a successful one — ``Bash``
    has always flagged the same thing as a failure.
    """
    root = _skill_root(tmp_path, {"run.sh": "exit 3\n"})
    tool = RunSkillScriptTool(
        workspace=_ws(tmp_path),
        scripts=(("s", "run.sh", root),),
        runner=_exit_runner(3, err=b"boom"),
    )
    res = tool.invoke({"skill": "s", "relpath": "run.sh"}, _ctx())
    assert res.success is False
    # Same body — the model still reads what went wrong.
    assert res.output["exit_code"] == 3
    assert res.output["stderr_tail"] == "boom"
    assert "exit=3" in res.summary


def test_timeout_is_a_failed_result(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"run.sh": "sleep 1\n"})

    def runner(argv: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[bytes]":
        raise subprocess.TimeoutExpired(argv, 1.0, output=b"partial", stderr=b"")

    tool = RunSkillScriptTool(
        workspace=_ws(tmp_path), scripts=(("s", "run.sh", root),), runner=runner
    )
    res = tool.invoke({"skill": "s", "relpath": "run.sh"}, _ctx())
    assert res.success is False
    assert res.output["timed_out"] is True
    assert "timeout" in res.summary


def test_clean_exit_is_still_a_success(tmp_path: Path) -> None:
    root = _skill_root(tmp_path, {"run.sh": "echo hi\n"})
    tool = RunSkillScriptTool(
        workspace=_ws(tmp_path),
        scripts=(("s", "run.sh", root),),
        runner=_ran_runner(b"hi"),
    )
    res = tool.invoke({"skill": "s", "relpath": "run.sh"}, _ctx())
    assert res.success is True
