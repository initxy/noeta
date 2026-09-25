"""Live AIO container: a foreground ``Bash`` stop and a windowed ``Read``.

The fake-transport tests (``test_sandbox_foreground_interrupt.py``,
``test_sandbox_read_range.py``) pin what the adapter sends; this proves the
real image behaves the way they assume:

* a session stop kills ``sleep 30`` inside the container and ``Bash`` returns
  within seconds reading *interrupted*;
* a timed-out command is gone from the container afterwards (the image's
  ``hard_timeout`` alone does not reliably end it);
* ``Read`` serves a window of a ~3 MB file through ``read_range`` without a
  whole-file ``read_bytes``, byte-exact against the container's own
  ``sha256sum``.

Opt in with ``pytest -m live`` (needs Docker). ``NOETA_TEST_AIO_IMAGE`` picks
the image (default ``ghcr.io/agent-infra/sandbox:latest``).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from noeta.builtins.fs.impl import ReadFileTool
from noeta.builtins.fs.impl.shell import ShellRunTool
from noeta.builtins.sandbox.impl.exec_env import AioSandboxExecEnv
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.background_shell import ProcessRegistry
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.tool import InMemoryFileReadRegistry
from noeta.runtime.workspace import WorkspaceRoot
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


pytestmark = pytest.mark.live

_IMAGE_ENV = "NOETA_TEST_AIO_IMAGE"
_DEFAULT_IMAGE = "ghcr.io/agent-infra/sandbox:latest"
_CONTAINER_PORT = 8080
_READY_TIMEOUT_S = 120.0
_WORKDIR = "/tmp/noeta-live-wp-s"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _await_ready(base_url: str, key: str) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(  # noqa: S310
                base_url + "/v1/sandbox", headers={"X-AIO-API-Key": key}
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:  # noqa: S310
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    raise TimeoutError(f"AIO sandbox at {base_url} not ready")


@pytest.fixture(scope="module")
def sandbox() -> Iterator[AioSandboxExecEnv]:
    if shutil.which("docker") is None:
        pytest.skip("docker not found on PATH")
    image = os.environ.get(_IMAGE_ENV, _DEFAULT_IMAGE)
    key = f"noeta-live-{os.getpid()}-{int(time.time())}"
    port = _free_port()
    name = f"noeta-wp-s-live-{os.getpid()}-{int(time.time())}"
    result = subprocess.run(  # noqa: S603
        [
            "docker", "run", "-d", "--name", name,
            "-p", f"127.0.0.1:{port}:{_CONTAINER_PORT}",
            "--security-opt", "seccomp=unconfined",
            "--memory", "2g", "--cpus", "2",
            "-e", f"SANDBOX_API_KEY={key}",
            image,
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"docker run failed (image {image!r}?): {result.stderr.strip()}")
    try:
        base_url = f"http://127.0.0.1:{port}"
        try:
            _await_ready(base_url, key)
        except TimeoutError as exc:
            pytest.skip(str(exc))
        env = AioSandboxExecEnv(base_url=base_url, api_key=key, timeout_s=60.0)
        env.mkdir(Path(_WORKDIR))
        yield env
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)  # noqa: S603


def _processes(env: AioSandboxExecEnv, pattern: str) -> str:
    outcome = env.run_argv(
        ["bash", "-c", f"ps -eo pid,cmd | grep -E '{pattern}' || true"],
        cwd=Path("/"), timeout_s=10, output_cap=10_000,
    )
    return outcome.stdout.decode()


def _await_gone(env: AioSandboxExecEnv, pattern: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _processes(env, pattern).strip():
            return
        time.sleep(0.3)
    raise AssertionError(f"still running in the container: {_processes(env, pattern)!r}")


def test_session_stop_interrupts_sleep_in_the_container(
    sandbox: AioSandboxExecEnv,
) -> None:
    store = InMemoryContentStore()
    registry = ProcessRegistry(event_log=InMemoryEventLog(), content_store=store)
    tool = ShellRunTool(
        workspace=WorkspaceRoot.for_container(_WORKDIR),
        mode=ShellMode.ARBITRARY,
        exec_env=sandbox,
    )
    ctx = ToolContext(
        artifact_store=store,
        metadata={"task_id": "live-root", "trace_id": "tr"},
        background_runner=registry,
    )
    out: list[ToolResult] = []
    ended: list[float] = []

    def _run() -> None:
        out.append(tool.invoke({"command": "sleep 30.1", "timeout": 120_000}, ctx))
        ended.append(time.monotonic())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while "sleep 30.1" not in _processes(sandbox, "[s]leep 30.1"):
        assert time.monotonic() < deadline, "sleep never started in the container"
        time.sleep(0.2)
    stopped_at = time.monotonic()
    registry.kill_root_task("live-root")
    thread.join(10)
    assert not thread.is_alive(), "Bash did not return after the stop"
    assert ended[0] - stopped_at < 3.0
    (result,) = out
    assert result.success is False
    assert result.summary == "Command interrupted (stop requested)"
    _await_gone(sandbox, "[s]leep 30.1")


def test_timed_out_command_is_killed_in_the_container(
    sandbox: AioSandboxExecEnv,
) -> None:
    start = time.monotonic()
    outcome = sandbox.run_argv(
        ["bash", "-c", "echo started; sleep 30.2"],
        cwd=Path(_WORKDIR), timeout_s=2, output_cap=10_000,
    )
    assert outcome.timed_out is True
    assert time.monotonic() - start < 15
    _await_gone(sandbox, "[s]leep 30.2")


def test_windowed_read_of_a_3mb_file(sandbox: AioSandboxExecEnv) -> None:
    path = f"{_WORKDIR}/big.log"
    made = sandbox.run_argv(
        ["bash", "-c", f"seq 1 450000 > {path} && stat -c %s {path} "
         f"&& sha256sum {path} | cut -d' ' -f1"],
        cwd=Path(_WORKDIR), timeout_s=60, output_cap=10_000,
    )
    assert made.returncode == 0, made
    size_text, digest = made.stdout.decode().split()
    assert int(size_text) > 3_000_000

    class Counting(AioSandboxExecEnv):
        ranges: list[tuple[int, int]] = []

        def read_bytes(self, p: Path) -> bytes:
            raise AssertionError("Read must not pull the whole file")

        def read_range(self, p: Path, offset: int, length: int) -> bytes:
            self.ranges.append((offset, length))
            return super().read_range(p, offset, length)

    env: Any = Counting(base_url=sandbox._base, auth_headers=sandbox._request_headers)  # noqa: SLF001
    ctx = ToolContext(
        artifact_store=InMemoryContentStore(),
        file_read_registry=InMemoryFileReadRegistry(),
    )
    result = ReadFileTool(
        workspace=WorkspaceRoot.for_container(_WORKDIR), exec_env=env
    ).invoke({"file_path": "big.log", "offset": 250_000, "limit": 3}, ctx)
    assert result.success, result.summary
    served = [line.split("\t")[1] for line in result.output.splitlines() if "\t" in line]
    assert served == ["250000", "250001", "250002"]
    assert "of 450000 total lines" in result.output
    assert len(Counting.ranges) >= 3
    assert ctx.file_read_registry is not None
    assert ctx.file_read_registry.digest(path) == digest
