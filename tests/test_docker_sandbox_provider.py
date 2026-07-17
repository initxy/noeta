"""``LocalDockerSandboxProvider`` — the Local family of the SandboxProvider seam.

Drives the full allocate → attach → release flow against a FAKE docker (a
``subprocess.run``-shaped recorder) and a fake readiness probe, so no daemon runs
and no socket opens. Pins the wire the provider is coded against:

* ``allocate`` runs ``docker run -d`` with the name / port / api-key / mounts /
  resource caps, polls readiness, and returns a handle whose ``sandbox_id`` is
  the container name and ``base_url`` the mapped port;
* ``release`` = ``docker rm -f``;
* ``attach`` reconnects to a still-running container and raises for a gone one.
"""

from __future__ import annotations

import subprocess
from typing import Mapping

import pytest

from noeta.agent.host.docker_sandbox import (
    DockerSandboxError,
    LocalDockerSandboxProvider,
)
from noeta.sdk import MountSpec, SandboxSpec, encode_exec_env_ref


class FakeDocker:
    """Records docker argv and simulates run / rm / inspect."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.running: set[str] = set()
        self.run_returncode = 0

    def __call__(self, argv, **kwargs) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        self.kwargs.append(dict(kwargs))
        verb = argv[1]
        name = ""
        if verb == "run":
            name = argv[argv.index("--name") + 1]
            if self.run_returncode == 0:
                self.running.add(name)
            return subprocess.CompletedProcess(
                argv, self.run_returncode, stdout="cid\n", stderr="boom"
            )
        if verb == "rm":
            self.running.discard(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if verb == "inspect":
            name = argv[-1]
            alive = name in self.running
            return subprocess.CompletedProcess(
                argv, 0 if alive else 1, stdout="true" if alive else "", stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _provider(docker: FakeDocker, *, ready: bool = True, **kw) -> LocalDockerSandboxProvider:
    def probe(base_url: str, headers: Mapping[str, str]) -> bool:
        return ready

    return LocalDockerSandboxProvider(
        image="img:latest",
        run=docker,
        probe=probe,
        pick_port=lambda: 54321,
        sleep=lambda s: None,
        monotonic=iter([0.0, 0.1, 0.2, 0.3, 0.4, 100.0]).__next__,
        **kw,
    )


def test_allocate_runs_docker_and_returns_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_API_KEY", "s3cr3t")
    docker = FakeDocker()
    provider = _provider(docker)
    spec = SandboxSpec(
        image="img:latest",
        mounts=(
            MountSpec(source="/host/ws", target="/workspace", mode="rw"),
            MountSpec(source="/opt/skills", target="/opt/noeta/skills/builtin", mode="ro"),
        ),
        resources={"memory": "1g", "cpus": "2"},
    )
    handle = provider.allocate("task-abc", spec)
    assert handle.base_url == "http://127.0.0.1:54321"
    assert handle.sandbox_id == "noeta-sbx-task-abc"
    assert handle.workdir == "/workspace"
    assert handle.auth.connect_headers() == {"X-AIO-API-Key": "s3cr3t"}
    # the docker run argv carried name / port / api-key / mounts / caps
    run = next(c for c in docker.calls if c[1] == "run")
    assert "--name" in run and run[run.index("--name") + 1] == "noeta-sbx-task-abc"
    assert "127.0.0.1:54321:8080" in run
    # The key rides by NAME only (pass-through) — its VALUE is never in the argv
    # (host process table), it is injected through the subprocess env instead.
    assert "-e" in run and "SANDBOX_API_KEY" in run
    assert "SANDBOX_API_KEY=s3cr3t" not in run
    assert not any("s3cr3t" in tok for tok in run)
    run_kwargs = docker.kwargs[docker.calls.index(run)]
    assert run_kwargs["env"]["SANDBOX_API_KEY"] == "s3cr3t"
    assert "/host/ws:/workspace" in run
    assert "/opt/skills:/opt/noeta/skills/builtin:ro" in run
    assert "--memory" in run and "--cpus" in run
    assert "seccomp=unconfined" in run
    assert run[-1] == "img:latest"


def test_allocate_removes_stale_before_run() -> None:
    docker = FakeDocker()
    provider = _provider(docker)
    provider.allocate("task-abc", SandboxSpec(image="img:latest"))
    # First a reuse check (inspect) of a same-named running container, then a
    # best-effort rm of the stale container, then run.
    assert docker.calls[0][1] == "inspect"
    ops = [c[1] for c in docker.calls]
    assert "rm" in ops and "run" in ops
    assert ops.index("rm") < ops.index("run")


def test_allocate_raises_on_docker_run_failure() -> None:
    docker = FakeDocker()
    docker.run_returncode = 1
    provider = _provider(docker)
    with pytest.raises(DockerSandboxError, match="docker run failed"):
        provider.allocate("task-abc", SandboxSpec(image="img:latest"))


def test_allocate_times_out_and_reaps_when_never_ready() -> None:
    docker = FakeDocker()
    provider = _provider(docker, ready=False)
    with pytest.raises(DockerSandboxError, match="did not become ready"):
        provider.allocate("task-abc", SandboxSpec(image="img:latest"))
    # the half-started container was reaped
    assert any(c[1] == "rm" and c[-1] == "noeta-sbx-task-abc" for c in docker.calls)


def test_release_runs_docker_rm() -> None:
    docker = FakeDocker()
    provider = _provider(docker)
    provider.allocate("task-abc", SandboxSpec(image="img:latest"))
    provider.release("task-abc")
    assert [c for c in docker.calls if c[1] == "rm" and c[-1] == "noeta-sbx-task-abc"]
    assert "noeta-sbx-task-abc" not in docker.running


def test_attach_reconnects_to_running_container() -> None:
    docker = FakeDocker()
    provider = _provider(docker)
    handle = provider.allocate("task-abc", SandboxSpec(image="img:latest"))
    ref = encode_exec_env_ref(handle.base_url, handle.sandbox_id)
    attached = provider.attach(ref)
    assert attached.base_url == handle.base_url
    assert attached.sandbox_id == handle.sandbox_id


def test_attach_raises_when_container_gone() -> None:
    docker = FakeDocker()
    provider = _provider(docker)
    ref = encode_exec_env_ref("http://127.0.0.1:54321", "noeta-sbx-task-gone")
    with pytest.raises(DockerSandboxError, match="not running on this host"):
        provider.attach(ref)


def test_attach_rejects_ref_without_sandbox_id() -> None:
    docker = FakeDocker()
    provider = _provider(docker)
    with pytest.raises(DockerSandboxError, match="no sandbox id"):
        provider.attach("http://127.0.0.1:54321")

