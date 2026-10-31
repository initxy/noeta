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

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.execenv import RunResult
from noeta.core.tool import ToolContext
from noeta.plugins.shell.bash import BashTool, resolve_timeout
from noeta.plugins.shell.git_prelude import git_prelude
from noeta.plugins.shell.jobs import Registry
from noeta.plugins.shell.state import SHELL, ShellState, reduce_shell
from noeta.plugins.shell.tools import BackgroundBash
from tests.plugins.conftest import Harness, text_of


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


@pytest.mark.parametrize("command", [
    "grep nothing-here marker.txt", "cd . && rg nothing-here marker.txt",
    "find . -name marker.txt -exec false {} +", "diff <(echo a) <(echo b)",
    "test -f absent", "[ -f absent ]",
    "git -C . diff --quiet --no-index marker.txt /dev/null",
    "egrep nothing-here marker.txt", "fgrep nothing-here marker.txt",
])
async def test_exit_one_of_a_search_or_compare_is_not_an_error(
        h: Harness, ws: Path, command: str) -> None:
    (ws / "marker.txt").write_text("x\n")
    out = await bash(h, command)
    assert out.meta["exit_code"] == 1
    assert not out.is_error
    assert "Exit code" not in text_of(out)


async def test_exit_two_of_grep_and_one_elsewhere_stay_errors(h: Harness) -> None:
    out = await bash(h, "grep x /no/such/file")
    assert out.is_error and text_of(out).startswith("Exit code 2\n")
    piped = await bash(h, "grep nothing-here /dev/null | cat; false")
    assert piped.is_error and text_of(piped) == "Exit code 1"


_PNG_1X1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9"
            "awAAAABJRU5ErkJggg==")


async def test_an_image_data_uri_comes_back_as_an_image(h: Harness) -> None:
    from noeta.core.types import Image
    out = await bash(h, f"echo; echo 'data:image/jpeg;base64,{_PNG_1X1}'")
    assert not out.is_error
    [block] = out.content
    assert isinstance(block, Image) and block.media_type == "image/png"
    assert block.data.startswith(b"\x89PNG")


async def test_a_printed_image_is_decoded_off_the_event_loop(
        h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from noeta.plugins.shell import output

    seen: list[bool] = []
    real = output.fit_image

    def fit(raw: bytes, media_type: str) -> Any:
        seen.append(threading.current_thread() is threading.main_thread())
        return real(raw, media_type)

    monkeypatch.setattr(output, "fit_image", fit)
    await bash(h, f"echo 'data:image/png;base64,{_PNG_1X1}'")
    assert seen == [False]


async def test_a_broken_or_failing_image_uri_stays_text(h: Harness) -> None:
    out = await bash(h, "echo 'data:image/png;base64,@@@'")
    assert text_of(out) == "data:image/png;base64,@@@"
    failed = await bash(h, f"echo 'data:image/png;base64,{_PNG_1X1}'; exit 3")
    assert failed.is_error and text_of(failed).startswith("Exit code 3\ndata:image/png")


async def test_stdout_and_stderr_merge_in_print_order(h: Harness) -> None:
    both = await bash(h, "sh -c 'echo err 1>&2; echo out; echo err2 1>&2'")
    assert text_of(both) == "err\nout\nerr2"
    only_err = await bash(h, "sh -c 'echo lone-err 1>&2'")
    assert text_of(only_err) == "lone-err"


async def test_empty_output_is_marked(h: Harness) -> None:
    assert text_of(await bash(h, "true")) == "(Bash completed with no output)"


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
    tool = BackgroundBash(Registry(log_dir=str(ws / "logs")))
    out = await bash(h, f"sleep 110 && touch {marker}", tool)
    assert out.is_error
    assert text_of(out) == (
        f"Bash: Blocked: sleep 110 followed by: touch {marker}. To wait until "
        "something is true, run an until-loop under Monitor (`until <check>; do "
        "sleep 2; done`); to wait for a command of yours, start it with "
        "run_in_background: true. Do not get around this with a chain of shorter "
        "sleeps."
    )
    assert not marker.exists()


async def test_without_background_jobs_a_long_sleep_just_runs(h: Harness, ws: Path) -> None:
    # Its refusal points at Monitor and run_in_background, which are not there.
    marker = ws / "ran"
    out = await bash(h, f"sleep 110 && touch {marker}", timeout=1000)
    assert text_of(out) == "Command timed out after 1s"
    assert not marker.exists()


async def test_a_short_sleep_still_runs(h: Harness) -> None:
    assert text_of(await bash(h, "sleep 0.1 && echo ok")) == "ok"


# -- the working directory persists --------------------------------------------


async def test_the_directory_persists_between_commands(h: Harness) -> None:
    first = await bash(h, "mkdir -p sub/deeper && cd sub")
    assert not first.is_error and text_of(first) == "(Bash completed with no output)"
    assert text_of(await bash(h, "pwd")).strip() == str(h.root / "sub")
    await bash(h, "cd deeper")
    assert text_of(await bash(h, "pwd")).strip() == str(h.root / "sub" / "deeper")
    assert [r for r in h.records if r[1] == "cwd"] == [
        (SHELL, "cwd", {"cwd": str(h.root / "sub")}),
        (SHELL, "cwd", {"cwd": str(h.root / "sub" / "deeper")}),
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
    assert h.shell.cwd == str(h.root / "s2")


async def test_leaving_the_workspace_resets_to_the_root(h: Harness) -> None:
    await bash(h, "mkdir sub && cd sub")
    out = await bash(h, "cd / && echo there")
    assert text_of(out) == f"there\nShell cwd was reset to {h.root}"
    assert text_of(await bash(h, "pwd")).strip() == str(h.root)
    assert h.shell.cwd == str(h.root)


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
    h.state[SHELL] = ShellState(cwd=str(tmp_path))
    out = await bash(h, "pwd")
    assert text_of(out) == f"{h.root}\nShell cwd was reset to {h.root}"


async def test_a_directory_swapped_for_a_link_out_starts_back_at_the_root(
    h: Harness, tmp_path: Path
) -> None:
    await bash(h, "mkdir sub && cd sub")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.rmdir(h.root / "sub")
    os.symlink(elsewhere, h.root / "sub")
    out = await bash(h, "pwd")
    assert text_of(out) == f"{h.root}\nShell cwd was reset to {h.root}"


async def test_a_failing_command_does_not_move_the_shell(h: Harness) -> None:
    out = await bash(h, "mkdir sub && cd sub && exit 2")
    assert out.is_error and text_of(out) == "Exit code 2"
    assert h.shell.cwd is None


async def test_a_sub_agent_shell_never_moves(h: Harness) -> None:
    ctx = h.ctx("c/1")
    ctx.root_id = "root"
    out = await BashTool().run({"command": "mkdir sub && cd sub"}, ctx)
    assert not out.is_error and "cwd" not in out.meta and h.records == []


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


async def test_reading_git_runs_under_the_hardening_function(ws: Path) -> None:
    rec = _RecordingEnv(str(ws))
    h = Harness(env=rec)
    commands = ("git status --short", "git -C sub log -p", "cd sub && git diff HEAD~ HEAD")
    for command in commands:
        assert not (await bash(h, command)).is_error
    for argv, command in zip(rec.argvs, commands, strict=True):
        assert argv[:2] == ["bash", "-c"]
        assert argv[2].endswith(git_prelude(command, str(ws)) + command)
        assert "git() {" in argv[2] and "-c safe.bareRepository=explicit" in argv[2]


def test_only_a_command_that_reads_with_git_gets_the_function() -> None:
    assert git_prelude("git log -p && git -C x --no-pager show")
    assert not git_prelude("git commit -m x")
    assert not git_prelude("git log && git push")
    assert not git_prelude("ls -la")
    assert not git_prelude("git log $(id)")


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


def _planted_clone(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """A clone of a repository that carries a bare repository ``sub``
    (``HEAD``, ``config``, ``objects``, ``refs`` committed as plain files)
    whose config names a program for every helper ``git`` reads it for."""
    genv = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}

    def git(cwd: Path, *args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                       env=genv, check=True, capture_output=True)

    markers = {k: tmp_path / f"{k}-ran" for k in ("ext", "fsmonitor", "textconv", "gpg",
                                                     "filter")}
    scripts = {}
    for k, marker in markers.items():
        script = tmp_path / f"{k}.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
        script.chmod(0o755)
        scripts[k] = script
    build = tmp_path / "build"
    git(tmp_path, "init", "-q", "--bare", str(build / "sub"))
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", str(seed))
    for n in ("1", "2"):
        (seed / "f.txt").write_text(f"{n}\n")
        git(seed, "add", "f.txt")
        git(seed, "commit", "-qm", n)
    # A signature on the tip, so showing it runs the configured gpg program.
    raw = subprocess.run(["git", "cat-file", "commit", "HEAD"], cwd=seed, env=genv, check=True,
                         capture_output=True, text=True).stdout
    head, _, message = raw.partition("\n\n")
    signed = (f"{head}\ngpgsig -----BEGIN PGP SIGNATURE-----\n x\n -----END PGP SIGNATURE-----"
              f"\n\n{message}")
    tip = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=seed,
                         env=genv, input=signed, check=True, capture_output=True,
                         text=True).stdout.strip()
    git(seed, "push", "-q", str(build / "sub"), f"{tip}:refs/heads/master")
    sub = build / "sub"
    for key, value in (("diff.external", scripts["ext"]), ("core.fsmonitor", scripts["fsmonitor"]),
                       ("diff.evil.textconv", scripts["textconv"]),
                       ("log.showSignature", "true"), ("gpg.program", scripts["gpg"]),
                       ("filter.evil.clean", scripts["filter"])):
        git(sub, "config", key, str(value))
    (sub / "info" / "attributes").write_text("* diff=evil filter=evil\n")
    git(build, "init", "-q")
    git(build, "add", "-f", "sub")
    git(build, "commit", "-qm", "planted")
    ws = tmp_path / "ws"
    git(tmp_path, "clone", "-q", str(build), str(ws))
    return ws, markers


PLANTED = ["git -C sub diff HEAD~ HEAD", "cd sub && git diff HEAD~ HEAD", "git -C sub log -p",
           "git -C sub show", "git -C sub log --format=%GG", "git -C sub blame f.txt HEAD",
           "cd sub && git log -p", "git -C sub reflog -p", "git -C sub status"]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("command", PLANTED)
async def test_read_only_git_never_runs_an_embedded_repositorys_helpers(
        tmp_path: Path, command: str) -> None:
    from noeta.plugins.permissions.scope import Scope, read_only

    ws, markers = _planted_clone(tmp_path)
    if command != "git -C sub status":  # a bare repository has no status: it fails anyway
        assert read_only(command, Scope(dirs=(str(ws),), home=str(tmp_path)))
    h = Harness(env=LocalExecEnv(str(ws)))
    await bash(h, command)
    assert [k for k, m in markers.items() if m.exists()] == []


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_the_planted_repository_does_run_helpers_through_plain_git(tmp_path: Path) -> None:
    ws, markers = _planted_clone(tmp_path)
    subprocess.run(["git", "-C", "sub", "diff", "HEAD~", "HEAD"], cwd=ws, capture_output=True,
                   check=False)
    assert markers["ext"].exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_the_shells_directory_inside_an_embedded_repository_is_hardened_too(
        tmp_path: Path) -> None:
    ws, markers = _planted_clone(tmp_path)
    h = Harness(env=LocalExecEnv(str(ws)))
    assert not (await bash(h, "cd sub")).is_error
    await bash(h, "git diff HEAD~ HEAD")
    await bash(h, "git log -p")
    assert [k for k, m in markers.items() if m.exists()] == []


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_hardened_git_still_reads_the_workspace_repository(tmp_path: Path) -> None:
    ws, _markers = _planted_clone(tmp_path)
    h = Harness(env=LocalExecEnv(str(ws)))
    for command in ("git log --oneline", "git status --short", "git show --stat",
                    "git diff HEAD", "git -C . log -p -1", "cd sub && cd .. && git log -1",
                    "git stash list", "git reflog -1", "git blame sub/HEAD"):
        out = await bash(h, command)
        assert not out.is_error, (command, text_of(out))
    assert "planted" in text_of(await bash(h, "git log --oneline"))


def _git(cwd: Path, *args: str) -> None:
    genv = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd), "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   env=genv, check=True, capture_output=True)


def _seeded(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", str(seed))
    (seed / "f.txt").write_text("1\n")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-qm", "seeded")
    return seed


_READS = ("git status --short", "git log --oneline", "git diff HEAD", "git branch",
          "git stash list")


async def _reads_fine(root: Path, cwd: str = ".") -> None:
    h = Harness(env=LocalExecEnv(os.path.realpath(root)))
    if cwd != ".":
        assert not (await bash(h, f"cd {cwd}")).is_error
    for command in _READS:
        out = await bash(h, command)
        assert not out.is_error, (command, text_of(out))
    assert "seeded" in text_of(await bash(h, "git log --oneline"))


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_hardened_git_reads_a_worktree_of_a_bare_repository(tmp_path: Path) -> None:
    seed = _seeded(tmp_path)
    box = tmp_path / "box"
    box.mkdir()
    _git(box, "clone", "-q", "--bare", str(seed), "r.git")
    _git(box / "r.git", "worktree", "add", "-q", "../wt")
    # The git directory outside the workspace, then inside it.
    await _reads_fine(box / "wt")
    await _reads_fine(box, "wt")
    # The ``.bare`` layout: a ``.git`` file at the top names a bare repository.
    dot = tmp_path / "dot"
    dot.mkdir()
    _git(dot, "clone", "-q", "--bare", str(seed), ".bare")
    (dot / ".git").write_text("gitdir: ./.bare\n")
    _git(dot, "worktree", "add", "-q", "main")
    await _reads_fine(dot, "main")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_hardened_git_reads_a_separate_git_directory(tmp_path: Path) -> None:
    seed = _seeded(tmp_path)
    _git(tmp_path, "clone", "-q", "--separate-git-dir", str(tmp_path / "gd"), str(seed), "ws")
    await _reads_fine(tmp_path / "ws")
    box = tmp_path / "box"
    box.mkdir()
    _git(box, "clone", "-q", "--separate-git-dir", str(box / "gd"), str(seed), "ws")
    await _reads_fine(box, "ws")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_a_planted_bare_repository_is_refused_unless_a_person_approved(
        tmp_path: Path) -> None:
    ws, markers = _planted_clone(tmp_path)
    h = Harness(env=LocalExecEnv(str(ws)))
    out = await bash(h, "git -C sub log --oneline")
    assert out.is_error and "not a .git directory" in text_of(out)
    # A person approved the command: it runs, still hardened.
    ctx = h.ctx()
    ctx.approved = True
    out = await BashTool().run({"command": "git -C sub diff HEAD~ HEAD"}, ctx)
    assert not out.is_error, text_of(out)
    assert "+2" in text_of(out)
    assert [k for k, m in markers.items() if m.exists()] == []


def _planted_with_a_work_tree(where: Path, marker: Path, *, bare: bool = False) -> Path:
    """A git directory ``where/evil`` (not ``.git``) whose config gives it
    ``where`` as its work tree and a filter that touches ``marker``, with
    ``f.txt`` in its index and changed since: what a model could write
    into a directory it may write without asking."""
    (where / "f.txt").write_text("x\n")
    _git(where, "init", "-q", "--separate-git-dir", str(where / "evil"), str(where))
    _git(where, "add", "f.txt")
    (where / ".git").unlink()
    config = (where / "evil" / "config").read_text()
    if bare:
        config = config.replace("bare = false", "bare = true")
    (where / "evil" / "config").write_text(
        config + f'\tworktree = ..\n[filter "x"]\n\tclean = "touch {marker}; cat"\n')
    (where / "evil" / "info" / "attributes").write_text("* filter=x\n")
    (where / "f.txt").write_text("y\n")
    return where / "evil"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_a_planted_repository_outside_the_workspace_is_refused_when_it_has_a_work_tree(
        tmp_path: Path) -> None:
    from noeta.plugins.permissions.scope import Scope, read_only

    ws, extra = tmp_path / "ws", tmp_path / "extra"
    ws.mkdir()
    extra.mkdir()
    marker = tmp_path / "filter-ran"
    evil = _planted_with_a_work_tree(extra, marker)
    h = Harness(env=LocalExecEnv(str(ws)))
    for command in (f"git -C {evil} status", f"git -C {evil} diff", f"cd {evil} && git status",
                    f"git -C {evil} ls-files -m"):
        # An additional working directory: read-only, so allowed without asking.
        assert read_only(command, Scope(dirs=(str(ws), str(extra)), home=str(tmp_path)))
        out = await bash(h, command)
        assert out.is_error and "not a .git directory" in text_of(out), command
    assert not marker.exists()
    # A bare repository outside the workspace (no work tree, so no filter)
    # still reads.
    other = tmp_path / "other"
    other.mkdir()
    bare = _planted_with_a_work_tree(other, marker, bare=True)
    out = await bash(h, f"git -C {bare} log --oneline")
    assert "not a .git directory" not in text_of(out)
    assert not marker.exists()


# -- the capture cap -------------------------------------------------------------


async def test_capture_cap_names_the_dropped_head(h: Harness) -> None:
    out = await bash(h, "python3 -c \"print('A'*4000)\"", BashTool(output_cap=1024))
    assert not out.is_error
    lines = text_of(out).splitlines()
    assert lines[0] == "[earlier output dropped: only the last 1 KB was captured]"
    # The tail is what survives (the directory marker the trap prints is
    # captured too, and stripped before display).
    assert set(lines[1]) == {"A"} and len(lines) == 2


async def test_capture_cap_with_a_small_cap(h: Harness) -> None:
    out = await bash(
        h, "printf 'x%.0s' $(seq 1 50); printf 'y%.0s' $(seq 1 50) >&2", BashTool(output_cap=10)
    )
    assert text_of(out).splitlines()[0] == (
        "[earlier output dropped: only the last 10 bytes was captured]"
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


async def test_every_command_gets_git_editor_true_and_a_host_may_override_it(
        ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Claude Code sets GIT_EDITOR=true over the inherited value (the person's
    # own editor included); only per-call variables override it.
    monkeypatch.setenv("GIT_EDITOR", "vim")
    rec = _RecordingEnv(str(ws))
    await bash(Harness(env=rec), "true")
    assert rec.envs == [{"GIT_EDITOR": "true"}]
    await bash(Harness(env=rec), "true", BashTool(shell_env=lambda ctx: {"GIT_EDITOR": "ed"}))
    assert rec.envs[-1] == {"GIT_EDITOR": "ed"}


# -- the tool's surface ----------------------------------------------------------------


def test_flags_and_schema() -> None:
    tool = BashTool()
    assert tool.name == "Bash"
    assert tool.read_only is False and tool.clearable is True
    schema = tool.input_schema
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["required"] == ["command"] and schema["additionalProperties"] is False
    assert {k: v["type"] for k, v in schema["properties"].items()} == {
        "command": "string", "timeout": "number", "description": "string",
        "run_in_background": "boolean", "dangerouslyDisableSandbox": "boolean"}
    assert all(v["description"] for v in schema["properties"].values())
    assert tool.input_schema is not BashTool().input_schema


def test_shell_state_fold() -> None:
    state = reduce_shell(None, "cwd", {"cwd": "/ws/sub"})
    assert state.cwd == "/ws/sub"
    assert reduce_shell(state, "cwd", {"cwd": None}).cwd is None
    assert reduce_shell(state, "cwd", {"cwd": ""}).cwd is None
    assert reduce_shell(state, "cwd", {"cwd": 3}) is state
    assert reduce_shell(state, "cwd", ["/x"]) is state
    assert reduce_shell(state, "unknown", {}) is state
