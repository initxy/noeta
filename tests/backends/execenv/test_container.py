"""``ContainerExecEnv`` against a real local Docker container.

Skipped when ``docker`` is unavailable or no suitable image is present. The
image comes from ``NOETA_TEST_CONTAINER_IMAGE``, else the first local one of
``python:3.12-slim``, ``debian:bookworm-slim``, ``postgres:16`` (any image
with sh, GNU coreutils, util-linux ``setsid`` and bash works). Grep/Glob run
when the host's ``rg`` can be copied in.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from noeta.backends.execenv.container import (
    ContainerError,
    ContainerExecEnv,
    container_state,
    remove_container,
    start_container,
    stop_container,
)
from noeta.core.execenv import ExecEnv
from noeta.core.tool import ToolContext
from noeta.core.types import Text
from noeta.plugins.fs import EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from noeta.plugins.fs.state import FS, reduce_fs
from noeta.plugins.shell import BashTool

CANDIDATES = ("python:3.12-slim", "debian:bookworm-slim", "postgres:16")


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120,
                          check=check)


def _image() -> str | None:
    if shutil.which("docker") is None:
        return None
    try:
        out = _docker("images", "--format", "{{.Repository}}:{{.Tag}}", check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    local = set(out.stdout.split())
    wanted = os.environ.get("NOETA_TEST_CONTAINER_IMAGE")
    if wanted:
        return wanted
    return next((c for c in CANDIDATES if c in local), None)


IMAGE = _image()
pytestmark = pytest.mark.skipif(IMAGE is None, reason="docker or a suitable image is missing")


@pytest.fixture(scope="module")
def container() -> Iterator[str]:
    assert IMAGE is not None
    name = f"noeta-test-{uuid.uuid4().hex[:10]}"
    _docker("run", "-d", "--init", "--name", name, "-w", "/workspace", IMAGE,
            "sleep", "infinity")
    try:
        _docker("exec", name, "mkdir", "-p", "/workspace")
        yield name
    finally:
        _docker("rm", "-f", name, check=False)


@pytest.fixture
def env(container: str) -> ContainerExecEnv:
    workdir = f"/workspace/{uuid.uuid4().hex[:8]}"
    _docker("exec", container, "mkdir", "-p", workdir)
    return ContainerExecEnv(container, cwd=workdir, kill_grace=1.0)


async def _exists(env: ContainerExecEnv, path: str) -> bool:
    return await env.stat(path) is not None


def test_it_is_an_exec_env(env: ContainerExecEnv) -> None:
    as_protocol: ExecEnv = env
    assert as_protocol.cwd.startswith("/workspace/")


# ---------------------------------------------------------------- processes


async def test_run_basics(env: ContainerExecEnv) -> None:
    r = await env.run(["sh", "-c", "pwd; echo err >&2; exit 3"])
    assert (r.exit_code, r.stdout.decode().strip(), r.stderr) == (3, env.cwd, b"err\n")
    r = await env.run(["cat"], stdin=b"\x00\x01binary")
    assert r.stdout == b"\x00\x01binary"
    r = await env.run(["sh", "-c", 'echo "$FOO"'], env={"FOO": "bar"})
    assert r.stdout == b"bar\n"
    await env.run(["mkdir", "-p", "sub"])
    r = await env.run(["pwd"], cwd="sub")
    assert r.stdout.decode().strip() == f"{env.cwd}/sub"


async def test_host_environment_never_reaches_the_container(
        env: ContainerExecEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOETA_HOST_SECRET", "leak")
    r = await env.run(["env"])
    assert b"NOETA_HOST_SECRET" not in r.stdout


async def test_output_cap_keeps_the_tail(env: ContainerExecEnv) -> None:
    r = await env.run(["sh", "-c", "seq 1 20000"], output_cap=100)
    assert r.stdout_truncated and r.stdout.endswith(b"20000\n") and len(r.stdout) == 100


async def test_missing_program_and_missing_cwd(env: ContainerExecEnv) -> None:
    r = await env.run(["no-such-program"])
    assert r.exit_code == 127 and b"not found" in r.stderr
    with pytest.raises(FileNotFoundError):
        await env.run(["true"], cwd="/no/such/dir")


async def test_timeout_kills_the_whole_group_inside(env: ContainerExecEnv) -> None:
    late = f"{env.cwd}/late"
    r = await env.run(["sh", "-c", f"(sleep 2; touch {late}) & echo started; sleep 30"],
                      timeout=0.5)
    assert r.timed_out and r.exit_code == -1 and r.stdout == b"started\n"
    await asyncio.sleep(2.5)
    assert not await _exists(env, late)


async def test_cancel_kills_the_command_inside(env: ContainerExecEnv) -> None:
    late = f"{env.cwd}/late2"
    task = asyncio.create_task(env.run(["sh", "-c", f"(sleep 2; touch {late}) & sleep 30"]))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.5)
    assert not await _exists(env, late)
    left = await env.run(["sh", "-c", "ls /tmp | grep -c noeta-run || true"])
    assert left.stdout.strip() == b"0"


async def test_an_unknown_container_is_an_error() -> None:
    env = ContainerExecEnv("noeta-no-such-container")
    with pytest.raises(ContainerError, match="No such container"):
        await env.run(["true"])
    with pytest.raises(ContainerError):
        await env.read_bytes("/etc/hostname")


# ---------------------------------------------------------------- files


async def test_bytes_round_trip_and_windows(env: ContainerExecEnv) -> None:
    data = bytes(range(256)) * 10
    await env.write_bytes("deep/dir/f.bin", data)
    assert await env.read_bytes("deep/dir/f.bin") == data
    assert await env.read_range("deep/dir/f.bin", 250, 10) == data[250:260]
    assert await env.read_range("deep/dir/f.bin", 2555, 100) == data[2555:]
    assert await env.read_range("deep/dir/f.bin", 99999, 10) == b""
    await env.write_bytes("empty", b"")
    assert await env.read_bytes("empty") == b""


async def test_read_errors_match_the_local_env(env: ContainerExecEnv) -> None:
    with pytest.raises(FileNotFoundError):
        await env.read_bytes("missing")
    with pytest.raises(FileNotFoundError):
        await env.read_range("missing", 0, 10)
    await env.run(["mkdir", "d"])
    with pytest.raises(IsADirectoryError):
        await env.read_bytes("d")
    with pytest.raises(IsADirectoryError):
        await env.write_bytes("d", b"x")


async def test_write_keeps_mode_and_follows_symlinks(env: ContainerExecEnv) -> None:
    await env.write_bytes("script.sh", b"#!/bin/sh\necho 1\n")
    await env.run(["chmod", "755", "script.sh"])
    await env.write_bytes("script.sh", b"#!/bin/sh\necho 2\n")
    assert (await env.run(["./script.sh"])).stdout == b"2\n"
    await env.run(["ln", "-s", "script.sh", "link"])
    await env.write_bytes("link", b"#!/bin/sh\necho 3\n")
    assert (await env.run(["./script.sh"])).stdout == b"3\n"
    st = await env.stat("link")
    assert st is not None and st.is_symlink
    new = await env.run(["stat", "-c", "%a", "script.sh"])
    assert new.stdout.strip() == b"755"
    left = await env.run(["sh", "-c", "ls -a | grep -c noeta- || true"])
    assert left.stdout.strip() == b"0"


async def test_stat_and_realpath(env: ContainerExecEnv) -> None:
    await env.write_bytes("f.txt", b"hello")
    st = await env.stat("f.txt")
    assert st is not None and st.size == 5 and not st.is_dir and st.mtime > 1e9
    d = await env.stat(".")
    assert d is not None and d.is_dir
    assert await env.stat("nope") is None
    assert await env.stat("f.txt/below") is None
    await env.run(["ln", "-s", "gone", "dangling"])
    dl = await env.stat("dangling")
    assert dl is not None and dl.is_symlink and not dl.is_dir
    await env.run(["ln", "-s", env.cwd, "/tmp/alias-" + env.cwd.rsplit("/", 1)[1]])
    real = await env.realpath("/tmp/alias-" + env.cwd.rsplit("/", 1)[1] + "/f.txt")
    assert real == f"{env.cwd}/f.txt"
    assert await env.realpath("a/../missing/x") == f"{env.cwd}/missing/x"


# ---------------------------------------------------------------- the tools


class Ctx:
    def __init__(self, env: ContainerExecEnv) -> None:
        self.env = env
        self.state: dict[str, Any] = {}
        self.n = 0

    def record(self, ext: str, kind: str, data: Any) -> None:
        if ext == FS:
            self.state[FS] = reduce_fs(self.state.get(FS), kind, data)

    def __call__(self) -> ToolContext:
        self.n += 1
        return ToolContext(task_id="t", call_id=f"c{self.n}", env=self.env, state=self.state,
                           record=self.record)


def text(out: Any) -> str:
    return "".join(b.text for b in out.content if isinstance(b, Text))


async def test_file_and_shell_tools_run_in_the_container(env: ContainerExecEnv) -> None:
    ctx = Ctx(env)
    path = f"{env.cwd}/notes.txt"
    out = await WriteTool().run({"file_path": path, "content": "alpha\nbeta\n"}, ctx())
    assert not out.is_error, text(out)
    out = await ReadTool().run({"file_path": path}, ctx())
    assert "alpha" in text(out) and not out.is_error
    out = await EditTool().run({"file_path": path, "old_string": "beta",
                                "new_string": "gamma"}, ctx())
    assert not out.is_error, text(out)
    out = await BashTool().run({"command": "cat notes.txt && cd /tmp && pwd"}, ctx())
    assert "gamma" in text(out) and "/tmp" in text(out)


async def test_grep_and_glob_need_rg_in_the_container(
        env: ContainerExecEnv, container: str) -> None:
    ctx = Ctx(env)
    has_rg = (await env.run(["sh", "-c", "command -v rg"])).exit_code == 0
    if not has_rg:
        out = await GrepTool().run({"pattern": "x"}, ctx())
        assert out.is_error and "ripgrep (rg) is required" in text(out)
        host_rg = shutil.which("rg")
        if host_rg is None:
            pytest.skip("no rg on the host to copy into the container")
        _docker("cp", host_rg, f"{container}:/usr/local/bin/rg")
        if (await env.run(["rg", "--version"])).exit_code != 0:
            pytest.skip("the host's rg does not run in this image")
    await env.write_bytes("src/a.py", b"def needle():\n    pass\n")
    out = await GrepTool().run({"pattern": "needle"}, ctx())
    assert "a.py" in text(out), text(out)
    out = await GlobTool().run({"pattern": "**/*.py"}, ctx())
    assert "a.py" in text(out), text(out)


# ---------------------------------------------------------------- provisioning


async def test_start_stop_remove(tmp_path: Any) -> None:
    assert IMAGE is not None
    (tmp_path / "hello.txt").write_text("from the host")
    name = await start_container(IMAGE, mounts=[(str(tmp_path), "/workspace")],
                                 env={"GREETING": "hi"})
    try:
        assert await container_state(name) == "running"
        env = ContainerExecEnv(name)
        assert await env.read_bytes("hello.txt") == b"from the host"
        assert (await env.run(["sh", "-c", 'echo "$GREETING"'])).stdout == b"hi\n"
        await env.write_bytes("back.txt", b"to the host")
        assert (tmp_path / "back.txt").read_bytes() == b"to the host"
        assert await stop_container(name, timeout=2)
        assert await container_state(name) == "stopped"
    finally:
        await remove_container(name)
    assert await container_state(name) == "absent"
    with pytest.raises(ContainerError):
        await start_container("noeta-no-such-image:never")
