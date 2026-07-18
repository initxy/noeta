"""vendored ``LocalDockerSandboxProvider`` — the Local family of the
SandboxProvider seam.

Drives the full allocate → attach → stop_idle → release with a fake docker (a
``subprocess.run``-shaped recorder) + a fake readiness probe — no daemon, no
sockets. Pins the wire the provider is coded against, the app-added
:func:`container_id_from_ref` reverse-decode (the per-exec preamble seam maps
a ref back to the session user through it), per-session container sharing
(resolver naming + reuse + refcounted release + force_release), and the idle
reclamation's "stop, don't tear down" (stop_idle → attach docker start
restore + port reservation). Adapted from noeta
``tests/test_docker_sandbox_provider.py``.
"""

from __future__ import annotations

import itertools
import subprocess
from typing import Callable, Mapping, Optional

import pytest

from noeta.agent.host.docker_sandbox import (
    CONTAINER_PREFIX,
    DockerSandboxError,
    LocalDockerSandboxProvider,
    container_id_from_ref,
)
from noeta.sdk import MountSpec, SandboxSpec, encode_exec_env_ref


class FakeDocker:
    """Records docker argv and simulates run / rm / stop / start / inspect /
    ps / port.

    The three container states are simulated faithfully — the provider relies
    on the ``inspect`` wire to distinguish "stopped" from "absent" (absent →
    non-zero exit; present → 0 + true/false), and attach uses that to decide
    between restoring and reporting cross-host unrecoverable, so the two must
    not be collapsed into a single alive boolean here.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.running: set[str] = set()
        self.stopped: set[str] = set()
        #: container name → host-mapped port (the static config laid down by
        #: ``run -p``; survives a stop)
        self.ports: dict[str, int] = {}
        self.run_returncode = 0
        self.start_returncode = 0

    def _ok(self, argv, stdout: str = "") -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    def _fail(self, argv, rc: int = 1, stderr: str = "boom"):
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr=stderr)

    def __call__(self, argv, **kwargs) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        self.kwargs.append(dict(kwargs))
        verb = argv[1]
        if verb == "run":
            if self.run_returncode != 0:
                return self._fail(argv, self.run_returncode)
            name = argv[argv.index("--name") + 1]
            self.running.add(name)
            # -p 127.0.0.1:<port>:8080
            self.ports[name] = int(argv[argv.index("-p") + 1].split(":")[1])
            return self._ok(argv, stdout="cid\n")
        if verb == "rm":
            name = argv[-1]
            self.running.discard(name)
            self.stopped.discard(name)
            self.ports.pop(name, None)
            return self._ok(argv)
        if verb == "stop":
            name = argv[-1]
            if name in self.running:
                self.running.discard(name)
                self.stopped.add(name)
            return self._ok(argv, stdout=name)
        if verb == "start":
            if self.start_returncode != 0:
                return self._fail(
                    argv, self.start_returncode, stderr="port is already allocated"
                )
            name = argv[-1]
            if name in self.stopped:
                self.stopped.discard(name)
                self.running.add(name)
            return self._ok(argv, stdout=name)
        if verb == "inspect":
            fmt_at = argv.index("-f")
            fmt, names = argv[fmt_at + 1], argv[fmt_at + 2:]
            if fmt == "{{.State.Running}}":
                name = names[0]
                if name in self.running:
                    return self._ok(argv, stdout="true\n")
                if name in self.stopped:
                    return self._ok(argv, stdout="false\n")
                return self._fail(argv, stderr="No such object")
            # port-mapping template (multi-container capable): one line per
            # container
            return self._ok(
                argv,
                stdout="\n".join(
                    f"{self.ports[n]} " if n in self.ports else "" for n in names
                ),
            )
        if verb == "ps":
            return self._ok(argv, stdout="\n".join(sorted(self.running | self.stopped)))
        if verb == "port":
            name = argv[2]
            if name not in self.running:
                return self._fail(argv)
            return self._ok(argv, stdout=f"127.0.0.1:{self.ports[name]}\n")
        return self._ok(argv)


def _tick() -> Callable[[], float]:
    """Monotonic clock: +10s per call. Makes _await_ready's 60s timeout land
    within a few rounds (sleep is a no-op)."""
    counter = itertools.count(0.0, 10.0)
    return lambda: next(counter)


class _Probe:
    """Readiness probe whose ready flag can flip mid-flight (build it, then
    make it unreachable — simulating a restore that never comes up)."""

    def __init__(self, ready: bool = True) -> None:
        self.ready = ready

    def __call__(self, base_url: str, headers: Mapping[str, str]) -> bool:
        return self.ready


def _provider(
    docker: FakeDocker,
    *,
    ready: bool = True,
    probe: Optional[_Probe] = None,
    pick_port: Optional[Callable[[], int]] = None,
    **kw,
) -> LocalDockerSandboxProvider:
    return LocalDockerSandboxProvider(
        image="img:latest",
        run=docker,
        probe=probe or _Probe(ready),
        pick_port=pick_port or (lambda: 54321),
        sleep=lambda s: None,
        monotonic=_tick(),
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
            MountSpec(source="/opt/skills", target="/skills", mode="ro"),
        ),
        resources={"memory": "1g", "cpus": "2"},
    )
    handle = provider.allocate("task-abc", spec)
    assert handle.base_url == "http://127.0.0.1:54321"
    assert handle.sandbox_id == "noeta-sbx-task-abc"
    assert handle.workdir == "/workspace"
    assert handle.auth.connect_headers() == {"X-AIO-API-Key": "s3cr3t"}
    # docker run argv carries name / port / api-key / mounts / caps
    run = next(c for c in docker.calls if c[1] == "run")
    assert "--name" in run and run[run.index("--name") + 1] == "noeta-sbx-task-abc"
    assert "127.0.0.1:54321:8080" in run
    # the key is passed through by name only — the value never enters argv
    # (host process table); it is injected via the subprocess env
    assert "-e" in run and "SANDBOX_API_KEY" in run
    assert "SANDBOX_API_KEY=s3cr3t" not in run
    assert not any("s3cr3t" in tok for tok in run)
    run_kwargs = docker.kwargs[docker.calls.index(run)]
    assert run_kwargs["env"]["SANDBOX_API_KEY"] == "s3cr3t"
    assert "/host/ws:/workspace" in run
    assert "/opt/skills:/skills:ro" in run
    assert "--memory" in run and "--cpus" in run
    assert "seccomp=unconfined" in run
    assert run[-1] == "img:latest"


def test_allocate_removes_stale_before_run() -> None:
    docker = FakeDocker()
    provider = _provider(docker)
    provider.allocate("task-abc", SandboxSpec(image="img:latest"))
    verbs = [c[1] for c in docker.calls]
    assert verbs[0] == "inspect"        # liveness probe first (reuse-path decision)
    assert verbs[-2:] == ["rm", "run"]  # best-effort removal of a stale same-name container → run


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
    # the half-started container is reaped
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


def test_attach_raises_when_container_absent() -> None:
    """The container does not exist on this host at all (machine changed /
    already removed by level-two reclaim) → cross-host unrecoverable, raise a
    clear error."""
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


def test_container_id_from_ref_round_trips() -> None:
    # sandbox_id == CONTAINER_PREFIX + container_id; the preamble seam relies
    # on this to decode session identity back out
    ref = encode_exec_env_ref("http://127.0.0.1:54321", CONTAINER_PREFIX + "task-abc")
    assert container_id_from_ref(ref) == "task-abc"
    # a bare ref without sandbox_id → empty string (attach-one-container backend)
    assert container_id_from_ref("http://127.0.0.1:54321") == ""


# ------------------------------------------------- per-session sharing
def _session_provider(
    docker: FakeDocker, mapping: dict, **kw
) -> LocalDockerSandboxProvider:
    return _provider(docker, resolve_container_id=mapping.get, **kw)


def test_allocate_names_container_by_session_and_reuses() -> None:
    docker = FakeDocker()
    mapping = {"task-1": "sess-a", "task-2": "sess-a"}
    provider = _session_provider(docker, mapping)

    h1 = provider.allocate("task-1", SandboxSpec(image="img:latest"))
    assert h1.sandbox_id == "noeta-sbx-sess-a"
    runs = [c for c in docker.calls if c[1] == "run"]
    assert len(runs) == 1

    # second task of the same session: reuse the running container, no second
    # docker run
    h2 = provider.allocate("task-2", SandboxSpec(image="img:latest"))
    assert h2.sandbox_id == "noeta-sbx-sess-a"
    assert h2.base_url == h1.base_url
    runs = [c for c in docker.calls if c[1] == "run"]
    assert len(runs) == 1


def test_release_is_refcounted_last_root_removes() -> None:
    docker = FakeDocker()
    mapping = {"task-1": "sess-a", "task-2": "sess-a"}
    provider = _session_provider(docker, mapping)
    provider.allocate("task-1", SandboxSpec(image="img:latest"))
    provider.allocate("task-2", SandboxSpec(image="img:latest"))

    # first root released: the container is still referenced by task-2, no rm
    provider.release("task-1")
    assert "noeta-sbx-sess-a" in docker.running
    # last root released: really tear the container down
    provider.release("task-2")
    assert "noeta-sbx-sess-a" not in docker.running


def test_force_release_removes_regardless_of_refcount() -> None:
    docker = FakeDocker()
    mapping = {"task-1": "sess-a", "task-2": "sess-a"}
    provider = _session_provider(docker, mapping)
    provider.allocate("task-1", SandboxSpec(image="img:latest"))
    provider.allocate("task-2", SandboxSpec(image="img:latest"))

    provider.force_release("sess-a")
    assert "noeta-sbx-sess-a" not in docker.running
    # subsequent per-root releases are idempotent, no more errors
    provider.release("task-1")
    provider.release("task-2")


def test_reuse_after_restart_recovers_port_via_docker_port() -> None:
    docker = FakeDocker()
    mapping = {"task-1": "sess-a", "task-2": "sess-a"}
    p1 = _session_provider(docker, mapping)
    h1 = p1.allocate("task-1", SandboxSpec(image="img:latest"))

    # a new provider instance (process restart, in-memory handles lost), the
    # container is still running
    p2 = _session_provider(docker, mapping, pick_port=lambda: 60001)
    h = p2.allocate("task-2", SandboxSpec(image="img:latest"))
    assert h.sandbox_id == "noeta-sbx-sess-a"
    # the port comes from the docker port reverse-decode (the container's
    # actual mapping), not the new provider's freshly picked 60001
    assert h.base_url == h1.base_url == "http://127.0.0.1:54321"
    runs = [c for c in docker.calls if c[1] == "run"]
    assert len(runs) == 1


def test_resolver_missing_falls_back_to_task_naming() -> None:
    docker = FakeDocker()
    provider = _session_provider(docker, {})  # mapping misses → fall back to root id
    h = provider.allocate("task-zzz", SandboxSpec(image="img:latest"))
    assert h.sandbox_id == "noeta-sbx-task-zzz"


# ------------------------------------------- idle reclaim: stop, don't tear down + attach restore
def test_stop_idle_keeps_container_for_restart() -> None:
    """Level-one reclaim = docker stop: the processes are gone (RAM/CPU back
    to the host), the container body stays."""
    docker = FakeDocker()
    provider = _session_provider(docker, {"task-1": "sess-a"})
    provider.allocate("task-1", SandboxSpec(image="img:latest"))

    assert provider.stop_idle("sess-a") is True
    assert "noeta-sbx-sess-a" not in docker.running
    # the container still exists (stopped, not rm'ed) — only then can attach
    # bring it back
    assert "noeta-sbx-sess-a" in docker.stopped
    assert docker.ports["noeta-sbx-sess-a"] == 54321


def test_stop_idle_false_when_not_running() -> None:
    """Already stopped / never existed → False (the reaper uses this to avoid
    duplicate logging and no-op docker stops)."""
    docker = FakeDocker()
    provider = _session_provider(docker, {"task-1": "sess-a"})
    provider.allocate("task-1", SandboxSpec(image="img:latest"))

    assert provider.stop_idle("sess-a") is True
    assert provider.stop_idle("sess-a") is False   # idempotent
    assert provider.stop_idle("sess-never") is False


def test_attach_restarts_idle_stopped_container() -> None:
    """The mandatory continue-the-conversation path: a container stopped by
    level-one reclaim is brought back as-is by attach via docker start."""
    docker = FakeDocker()
    provider = _session_provider(docker, {"task-1": "sess-a"})
    handle = provider.allocate("task-1", SandboxSpec(image="img:latest"))
    ref = encode_exec_env_ref(handle.base_url, handle.sandbox_id)
    provider.stop_idle("sess-a")

    attached = provider.attach(ref)
    assert attached.sandbox_id == handle.sandbox_id
    # docker start restores the port mapping as-is → the base_url in the ref
    # stays valid
    assert attached.base_url == handle.base_url
    assert "noeta-sbx-sess-a" in docker.running
    assert [c for c in docker.calls if c[1] == "start"]
    # the restored container is the same one, not a rebuild (a rebuild would
    # lose in-container state, and attach has no spec anyway)
    assert len([c for c in docker.calls if c[1] == "run"]) == 1


def test_attach_raises_when_restart_fails() -> None:
    """docker start failed (e.g. the original port got taken) → raise a clear
    error and do NOT tear the container down (tearing it down makes it
    unrecoverable)."""
    docker = FakeDocker()
    provider = _session_provider(docker, {"task-1": "sess-a"})
    handle = provider.allocate("task-1", SandboxSpec(image="img:latest"))
    ref = encode_exec_env_ref(handle.base_url, handle.sandbox_id)
    provider.stop_idle("sess-a")
    docker.start_returncode = 1

    with pytest.raises(DockerSandboxError, match="docker start failed"):
        provider.attach(ref)
    assert "noeta-sbx-sess-a" in docker.stopped  # kept; the next attach can retry


def test_attach_stops_back_when_restart_never_ready() -> None:
    """Started but the probe never passes → stop it back to stopped so it
    stays retryable; don't leave a half-dead container that runs but serves
    nothing."""
    docker = FakeDocker()
    probe = _Probe(ready=True)
    provider = _session_provider(docker, {"task-1": "sess-a"}, probe=probe)
    handle = provider.allocate("task-1", SandboxSpec(image="img:latest"))
    ref = encode_exec_env_ref(handle.base_url, handle.sandbox_id)
    provider.stop_idle("sess-a")
    probe.ready = False

    with pytest.raises(DockerSandboxError, match="did not become ready"):
        provider.attach(ref)
    assert "noeta-sbx-sess-a" not in docker.running
    assert "noeta-sbx-sess-a" in docker.stopped  # stopped back, and not rm'ed


def test_allocate_skips_port_reserved_by_stopped_container() -> None:
    """A new container must not steal a stopped container's host port — steal
    it and that container can never docker start back.

    A stopped container binds no port, so the bind(0) probe cannot see it; it
    must be excluded explicitly (the module docstring's "stop vs tear down").
    """
    docker = FakeDocker()
    ports = iter([54321, 54322])
    provider = _provider(
        docker,
        pick_port=lambda: next(ports),
        resolve_container_id={"task-1": "sess-a", "task-2": "sess-b"}.get,
    )
    a = provider.allocate("task-1", SandboxSpec(image="img:latest"))
    assert a.base_url == "http://127.0.0.1:54321"
    provider.stop_idle("sess-a")

    # another session provisions a new container: 54321 is reserved by the
    # stopped sess-a → move to 54322
    b = provider.allocate("task-2", SandboxSpec(image="img:latest"))
    assert b.base_url == "http://127.0.0.1:54322"

    # sess-a can still be brought back as-is (its port was not stolen)
    ref = encode_exec_env_ref(a.base_url, a.sandbox_id)
    assert provider.attach(ref).base_url == "http://127.0.0.1:54321"


def test_allocate_ignores_reserved_ports_when_docker_query_fails() -> None:
    """docker ps flake → treat as "no reservations"; one failed query must
    not block provisioning."""
    docker = FakeDocker()

    def flaky(argv, **kwargs):
        if argv[1] == "ps":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        return docker(argv, **kwargs)

    provider = _provider(flaky)
    h = provider.allocate("task-abc", SandboxSpec(image="img:latest"))
    assert h.base_url == "http://127.0.0.1:54321"
