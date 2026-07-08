"""``LocalDockerSandboxProvider`` — provision a per-session AIO Sandbox container.

The **Local** family of the ``SandboxProvider`` seam (D2): the container runs on
the worker's own Docker daemon, addressed at ``127.0.0.1:<port>``, authed by a
static ``SANDBOX_API_KEY``. It is the "who runs ``docker``" work the runtime /
SDK deliberately do not do (D1) — it lives in the product (``noeta.agent.host``)
and reaches the seam only through ``noeta.sdk``.

* :meth:`allocate` picks a free host port, ``docker run -d`` the AIO image with
  the assembled ``-v`` mounts + api-key + resource caps, polls ``GET /v1/sandbox``
  until the container is ready, and returns the live :class:`SandboxHandle`. The
  ``sandbox_id`` IS the container name (``noeta-sbx-<session_root_id>``), so the
  durable ``exec_env_ref`` names a specific container.
* :meth:`release` = ``docker rm -f`` (idempotent; a missing container is a
  no-op).
* :meth:`attach` reconnects to an ALREADY-running container named by a recorded
  ref — same host only. A container that is gone (host restart / another
  machine) raises a clear error: the Docker-local limitation a Distributed / NAS
  backend removes (R2 / D5-NAS).

**Isolation is process + mounted-FS only** (R1): the container sees only the
directories mounted in (workspace + skills), not the host root; it is not a full
FS/network jail. ``--security-opt seccomp=unconfined`` matches the AIO image's
own guidance (its inner tooling needs syscalls the default profile blocks).

``run`` / ``probe`` / ``pick_port`` are injected so the unit tests drive the full
allocate → attach → release flow with a fake docker + fake readiness probe,
opening no socket and shelling out to nothing.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Callable, Mapping, Optional

from noeta.sdk import (
    MountSpec,
    SandboxHandle,
    SandboxSpec,
    StaticApiKeyAuth,
    decode_exec_env_ref,
)


__all__ = ["LocalDockerSandboxProvider", "DockerSandboxError"]

_log = logging.getLogger(__name__)

#: Default AIO Sandbox image (the open-source ``agent-infra/sandbox``).
DEFAULT_SANDBOX_IMAGE = "ghcr.io/agent-infra/sandbox:latest"

#: The single port every AIO service fronts inside the container.
_CONTAINER_PORT = 8080

#: A ``subprocess.run``-shaped callable (injected so tests fake docker).
DockerRunner = Callable[..., "subprocess.CompletedProcess[str]"]
#: ``(base_url, headers) -> bool`` — one readiness probe attempt (injected).
ReadinessProbe = Callable[[str, Mapping[str, str]], bool]


class DockerSandboxError(RuntimeError):
    """A ``docker`` provisioning / attach failure (run, health, or missing)."""


def _pick_free_port() -> int:
    """Bind an ephemeral 127.0.0.1 port, release it, and return the number.

    A tiny TOCTOU window (the port could be taken before ``docker run`` binds
    it) — a failed ``docker run`` surfaces as :class:`DockerSandboxError`, which
    the caller can retry. Good enough for a local dev daemon.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _default_probe(base_url: str, headers: Mapping[str, str]) -> bool:
    """One readiness attempt: ``GET {base_url}/v1/sandbox`` → 2xx?"""
    request = urllib.request.Request(  # noqa: S310 — operator-configured URL
        base_url + "/v1/sandbox", headers=dict(headers), method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


class LocalDockerSandboxProvider:
    """Provision per-session AIO containers on the local Docker daemon.

    ``image`` / ``memory`` / ``cpus`` / ``extra_run_args`` are the deployment-fixed
    ``docker run`` shape; the per-session mounts come off the :class:`SandboxSpec`
    the SDK manager assembles. ``api_key_env`` names the env var holding the
    container's ``SANDBOX_API_KEY`` (read at provision time, injected into the
    container AND used for the readiness probe / adapter auth; never recorded, D5).
    """

    def __init__(
        self,
        *,
        image: str = DEFAULT_SANDBOX_IMAGE,
        api_key_env: str = "SANDBOX_API_KEY",
        memory: Optional[str] = "2g",
        cpus: Optional[str] = "2",
        workdir: str = "/workspace",
        container_prefix: str = "noeta-sbx-",
        extra_run_args: tuple[str, ...] = ("--security-opt", "seccomp=unconfined"),
        docker_bin: str = "docker",
        health_timeout_s: float = 60.0,
        health_interval_s: float = 0.5,
        run: Optional[DockerRunner] = None,
        probe: Optional[ReadinessProbe] = None,
        pick_port: Optional[Callable[[], int]] = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._image = image
        self._api_key_env = api_key_env
        self._memory = memory
        self._cpus = cpus
        self._workdir = workdir
        self._prefix = container_prefix
        self._extra_run_args = tuple(extra_run_args)
        self._docker = docker_bin
        self._health_timeout_s = health_timeout_s
        self._health_interval_s = health_interval_s
        self._run: DockerRunner = run or self._default_run
        self._probe: ReadinessProbe = probe or _default_probe
        self._pick_port = pick_port or _pick_free_port
        self._sleep = sleep
        self._monotonic = monotonic

    # -- SandboxProvider ---------------------------------------------------- #

    def allocate(self, session_root_id: str, spec: SandboxSpec) -> SandboxHandle:
        name = self._container_name(session_root_id)
        port = self._pick_port()
        base_url = f"http://127.0.0.1:{port}"
        # Best-effort remove a stale container of the same name (a crashed prior
        # session for this root id) so the run does not collide on the name.
        self._rm(name)
        argv = self._run_argv(name=name, port=port, spec=spec)
        result = self._run(
            argv, capture_output=True, text=True, check=False, env=self._run_env()
        )
        if result.returncode != 0:
            raise DockerSandboxError(
                f"docker run failed for {name!r}: {result.stderr.strip()}"
            )
        headers = StaticApiKeyAuth(self._api_key_env).connect_headers()
        self._await_ready(base_url, headers, name=name)
        _log.info("provisioned sandbox %s at %s", name, base_url)
        return SandboxHandle(
            base_url=base_url,
            sandbox_id=name,
            auth=StaticApiKeyAuth(self._api_key_env),
            workdir=self._workdir,
        )

    def release(self, session_root_id: str) -> None:
        self._rm(self._container_name(session_root_id))

    def attach(self, exec_env_ref: str) -> SandboxHandle:
        base_url, sandbox_id = decode_exec_env_ref(exec_env_ref)
        if not sandbox_id:
            raise DockerSandboxError(
                f"exec_env_ref {exec_env_ref!r} carries no sandbox id to attach"
            )
        if not self._is_running(sandbox_id):
            raise DockerSandboxError(
                f"sandbox container {sandbox_id!r} is not running on this host "
                "(a local-Docker container is bound to the machine that "
                "provisioned it; cross-machine reconnect needs a distributed / "
                "NAS backend)"
            )
        return SandboxHandle(
            base_url=base_url,
            sandbox_id=sandbox_id,
            auth=StaticApiKeyAuth(self._api_key_env),
            workdir=self._workdir,
        )

    # -- helpers ------------------------------------------------------------ #

    def _container_name(self, session_root_id: str) -> str:
        # Docker names allow [a-zA-Z0-9][a-zA-Z0-9_.-]; the session root id is a
        # ``task-<hex>`` string, already safe.
        return f"{self._prefix}{session_root_id}"

    def _run_argv(self, *, name: str, port: int, spec: SandboxSpec) -> list[str]:
        argv = [
            self._docker, "run", "-d",
            "--name", name,
            "-p", f"127.0.0.1:{port}:{_CONTAINER_PORT}",
        ]
        # Pass the key by NAME only (``-e SANDBOX_API_KEY``, no ``=value``): docker
        # reads the value from this process's environment (seeded in
        # :meth:`_run_env`) so the credential never lands in the ``docker run``
        # argv / host process table. It still appears in ``docker inspect`` — the
        # container genuinely needs it — but that is a container-scoped surface,
        # not the host command line (D5).
        if os.environ.get(self._api_key_env):
            argv += ["-e", "SANDBOX_API_KEY"]
        for mount in spec.mounts:
            argv += ["-v", self._mount_arg(mount)]
        for key, value in spec.env.items():
            argv += ["-e", f"{key}={value}"]
        memory = spec.resources.get("memory", self._memory)
        cpus = spec.resources.get("cpus", self._cpus)
        if memory:
            argv += ["--memory", str(memory)]
        if cpus:
            argv += ["--cpus", str(cpus)]
        argv += list(self._extra_run_args)
        argv.append(spec.image or self._image)
        return argv

    def _run_env(self) -> Optional[dict[str, str]]:
        """Subprocess env for ``docker run`` seeding the container key by name.

        Returns the current environment with ``SANDBOX_API_KEY`` bound to the
        value read from ``self._api_key_env`` (which may be spelled differently),
        so the ``-e SANDBOX_API_KEY`` pass-through injects it without the value
        ever entering the argv. ``None`` when no key is configured (the run then
        inherits the ambient environment unchanged)."""
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            return None
        return {**os.environ, "SANDBOX_API_KEY": api_key}

    @staticmethod
    def _mount_arg(mount: MountSpec) -> str:
        # Local Docker expresses every mount as ``-v source:target[:ro]``. The
        # ``kind`` (local-path / volume) is transparent to ``-v``; nas / pvc are
        # a Distributed-provider concern and never reach here.
        arg = f"{mount.source}:{mount.target}"
        if mount.mode == "ro":
            arg += ":ro"
        return arg

    def _await_ready(
        self, base_url: str, headers: Mapping[str, str], *, name: str
    ) -> None:
        deadline = self._monotonic() + self._health_timeout_s
        while self._monotonic() < deadline:
            if self._probe(base_url, headers):
                return
            self._sleep(self._health_interval_s)
        # Timed out — reap the half-started container so it does not linger.
        self._rm(name)
        raise DockerSandboxError(
            f"sandbox container {name!r} did not become ready at {base_url} "
            f"within {self._health_timeout_s:.0f}s"
        )

    def _rm(self, name: str) -> None:
        try:
            self._run(
                [self._docker, "rm", "-f", name],
                capture_output=True, text=True, check=False,
            )
        except Exception as exc:  # docker missing / daemon down — best effort
            _log.warning("docker rm %s failed: %s", name, exc)

    def _is_running(self, name: str) -> bool:
        result = self._run(
            [self._docker, "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, text=True, check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    @staticmethod
    def _default_run(argv: list[str], **kwargs: object) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(argv, **kwargs)  # noqa: S603 — operator-configured docker
