"""``Bash`` in the foreground: real exit codes and output through
``LocalExecEnv``, the persistent working directory, the leading-sleep block,
git hardening, the capture cap and the large-output rule."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.execenv.base import RunResult
from noeta.execenv.local import LocalExecEnv
from noeta.tools.base import ToolContext
from noeta.tools.bash import BashTool, resolve_timeout
from noeta.tools.fs_state import FS, KIND_SHELL_CWD, FsState

from .conftest import Harness, text_of


async def bash(h: Harness, command: str, tool: BashTool | None = None, **extra: Any) -> Any:
    return await h.call(tool or BashTool(), command=command, **extra)


# -- the happy path ----------------------------------------------------------


async def test_echo_captures_stdout(h: Harness) -> None:
    out = await bash(h, "printf hello-noeta")
    assert not out.is_error
    assert text_of(out) == "hello-noeta"
    assert out.meta["exit_code"] == 0


async def test_python_computes(h: Harness) -> None:
    out = await bash(h, 'python3 -c "print(6*7)"')
    assert "42" in text_of(out)


async def test_nonzero_exit_leads_with_the_code(h: Harness) -> None:
    out = await bash(h, "printf hi; exit 3")
    assert out.is_error
    assert text_of(out) == "Exit code 3\nhi"
    assert out.meta["exit_code"] == 3
    bare = await bash(h, "sh -c 'exit 4'")
    assert bare.is_error and text_of(bare) == "Exit code 4"


async def test_stderr_labeled_only_when_both_streams(h: Harness) -> None:
    both = await bash(h, "sh -c 'echo out; echo err 1>&2'")
    assert text_of(both) == "out\nstderr:\nerr"
    only_err = await bash(h, "sh -c 'echo lone-err 1>&2'")
    assert text_of(only_err) == "lone-err"


async def test_empty_output_is_marked(h: Harness) -> None:
    assert text_of(await bash(h, "true")) == "(no output)"


async def test_runs_in_the_workspace(h: Harness, ws: Path) -> None:
    (ws / "marker.txt").write_text("x")
    assert "marker.txt" in text_of(await bash(h, "ls"))


async def test_bad_input_is_refused(h: Harness) -> None:
    for command in ("", "   ", None, 3):
        out = await h.call(BashTool(), command=command)
        assert out.is_error and text_of(out) == "Bash: requires non-empty 'command'"


async def test_background_is_refused_without_the_extension(h: Harness, ws: Path) -> None:
    out = await bash(h, f"touch {ws / 'ran'}", run_in_background=True)
    assert out.is_error
    assert text_of(out) == "Bash: background execution is not available on this host"
    assert not (ws / "ran").exists()


# -- timeout -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 120),
        (0, 120),
        (-5, 120),
        (True, 120),
        ("1000", 120),
        (500, 1),
        (1000, 1),
        (2500, 2),
        (2500.9, 2),
        (600_000, 600),
        (10_000_000, 600),
    ],
)
def test_timeout_is_milliseconds_clamped(raw: Any, expected: int) -> None:
    assert resolve_timeout(raw, 120) == expected


async def test_timeout_is_an_error_with_the_output_so_far(h: Harness) -> None:
    out = await bash(h, "echo early; sleep 30", timeout=1000)
    assert out.is_error
    assert text_of(out) == "Command timed out after 1s\nearly"
    assert out.meta["timed_out"] is True


# -- the long leading sleep ---------------------------------------------------


async def test_a_long_leading_sleep_is_blocked_before_it_runs(h: Harness, ws: Path) -> None:
    marker = ws / "ran"
    out = await bash(h, f"sleep 110 && touch {marker}")
    assert out.is_error
    assert text_of(out) == (
        f"Bash: Blocked: sleep 110 followed by: touch {marker}. To wait for a "
        "condition, use Monitor with an until-loop (e.g. `until <check>; do "
        "sleep 2; done`). To wait for a command you started, use "
        "run_in_background: true. Do not chain shorter sleeps to work around "
        "this block."
    )
    assert not marker.exists()


async def test_a_short_sleep_still_runs(h: Harness) -> None:
    assert text_of(await bash(h, "sleep 0.1 && echo ok")) == "ok"


# -- the working directory persists --------------------------------------------


async def test_the_directory_persists_between_commands(h: Harness) -> None:
    first = await bash(h, "mkdir -p sub/deeper && cd sub")
    assert not first.is_error and text_of(first) == "(no output)"
    assert text_of(await bash(h, "pwd")).strip() == str(h.root / "sub")
    await bash(h, "cd deeper")
    assert text_of(await bash(h, "pwd")).strip() == str(h.root / "sub" / "deeper")
    assert [r for r in h.records if r[1] == KIND_SHELL_CWD] == [
        (FS, KIND_SHELL_CWD, {"cwd": str(h.root / "sub")}),
        (FS, KIND_SHELL_CWD, {"cwd": str(h.root / "sub" / "deeper")}),
    ]


async def test_a_fresh_context_rebuilds_the_directory_from_the_state(
    h: Harness, ws: Path
) -> None:
    await bash(h, "mkdir sub && cd sub")
    restarted = Harness(env=LocalExecEnv(str(ws)), state=dict(h.state))
    assert text_of(await bash(restarted, "pwd")).strip() == str(ws / "sub")


async def test_no_record_when_the_directory_did_not_move(h: Harness) -> None:
    await bash(h, "echo hi")
    assert h.records == []


async def test_output_and_exit_status_are_untouched_by_tracking(h: Harness) -> None:
    out = await bash(h, "printf hi; exit 3")
    assert text_of(out) == "Exit code 3\nhi"
    assert "NOETA" not in text_of(await bash(h, "echo out; echo err >&2"))
    # A command that sends its stdout to stderr still reports its directory.
    assert "NOETA" not in text_of(await bash(h, "mkdir s2; cd s2; exec 1>&2; echo moved"))
    assert h.fs.cwd == str(h.root / "s2")


async def test_leaving_the_workspace_resets_to_the_root(h: Harness) -> None:
    await bash(h, "mkdir sub && cd sub")
    out = await bash(h, "cd / && echo there")
    assert text_of(out) == f"there\nShell cwd was reset to {h.root}"
    assert text_of(await bash(h, "pwd")).strip() == str(h.root)
    assert h.fs.cwd == str(h.root)


async def test_leaving_with_no_output(h: Harness) -> None:
    out = await bash(h, "cd /")
    assert text_of(out) == f"Shell cwd was reset to {h.root}"


async def test_a_vanished_directory_starts_back_at_the_root(h: Harness) -> None:
    await bash(h, "mkdir gone && cd gone")
    os.rmdir(h.root / "gone")
    out = await bash(h, "pwd")
    assert text_of(out) == f"{h.root}\nShell cwd was reset to {h.root}"
    assert text_of(await bash(h, "pwd")).strip() == str(h.root)


async def test_a_recorded_directory_outside_the_workspace_is_given_up(
    h: Harness, tmp_path: Path
) -> None:
    h.state[FS] = FsState(cwd=str(tmp_path))
    out = await bash(h, "pwd")
    assert text_of(out) == f"{h.root}\nShell cwd was reset to {h.root}"


async def test_a_failing_command_keeps_the_new_directory(h: Harness) -> None:
    out = await bash(h, "mkdir sub && cd sub && exit 2")
    assert out.is_error and text_of(out) == "Exit code 2"
    assert h.fs.cwd == str(h.root / "sub")


# -- git hardening --------------------------------------------------------------


class _RecordingEnv(LocalExecEnv):
    def __init__(self, cwd: str) -> None:
        super().__init__(cwd)
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
        output_cap: int | None = None,
    ) -> RunResult:
        self.argvs.append(list(argv))
        self.envs.append(dict(env or {}))
        return RunResult(exit_code=0, stdout=b"")


async def test_allowlisted_git_runs_with_hardening_flags(ws: Path) -> None:
    rec = _RecordingEnv(str(ws))
    h = Harness(env=rec)
    for command in ("git status --short", "git diff --stat -- a", "git log -p"):
        assert not (await bash(h, command)).is_error
    assert rec.argvs == [
        ["git", "-c", "core.fsmonitor=", "status", "--short"],
        [
            "git", "-c", "core.fsmonitor=", "diff", "--no-ext-diff",
            "--no-textconv", "--stat", "--", "a",
        ],
        ["git", "log", "--no-ext-diff", "--no-textconv", "-p"],
    ]


async def test_non_git_and_unmatched_commands_run_through_bash(ws: Path) -> None:
    rec = _RecordingEnv(str(ws))
    h = Harness(env=rec)
    await bash(h, "ls -la")
    await bash(h, "git status | cat")
    assert [argv[:2] for argv in rec.argvs] == [["bash", "-c"], ["bash", "-c"]]
    assert rec.argvs[0][2].endswith("\nls -la")
    assert rec.argvs[1][2].endswith("\ngit status | cat")


async def test_hardening_can_be_switched_off(ws: Path) -> None:
    rec = _RecordingEnv(str(ws))
    h = Harness(env=rec)
    await bash(h, "git status", BashTool(hardening_rules=()))
    assert rec.argvs[0][:2] == ["bash", "-c"]


def _plain_git_diff(repo: Path) -> None:
    subprocess.run(["bash", "-c", "git diff"], cwd=repo, capture_output=True, check=False)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_repo_configured_git_helpers_do_not_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker_fsmonitor = tmp_path / "fsmonitor-ran"
    marker_ext = tmp_path / "extdiff-ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker_fsmonitor}\n")
    hook.chmod(0o755)
    ext = tmp_path / "ext.sh"
    ext.write_text(f"#!/bin/sh\ntouch {marker_ext}\n")
    ext.chmod(0o755)
    genv = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=genv, check=True, capture_output=True)

    git("init", "-q")
    (repo / "f.txt").write_text("a\n")
    git("add", "f.txt")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    (repo / "f.txt").write_text("b\n")
    git("config", "core.fsmonitor", str(hook))
    git("config", "diff.external", str(ext))

    h = Harness(env=LocalExecEnv(str(repo)))
    assert not (await bash(h, "git status")).is_error
    assert not (await bash(h, "git diff")).is_error
    assert not marker_fsmonitor.exists()
    assert not marker_ext.exists()
    # Sanity: the same repo DOES run them through plain bash.
    _plain_git_diff(repo)
    assert marker_ext.exists()


# -- the capture cap -------------------------------------------------------------


async def test_capture_cap_names_the_dropped_head(h: Harness) -> None:
    out = await bash(h, "python3 -c \"print('A'*4000)\"", BashTool(output_cap=1024))
    assert not out.is_error
    lines = text_of(out).splitlines()
    assert lines[0] == "[earlier output dropped: only the last 1 KB of stdout was captured]"
    # The tail is what survives (the directory marker the trap prints is
    # captured too, and stripped before display).
    assert set(lines[1]) == {"A"} and len(lines) == 2


async def test_capture_cap_names_both_streams_and_small_caps(h: Harness) -> None:
    out = await bash(
        h, "printf 'x%.0s' $(seq 1 50); printf 'y%.0s' $(seq 1 50) >&2", BashTool(output_cap=10)
    )
    assert text_of(out).splitlines()[0] == (
        "[earlier output dropped: only the last 10 bytes of stdout and stderr was captured]"
    )


async def test_capture_cap_silent_when_nothing_was_dropped(h: Harness) -> None:
    assert text_of(await bash(h, "echo hi")) == "hi"


# -- large output ------------------------------------------------------------------


async def test_large_output_is_spilled_with_a_preview(h: Harness, tmp_path: Path) -> None:
    h.spill_dir = str(tmp_path / "spill")
    out = await BashTool().run({"command": "seq 1 20000"}, h.ctx("c/1"))
    text = text_of(out)
    saved = tmp_path / "spill" / "task-1" / "c_1.txt"
    assert text.startswith("<persisted-output>\nOutput too large (")
    assert f"Full output saved to: {saved}\n\nPreview (first 2KB):\n1\n2\n" in text
    assert text.endswith("\n...\n</persisted-output>")
    assert saved.read_text() == "\n".join(str(i) for i in range(1, 20001))


async def test_large_output_without_a_spill_dir_keeps_head_and_tail(h: Harness) -> None:
    out = await bash(h, "seq 1 20000")
    text = text_of(out)
    assert len(text) <= 30_000
    assert text.startswith("<truncated-output>\nOutput too large (")
    assert "\n1\n2\n3\n" in text
    assert text.endswith("19999\n20000\n</truncated-output>")
    assert "characters dropped] ..." in text


async def test_a_large_failing_output_keeps_the_error_line_first(h: Harness) -> None:
    out = await bash(h, "seq 1 20000; exit 1")
    assert out.is_error
    assert text_of(out).startswith("Exit code 1\n<truncated-output>\n")


# -- per-call variables --------------------------------------------------------------

ECHO = f"{sys.executable} -c \"import os; print(os.environ.get('JOB', '<unset>'))\""


async def test_shell_env_reaches_the_command(h: Harness) -> None:
    asked: list[str] = []

    def resolver(ctx: ToolContext) -> Mapping[str, str]:
        asked.append(ctx.task_id)
        return {"JOB": f"job-of-{ctx.task_id}"}

    out = await bash(h, ECHO, BashTool(shell_env=resolver))
    assert text_of(out).strip() == "job-of-task-1"
    assert asked == ["task-1"]


async def test_a_broken_resolver_or_bad_entries_never_fail_the_command(h: Harness) -> None:
    def broken(ctx: ToolContext) -> Mapping[str, str]:
        raise RuntimeError("boom")

    out = await bash(h, ECHO, BashTool(shell_env=broken))
    assert not out.is_error and text_of(out).strip() == "<unset>"

    bad: Any = {"JOB=x; rm -rf": "a", "OK": 1, 3: "b", "NUL": "a\x00b"}
    probe = (
        f"{sys.executable} -c \"import os; "
        "print(sorted(k for k in os.environ if k in ('OK', 'NUL')))\""
    )
    out = await bash(h, probe, BashTool(shell_env=lambda ctx: bad))
    assert text_of(out).strip() == "[]"
    out = await bash(h, ECHO, BashTool(shell_env=lambda ctx: "not a mapping"))  # type: ignore[arg-type,return-value]
    assert text_of(out).strip() == "<unset>"


async def test_no_resolver_adds_nothing(ws: Path) -> None:
    rec = _RecordingEnv(str(ws))
    await bash(Harness(env=rec), "true")
    assert rec.envs == [{}]


# -- the tool's surface ----------------------------------------------------------------


def test_flags_and_schema() -> None:
    tool = BashTool()
    assert tool.name == "Bash"
    assert tool.read_only is False and tool.clearable is True
    assert tool.input_schema == {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number"},
            "description": {"type": "string"},
            "run_in_background": {"type": "boolean"},
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    assert tool.input_schema is not BashTool().input_schema
