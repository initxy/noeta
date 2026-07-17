"""LocalDockerSandboxProvider idle-reclaim behavior against **real docker**
(docker-gated).

test_docker_sandbox.py pins the provider's logic with a fake docker; this
inverts it and pins that "the wire the fake docker simulates really is real
docker's behavior" — the whole "stop, don't tear down" idle-reclaim scheme
rests on it (see the docker_sandbox module docstring):

- ``docker stop`` keeps the container, :meth:`attach` can ``docker start`` it
  back as-is, and the **host port mapping is unchanged** (so the base_url in
  the exec_env_ref needs no re-resolution);
- the container's write layer comes back alive with it (stop-not-teardown =
  in-container state survives; this is where it beats "tear down and
  rebuild");
- the restored container really serves (through the real readiness probe, not
  a fake one);
- when the container is entirely gone, attach raises the clear cross-host
  unrecoverable error.

Really starts an AIO container (~10s); always torn down afterwards.
"""
from __future__ import annotations

import subprocess

import pytest

from noeta.agent.host.docker_sandbox import (
    DockerSandboxError,
    LocalDockerSandboxProvider,
    _default_probe,
)
from noeta.sdk import MountSpec, SandboxSpec, encode_exec_env_ref
from tests._docker_sandbox import DOCKER_SANDBOX_IMAGE, requires_docker_sandbox


_CID = "e2e-idle-restart"


@pytest.fixture
def provider(tmp_path):
    p = LocalDockerSandboxProvider(
        image=DOCKER_SANDBOX_IMAGE or "",
        health_timeout_s=120.0,
        resolve_container_id=lambda _root: _CID,
    )
    yield p
    p.force_release(_CID)  # idempotent: always tear down, never leave the container on the dev machine


def _spec(ws) -> SandboxSpec:
    return SandboxSpec(
        image=DOCKER_SANDBOX_IMAGE,
        mounts=(MountSpec(source=str(ws), target="/workspace", mode="rw"),),
        resources={"memory": "2g", "cpus": "2"},
    )


def _in_container(name: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", name, "sh", "-c", command],
        capture_output=True, text=True, check=False,
    )


@requires_docker_sandbox
def test_stop_idle_then_attach_restarts_same_container(provider, tmp_path):
    """A container stopped while idle: attach brings it back as-is — same
    container, same port, state intact, really serving."""
    ws = tmp_path / "ws"
    ws.mkdir()
    handle = provider.allocate("task-root", _spec(ws))
    name = handle.sandbox_id

    # Leave a marker in the container's write layer (outside the mounts):
    # only "stop, don't tear down" lets it survive reclamation
    marker = _in_container(name, "echo alive > /tmp/marker && cat /tmp/marker")
    assert marker.returncode == 0 and "alive" in marker.stdout

    assert provider.stop_idle(_CID) is True
    state = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True, text=True, check=False,
    )
    assert state.returncode == 0 and state.stdout.strip() == "false"  # stopped but still there

    attached = provider.attach(encode_exec_env_ref(handle.base_url, handle.sandbox_id))

    assert attached.sandbox_id == name
    assert attached.base_url == handle.base_url          # port mapping restored as-is
    assert _default_probe(attached.base_url, attached.auth.connect_headers())  # really serving
    assert "alive" in _in_container(name, "cat /tmp/marker").stdout  # write layer came back


@requires_docker_sandbox
def test_attach_raises_when_container_removed(provider, tmp_path):
    """After level-two reclaim tore it down (or the machine changed): attach
    raises the clear cross-host unrecoverable error."""
    ws = tmp_path / "ws"
    ws.mkdir()
    handle = provider.allocate("task-root", _spec(ws))
    ref = encode_exec_env_ref(handle.base_url, handle.sandbox_id)
    provider.force_release(_CID)

    with pytest.raises(DockerSandboxError, match="not running on this host"):
        provider.attach(ref)


@requires_docker_sandbox
def test_allocate_does_not_steal_port_of_stopped_container(provider, tmp_path):
    """A new container must not steal a stopped container's port — steal it
    and that container can never start back (real docker reports the port
    conflict)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    handle = provider.allocate("task-root", _spec(ws))
    stopped_port = int(handle.base_url.rsplit(":", 1)[1])
    assert provider.stop_idle(_CID) is True

    # another session provisions a new container: pick_port deliberately keeps
    # proposing the stopped container's port
    other = LocalDockerSandboxProvider(
        image=DOCKER_SANDBOX_IMAGE or "",
        health_timeout_s=120.0,
        pick_port=lambda: stopped_port,
        resolve_container_id=lambda _root: "e2e-other",
    )
    with pytest.raises(DockerSandboxError, match="no free host port"):
        other.allocate("task-other", _spec(ws))

    # the stopped container can still be brought back as-is (port not stolen)
    attached = provider.attach(encode_exec_env_ref(handle.base_url, handle.sandbox_id))
    assert attached.base_url == handle.base_url
