"""``LocalExecEnv``: real subprocesses and files in a temp directory.

The process tests are POSIX-only, matching the codebase's process handling.
"""

from __future__ import annotations

import asyncio
import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs import local as local_mod
from noeta.backends.execenvs.local import LocalExecEnv, send_group_signal
from noeta.core.execenv import ExecEnv, FileStat, RunResult
from noeta.toolkit.local_env import OWN_SECRETS, child_env


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while _alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return not _alive(pid)


async def _wait_file(path: Path, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


@pytest.fixture
def env(tmp_path: Path) -> LocalExecEnv:
    return LocalExecEnv(str(tmp_path))


def test_it_is_an_exec_env(env: LocalExecEnv) -> None:
    as_protocol: ExecEnv = env
    assert as_protocol.cwd == env.cwd


# -- run -----------------------------------------------------------------------


async def test_happy_path(env: LocalExecEnv) -> None:
    result = await env.run(["/bin/sh", "-c", "echo out; echo err 1>&2"], timeout=10)
    assert isinstance(result, RunResult)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.stdout == b"out\n"
    assert result.stderr == b"err\n"
    assert not result.stdout_truncated and not result.stderr_truncated


async def test_exit_code_and_cwd(env: LocalExecEnv, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    result = await env.run(["/bin/sh", "-c", "pwd; exit 3"])
    assert result.exit_code == 3
    assert result.stdout.decode().strip() == os.path.realpath(tmp_path)
    sub = await env.run(["pwd"], cwd="sub")
    assert sub.stdout.decode().strip() == os.path.realpath(tmp_path / "sub")


async def test_stdin_is_fed_or_closed(env: LocalExecEnv) -> None:
    fed = await env.run(["cat"], stdin=b"hello")
    assert fed.stdout == b"hello"
    # No stdin: the child reads EOF at once instead of blocking on the host's.
    closed = await env.run(["cat"], timeout=5)
    assert closed.stdout == b"" and not closed.timed_out


async def test_missing_program_raises(env: LocalExecEnv) -> None:
    with pytest.raises(FileNotFoundError):
        await env.run(["definitely-not-a-program-xyz"])
    with pytest.raises(ValueError):
        await env.run([])


async def test_timeout_reaps_backgrounded_grandchild(
    env: LocalExecEnv, tmp_path: Path
) -> None:
    """A grandchild backgrounded by the direct child must not survive the
    timeout kill — the reason for the group escalation."""
    pid_file = tmp_path / "grandchild.pid"
    script = f"sleep 300 & echo $! > {pid_file}; wait"
    start = time.monotonic()
    result = await env.run(["/bin/sh", "-c", script], timeout=1)
    assert result.timed_out is True
    assert result.exit_code == -1
    grandchild = int(pid_file.read_text().strip())
    assert await _wait_dead(grandchild), f"grandchild {grandchild} survived"
    # SIGTERM killed the sh promptly — the 5 s SIGKILL grace was not burned.
    assert time.monotonic() - start < 8.0


async def test_timeout_kills_the_group_after_its_leader_exited(
    env: LocalExecEnv, tmp_path: Path
) -> None:
    """The direct child exited (and was reaped) while a grandchild keeps
    its output open: the timeout still kills the grandchild."""
    pid_file = tmp_path / "grandchild.pid"
    script = f"sleep 77 & echo $! > {pid_file}; exit 0"
    result = await env.run(["/bin/sh", "-c", script], timeout=2)
    assert result.timed_out is True
    grandchild = int(pid_file.read_text().strip())
    assert await _wait_dead(grandchild), f"grandchild {grandchild} survived"


async def test_timeout_keeps_partial_output(env: LocalExecEnv) -> None:
    result = await env.run(["bash", "-c", "echo hi; sleep 30"], timeout=1, output_cap=1024)
    assert result.timed_out is True
    assert result.exit_code == -1
    assert result.stdout == b"hi\n"


async def test_sigterm_ignoring_child_is_killed_after_the_grace(tmp_path: Path) -> None:
    env = LocalExecEnv(str(tmp_path), kill_grace=0.3)
    start = time.monotonic()
    result = await env.run(["bash", "-c", "trap '' TERM; sleep 30"], timeout=0.5)
    assert result.timed_out
    assert time.monotonic() - start < 5.0


async def test_cancellation_kills_the_group(env: LocalExecEnv, tmp_path: Path) -> None:
    pid_file = tmp_path / "pids"
    script = f"sleep 300 & echo $$ $! > {pid_file}; wait"
    task = asyncio.ensure_future(env.run(["bash", "-c", script]))
    pids = [int(p) for p in (await _wait_file(pid_file)).split()]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for pid in pids:
        assert await _wait_dead(pid), f"{pid} survived cancellation"


@pytest.mark.parametrize("steps", range(8))
def test_a_loop_shutting_down_mid_spawn_still_closes(tmp_path: Path, steps: int) -> None:
    """The loop shuts down (every task cancelled at once) ``steps`` loop
    turns into a run, while its child may still be being spawned: closing
    the loop ends, and the child's group is killed."""
    env = LocalExecEnv(str(tmp_path), kill_grace=0.5)
    pid_file = tmp_path / "pid"
    script = f"echo $$ > {pid_file}; sleep 300"
    runs: list[asyncio.Future[Any]] = []

    async def main() -> None:
        runs.append(asyncio.ensure_future(env.run(["bash", "-c", script])))
        for _ in range(steps):
            await asyncio.sleep(0)

    closed = threading.Event()

    def run_loop() -> None:
        asyncio.run(main())
        closed.set()

    threading.Thread(target=run_loop, daemon=True).start()
    assert closed.wait(20), "the loop never finished closing"
    if pid_file.exists() and pid_file.read_text().strip():
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid), f"{pid} survived the loop's shutdown"


async def test_a_second_cancel_still_finishes_the_kill(tmp_path: Path) -> None:
    env = LocalExecEnv(str(tmp_path), kill_grace=0.5)
    pid_file = tmp_path / "pid"
    script = f"trap '' TERM; echo $$ > {pid_file}; sleep 300"
    task = asyncio.ensure_future(env.run(["bash", "-c", script]))
    pid = int(await _wait_file(pid_file))
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The kill keeps going in the background and reaches SIGKILL.
    assert await _wait_dead(pid)


async def test_output_cap_keeps_a_bounded_tail(env: LocalExecEnv) -> None:
    result = await env.run(
        ["bash", "-c", "head -c 5000000 /dev/zero; printf END"], timeout=30, output_cap=100
    )
    assert len(result.stdout) == 100
    assert result.stdout.endswith(b"END")
    assert result.stdout_truncated is True


async def test_output_cap_renders_the_exact_tail(env: LocalExecEnv) -> None:
    result = await env.run(
        ["bash", "-c", "seq 1 100000; echo err >&2"], timeout=30, output_cap=64
    )
    full = "".join(f"{i}\n" for i in range(1, 100001)).encode()
    assert result.stdout == full[-64:]
    assert result.stdout_truncated is True
    assert result.stderr == b"err\n"
    assert result.stderr_truncated is False


async def test_exactly_at_the_cap_is_not_truncated(env: LocalExecEnv) -> None:
    result = await env.run(["printf", "abcd"], output_cap=4)
    assert result.stdout == b"abcd" and not result.stdout_truncated


async def test_commands_inherit_the_environment_but_noetas_credentials(
    env: LocalExecEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As in Claude Code: a toolchain's variables reach the command; only
    # noeta's own credentials stay behind.
    monkeypatch.setenv("CARGO_HOME", "/opt/cargo")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("NOETA_STORE", "postgresql://noeta:pw@db/noeta")
    monkeypatch.setenv("NOETA_API_KEY", "sk-noeta")
    monkeypatch.setenv("JOB", "from-host")
    code = ("import os; print(*(os.environ.get(k, '-') for k in "
            "('CARGO_HOME', 'GITHUB_TOKEN', 'NOETA_STORE', 'NOETA_API_KEY', 'JOB')))")
    out = await env.run([sys.executable, "-c", code], env={"JOB": "from-call"})
    assert out.stdout.decode().split() == ["/opt/cargo", "gh", "-", "-", "from-call"]


def test_child_env_drops_only_noetas_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in OWN_SECRETS:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "passed, as Claude Code passes it")
    child = child_env()
    assert not OWN_SECRETS & set(child)
    assert {k: v for k, v in os.environ.items() if k not in OWN_SECRETS} == child
    assert child["ANTHROPIC_API_KEY"].startswith("passed")


def test_libpq_credentials_are_withheld_once_noetas_store_is_postgres(
        monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("psycopg")
    from noeta.backends.stores.postgres import PostgresStore
    from noeta.toolkit import local_env

    monkeypatch.setattr(local_env, "_withheld", set())
    monkeypatch.setenv("PGPASSWORD", "pw")
    monkeypatch.setenv("PGHOST", "db")
    assert child_env()["PGPASSWORD"] == "pw"  # the person's own psql, no noeta store on it
    PostgresStore("postgresql://noeta@db/noeta")
    env = child_env()
    assert "PGPASSWORD" not in env and env["PGHOST"] == "db"


# -- send_group_signal ---------------------------------------------------------


def _spy_os(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pgid: Any,
    killpg_exc: BaseException | None = None,
    kill_exc: BaseException | None = None,
) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"killpg": [], "kill": []}

    def _getpgid(pid: int) -> int:
        if isinstance(pgid, BaseException):
            raise pgid
        return int(pgid)

    def _killpg(pg: int, sig: int) -> None:
        calls["killpg"].append((pg, sig))
        if killpg_exc is not None:
            raise killpg_exc

    def _kill(pid: int, sig: int) -> None:
        calls["kill"].append((pid, sig))
        if kill_exc is not None:
            raise kill_exc

    monkeypatch.setattr(os, "getpgid", _getpgid)
    monkeypatch.setattr(os, "killpg", _killpg)
    monkeypatch.setattr(os, "kill", _kill)
    return calls


def test_group_send_success_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_os(monkeypatch, pgid=4242)
    send_group_signal(4242, signal.SIGTERM)
    assert calls == {"killpg": [(4242, signal.SIGTERM)], "kill": []}


def test_group_send_failure_falls_back_to_single_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy_os(monkeypatch, pgid=4242, killpg_exc=ProcessLookupError())
    send_group_signal(99, signal.SIGKILL)
    assert calls == {"killpg": [(4242, signal.SIGKILL)], "kill": [(99, signal.SIGKILL)]}


def test_getpgid_failure_falls_back_to_single_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_os(monkeypatch, pgid=ProcessLookupError())
    send_group_signal(99, signal.SIGTERM)
    assert calls == {"killpg": [], "kill": [(99, signal.SIGTERM)]}


def test_require_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_os(monkeypatch, pgid=7000)  # not the leader
    send_group_signal(99, signal.SIGKILL, require_leader=True)
    assert calls == {"killpg": [], "kill": [(99, signal.SIGKILL)]}
    calls = _spy_os(monkeypatch, pgid=99)  # the leader
    send_group_signal(99, signal.SIGKILL, require_leader=True)
    assert calls == {"killpg": [(99, signal.SIGKILL)], "kill": []}


def test_all_races_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_os(
        monkeypatch, pgid=4242, killpg_exc=PermissionError(), kill_exc=ProcessLookupError()
    )
    send_group_signal(99, signal.SIGTERM)  # must not raise
    assert calls == {"killpg": [(4242, signal.SIGTERM)], "kill": [(99, signal.SIGTERM)]}


# -- files ---------------------------------------------------------------------


async def test_read_write_roundtrip_relative_and_absolute(
    env: LocalExecEnv, tmp_path: Path
) -> None:
    await env.write_bytes("a/b/c.txt", b"hello")  # parents are created
    assert (tmp_path / "a" / "b" / "c.txt").read_bytes() == b"hello"
    assert await env.read_bytes(str(tmp_path / "a/b/c.txt")) == b"hello"
    assert await env.read_range("a/b/c.txt", 1, 3) == b"ell"
    assert await env.read_range("a/b/c.txt", 3, 100) == b"lo"
    assert await env.read_range("a/b/c.txt", 50, 10) == b""
    with pytest.raises(FileNotFoundError):
        await env.read_bytes("missing.txt")


async def test_write_is_atomic_and_keeps_permissions(
    env: LocalExecEnv, tmp_path: Path
) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    await env.write_bytes("run.sh", b"#!/bin/sh\necho hi\n")
    assert script.read_bytes() == b"#!/bin/sh\necho hi\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    # No temp file is left behind.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run.sh"]


async def test_write_through_a_symlink_writes_its_target(
    env: LocalExecEnv, tmp_path: Path
) -> None:
    target = tmp_path / "real.txt"
    target.write_text("old")
    (tmp_path / "link.txt").symlink_to(target)
    await env.write_bytes("link.txt", b"new")
    assert (tmp_path / "link.txt").is_symlink()
    assert target.read_text() == "new"


async def test_new_file_gets_default_permissions(env: LocalExecEnv, tmp_path: Path) -> None:
    await env.write_bytes("new.txt", b"x")
    mode = stat.S_IMODE((tmp_path / "new.txt").stat().st_mode)
    assert mode == 0o666 & ~local_mod._UMASK


async def test_stat(env: LocalExecEnv, tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"12345")
    (tmp_path / "d").mkdir()
    (tmp_path / "l").symlink_to(tmp_path / "f")
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    f = await env.stat("f")
    assert isinstance(f, FileStat) and f.size == 5 and not f.is_dir and not f.is_symlink
    d = await env.stat("d")
    assert d is not None and d.is_dir
    link = await env.stat("l")
    assert link is not None and link.is_symlink and link.size == 5
    dangling = await env.stat("dangling")
    assert dangling is not None and dangling.is_symlink
    assert await env.stat("missing") is None
    assert await env.stat("f/under-a-file") is None


async def test_realpath(env: LocalExecEnv, tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "l").symlink_to(tmp_path / "d")
    real = os.path.realpath(tmp_path)
    assert await env.realpath("l/x/../y") == os.path.join(real, "d", "y")
    assert await env.realpath(".") == real
    assert await env.realpath("/") == "/"
