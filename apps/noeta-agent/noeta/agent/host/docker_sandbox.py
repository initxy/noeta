"""``LocalDockerSandboxProvider`` — one local Docker AIO Sandbox container per app session.

Vendored from noeta ``apps/noeta-agent/noeta/agent/host/docker_sandbox.py``
(noeta 0.1.14), with **per-session container sharing** added on top.
``noeta.agent.host`` is noeta product-layer code and is not distributed with
the PyPI noeta-sdk / noeta-runtime this app installs, hence the copy; it
depends only on public ``noeta.sdk`` symbols. When upstream updates, re-sync
against the same signatures and re-apply this layer.

It is the Local family implementation of the ``SandboxProvider`` seam: the
container runs on the backend's own Docker daemon, addressed at
``127.0.0.1:<port>``, authed by a static ``SANDBOX_API_KEY``. This is the
"who runs ``docker``" work the SDK / runtime deliberately do not do — it
lands on the deployment (this app).

Per-session sharing: noeta calls allocate/release per root task (its
"session" = one task tree), while multiple root tasks of one app workflow
session share a single container. With ``resolve_container_id`` injected
(root task id → app session id):

* Container name = ``noeta-sbx-<session_id>``; when the same session's second
  task allocates and finds the same-named container running → **reuse** (the
  host port is recovered via ``docker port`` to rebuild the handle) instead
  of starting a new container.
* release is **reference-counted** per container: the noeta driver calls
  release at any root task's terminal state (SDK sandbox.py), and a shared
  container is only really ``docker rm``'ed when its last root releases —
  otherwise stopping one task would tear down the whole session's container.
* Without an injected resolver (or when resolution fails) the naming falls
  back to the root task id, matching upstream behavior.

* :meth:`allocate` picks a free host port, ``docker run -d`` the AIO image
  (with the assembled ``-v`` mounts + api-key + resource caps), polls
  ``GET /v1/sandbox`` until ready, and returns the live
  :class:`SandboxHandle`. ``sandbox_id`` IS the container name.
* :meth:`release` = reference-count decrement; only at zero does it
  ``docker rm -f`` (idempotent).
* :meth:`force_release` = ``docker rm -f`` directly by app session id
  (session deletion path, bypassing the refcount — the session is gone, the
  container must go).
* :meth:`stop_idle` = idle-reclaim level one: ``docker stop`` but **keep the
  container**.
* :meth:`attach` reconnects to a container named by a ref — same host only.

Stop vs remove (idle reclamation, see ``Settings.sandbox_idle_*``): a
container gets its :class:`SandboxSpec` (mounts / env / resources) only at
the ``docker run`` moment; :meth:`attach` only ever has the
``exec_env_ref`` — **once removed it cannot be rebuilt**. And conversation
continuation goes resume→attach rather than seed_start→allocate (one app
session = one noeta root task), so idle reclamation always ``docker stop``s
and never ``rm``s: stop already kills the processes and hands memory and CPU
back to the host (the whole point of reclamation), while the container body
plus its write layer / mounts / **port mappings** all remain; :meth:`attach`
``docker start``s it back as-is — the ``base_url`` inside ``exec_env_ref``
stays valid and in-container state is not lost. The one thing stop does not
return is disk, which the long-TTL level-two ``rm`` collects (after which
attach no longer works, as intended).

This is also why :meth:`allocate` must exclude :meth:`_reserved_ports` when
provisioning a new container: a stopped container binds no port, so a
``bind(0)`` probe cannot see it, yet ``docker start`` restores the original
mapping — hand that port to a new container and the stopped session can
never come back up.

When the container does not exist on this machine at all (different host /
already removed by level two), :meth:`attach` raises a clear error: an
inherent limitation of the Docker-local backend that a future distributed /
NAS backend removes.

**Isolation is process + mounted-FS only**: the container sees only the
directories mounted in (workspace + skills), not the host root; it is not a
full FS / network jail. ``--security-opt seccomp=unconfined`` is the AIO
image's own requirement (its inner tooling needs syscalls the default
seccomp profile blocks).

``run`` / ``probe`` / ``pick_port`` are injected so the unit tests drive the
full allocate → attach → release flow with a fake docker + fake readiness
probe, opening no socket and shelling out to nothing.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Literal, Mapping, Optional

from noeta.sdk import (
    MountSpec,
    SandboxHandle,
    SandboxSpec,
    StaticApiKeyAuth,
    decode_exec_env_ref,
)


__all__ = [
    "LocalDockerSandboxProvider",
    "DockerSandboxError",
    "DEFAULT_SANDBOX_IMAGE",
    "CONTAINER_PREFIX",
    "container_id_from_ref",
]

_log = logging.getLogger(__name__)

#: Default AIO Sandbox image (the open-source ``agent-infra/sandbox``).
DEFAULT_SANDBOX_IMAGE = "ghcr.io/agent-infra/sandbox:latest"

#: Container-name prefix; container name = ``CONTAINER_PREFIX + <container_id>``,
#: i.e. handle.sandbox_id. With a resolver injected the container_id is the app
#: session id, otherwise it falls back to the root task id. The per-exec
#: preamble seam derives it back out of an exec_env_ref (see
#: :func:`container_id_from_ref`) — single source of truth; change both
#: together.
CONTAINER_PREFIX = "noeta-sbx-"

#: The single port every AIO service fronts inside the container.
_CONTAINER_PORT = 8080

#: ``docker inspect -f`` template: extract the host ports from a container's
#: **static** port bindings (one line per container, space-separated). Why not
#: ``docker port``: that reads NetworkSettings.Ports, which is empty the moment
#: the container stops; HostConfig.PortBindings is the static configuration
#: laid down by ``docker run -p``, present for stopped containers too, and
#: ``docker start`` restores the mapping exactly from it — which is precisely
#: the port we must protect from being grabbed by a new container (see the
#: module docstring, "stop vs remove").
_PORT_BINDINGS_FMT = (
    "{{range $p, $conf := .HostConfig.PortBindings}}"
    "{{range $conf}}{{.HostPort}} {{end}}{{end}}"
)

#: Retry count for avoiding reserved ports when provisioning a new container
#: (each retry re-``bind(0)``s for a different port).
_PORT_PICK_ATTEMPTS = 10

#: Container state on this machine: running / stopped (exists but stopped;
#: ``docker start`` brings it back as-is) / absent (no such container here —
#: removed, or a different host).
ContainerState = Literal["running", "stopped", "absent"]

#: A ``subprocess.run``-shaped callable (injected so tests fake docker).
DockerRunner = Callable[..., "subprocess.CompletedProcess[str]"]
#: ``(base_url, headers) -> bool`` — one readiness probe attempt (injected).
ReadinessProbe = Callable[[str, Mapping[str, str]], bool]


class DockerSandboxError(RuntimeError):
    """A ``docker`` provisioning / attach failure (run, health, or missing)."""


def container_id_from_ref(
    exec_env_ref: str, *, prefix: str = CONTAINER_PREFIX
) -> str:
    """Derive the container_id back out of an ``exec_env_ref`` (``base_url#sandbox_id``).

    ``sandbox_id == prefix + container_id`` (see :meth:`_container_name`), so
    stripping the prefix yields it. Under a deployment with the resolver
    injected it is the app session id, otherwise the root task id — a per-exec
    preamble binding can map either shape back to the session user identity.
    Returns the empty string when there is no sandbox_id (e.g. an
    attach-one-container backend).
    """
    _, sandbox_id = decode_exec_env_ref(exec_env_ref)
    return sandbox_id.removeprefix(prefix) if sandbox_id else ""


def _pick_free_port() -> int:
    """Bind an ephemeral 127.0.0.1 port, release it, and return the number.

    A tiny TOCTOU window (the port could be taken before ``docker run`` binds
    it) — a failed ``docker run`` surfaces as :class:`DockerSandboxError`,
    which the caller can retry. Good enough for a local dev daemon.
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

    ``image`` / ``memory`` / ``cpus`` / ``extra_run_args`` are the
    deployment-fixed ``docker run`` shape; the per-session mounts come off the
    :class:`SandboxSpec` the SDK manager assembles. ``api_key_env`` names the
    env var holding the container's ``SANDBOX_API_KEY`` (read at provision
    time, injected into the container AND used for the readiness probe /
    adapter auth; never recorded).
    """

    def __init__(
        self,
        *,
        image: str = DEFAULT_SANDBOX_IMAGE,
        api_key_env: str = "SANDBOX_API_KEY",
        memory: Optional[str] = "2g",
        cpus: Optional[str] = "2",
        workdir: str = "/workspace",
        container_prefix: str = CONTAINER_PREFIX,
        extra_run_args: tuple[str, ...] = ("--security-opt", "seccomp=unconfined"),
        docker_bin: str = "docker",
        health_timeout_s: float = 60.0,
        health_interval_s: float = 0.5,
        run: Optional[DockerRunner] = None,
        probe: Optional[ReadinessProbe] = None,
        pick_port: Optional[Callable[[], int]] = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        resolve_container_id: Optional[Callable[[str], Optional[str]]] = None,
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
        # Per-session sharing: root task id → container id (app session id).
        # None / unresolvable → fall back to the root task id (per-task
        # container, the original upstream behavior).
        self._resolve_container_id = resolve_container_id
        self._cid_lock = threading.Lock()
        #: root task id → container id (recorded by allocate, consumed by release)
        self._cid_by_root: dict[str, str] = {}
        #: container id → set of root task ids referencing it (refcount)
        self._roots_by_cid: dict[str, set[str]] = {}
        #: container id → live handle (reused by the same session's later
        #: tasks, avoiding the docker-port recovery)
        self._handle_by_cid: dict[str, SandboxHandle] = {}

    # -- SandboxProvider ---------------------------------------------------- #

    def _container_id_for(self, session_root_id: str) -> str:
        """root task id → container id. Resolver missing / failing / empty → fall back to the root id."""
        if self._resolve_container_id is not None:
            try:
                cid = self._resolve_container_id(session_root_id)
            except Exception:  # noqa: BLE001 - resolution failure degrades to a per-task container
                _log.warning(
                    "resolve_container_id failed for %s", session_root_id,
                    exc_info=True,
                )
                cid = None
            if cid:
                return cid
        return session_root_id

    def allocate(self, session_root_id: str, spec: SandboxSpec) -> SandboxHandle:
        cid = self._container_id_for(session_root_id)
        name = self._container_name(cid)
        headers = StaticApiKeyAuth(self._api_key_env).connect_headers()
        with self._cid_lock:
            self._cid_by_root[session_root_id] = cid
            self._roots_by_cid.setdefault(cid, set()).add(session_root_id)
            cached = self._handle_by_cid.get(cid)

        # Reuse path: the same container is already running (a later task of
        # the same app session). allocate runs serially on the jobs worker
        # (the app seeds serially), so there is no concurrent name collision;
        # should one happen anyway (an externally started container of the
        # same name, etc.), the docker run name clash surfaces it clearly.
        if self._is_running(name):
            handle = cached
            if handle is None:
                port = self._mapped_port(name)
                if port is not None:
                    handle = SandboxHandle(
                        base_url=f"http://127.0.0.1:{port}",
                        sandbox_id=name,
                        auth=StaticApiKeyAuth(self._api_key_env),
                        workdir=self._workdir,
                    )
            if handle is not None and self._probe(handle.base_url, headers):
                with self._cid_lock:
                    self._handle_by_cid[cid] = handle
                _log.info("reusing sandbox %s at %s", name, handle.base_url)
                return handle
            # Running but port recovery failed / probe unreachable → tear down
            # and rebuild (stale / half-dead container)

        port = self._pick_unreserved_port()
        base_url = f"http://127.0.0.1:{port}"
        # Best-effort remove a stale same-named container (leftover from a
        # crashed prior process / idle-stopped but with a changed spec) so the
        # run does not collide on the name.
        self._rm(name)
        argv = self._run_argv(name=name, port=port, spec=spec)
        result = self._run(
            argv, capture_output=True, text=True, check=False, env=self._run_env()
        )
        if result.returncode != 0:
            raise DockerSandboxError(
                f"docker run failed for {name!r}: {result.stderr.strip()}"
            )
        self._await_ready(base_url, headers, name=name)
        _log.info("provisioned sandbox %s at %s", name, base_url)
        handle = SandboxHandle(
            base_url=base_url,
            sandbox_id=name,
            auth=StaticApiKeyAuth(self._api_key_env),
            workdir=self._workdir,
        )
        with self._cid_lock:
            self._handle_by_cid[cid] = handle
        return handle

    def release(self, session_root_id: str) -> None:
        """Reference-count decrement; only the container's last root actually ``docker rm``s.

        The noeta driver calls this at any root task's terminal state (SDK
        sandbox.py) — a shared container must not be rm'ed directly, or
        stopping one task tears down the whole session's container. After a
        process restart the counts are lost: then rm directly by the resolved
        container id (containers are not reused across restarts anyway;
        teardown already dismantled them at the last shutdown, so a leftover
        is stale).
        """
        with self._cid_lock:
            cid = self._cid_by_root.pop(session_root_id, None)
            if cid is None:
                cid = self._container_id_for(session_root_id)
            roots = self._roots_by_cid.get(cid)
            if roots is not None:
                roots.discard(session_root_id)
                if roots:
                    return  # the container is still referenced by other root tasks
            self._roots_by_cid.pop(cid, None)
            self._handle_by_cid.pop(cid, None)
        self._rm(self._container_name(cid))

    def force_release(self, container_id: str) -> None:
        """Tear down the container directly by container id (app session id), bypassing the refcount (session deletion).

        Idle reclamation's level two also uses it (the long TTL collecting
        disk) — after the teardown :meth:`attach` cannot bring it back, which
        is why level one uses :meth:`stop_idle` (see the module docstring,
        "stop vs remove").
        """
        with self._cid_lock:
            for root in self._roots_by_cid.pop(container_id, set()):
                self._cid_by_root.pop(root, None)
            self._handle_by_cid.pop(container_id, None)
        self._rm(self._container_name(container_id))

    def stop_idle(self, container_id: str) -> bool:
        """Idle-reclaim level one: stop the container but keep it, so :meth:`attach` can bring it back as-is.

        The refcount is untouched: what stops is the container process, not
        the session↔container binding — the session still exists, and when
        the user continues the conversation, attach ``docker start``s it
        back. Returns whether it actually stopped (not running in the first
        place → False, so the reaper neither logs duplicates nor spins empty
        docker stops).
        """
        name = self._container_name(container_id)
        if self._state(name) != "running":
            return False
        # The cached handle points at the stopped container; allocate /
        # live_handle both probe before using the cache, so keeping it would
        # not cause misuse — but there is no reason to keep it.
        with self._cid_lock:
            self._handle_by_cid.pop(container_id, None)
        if not self._stop(name):
            return False
        _log.info("stopped idle sandbox %s (container kept for restart)", name)
        return True

    def live_handle(self, container_id: str) -> Optional[SandboxHandle]:
        """Look up the live container handle by container id (app session id); None when no live container.

        Used by the preview discovery endpoint's lazy-mount fallback: after a
        process restart, requeued tasks go down the attach path, which fires
        no allocate lifecycle listener, so the preview registry has no mount —
        yet the container may still be running. On a cache miss the port is
        recovered via ``docker port`` (same as allocate's reuse path), with
        no readiness probe — the preview surface exposes unreachability
        itself (502).
        """
        name = self._container_name(container_id)
        with self._cid_lock:
            cached = self._handle_by_cid.get(container_id)
        if not self._is_running(name):
            return None
        if cached is not None:
            return cached
        port = self._mapped_port(name)
        if port is None:
            return None
        return SandboxHandle(
            base_url=f"http://127.0.0.1:{port}",
            sandbox_id=name,
            auth=StaticApiKeyAuth(self._api_key_env),
            workdir=self._workdir,
        )

    def attach(self, exec_env_ref: str) -> SandboxHandle:
        """Reconnect to the container named by the ref; one stopped by idle reclamation is ``docker start``ed back first.

        This is **the mandatory path for continuing a conversation** (one app
        session = one noeta root task; from the second turn on it goes
        resume→attach, never allocate again), so "the container is stopped"
        must be recoverable rather than an error. ``docker start`` restores
        the port mapping exactly, so the base_url inside the ref needs no
        re-resolution.
        """
        base_url, sandbox_id = decode_exec_env_ref(exec_env_ref)
        if not sandbox_id:
            raise DockerSandboxError(
                f"exec_env_ref {exec_env_ref!r} carries no sandbox id to attach"
            )
        state = self._state(sandbox_id)
        if state == "absent":
            raise DockerSandboxError(
                f"sandbox container {sandbox_id!r} is not running on this host "
                "(a local-Docker container is bound to the machine that "
                "provisioned it; cross-machine reconnect needs a distributed / "
                "NAS backend)"
            )
        if state == "stopped":
            self._restart(sandbox_id, base_url)
        return SandboxHandle(
            base_url=base_url,
            sandbox_id=sandbox_id,
            auth=StaticApiKeyAuth(self._api_key_env),
            workdir=self._workdir,
        )

    def _restart(self, name: str, base_url: str) -> None:
        """``docker start`` + await readiness, bringing an idle-stopped container back into service.

        When it comes up but the probe fails, **stop it back**: leaving a
        running-but-unreachable half-dead container means the next attach
        would treat it as alive and use it directly (execs would only get
        weird connection errors); stopping it back preserves the clean
        "retryable" state. For the same reason failure does not ``rm`` —
        attach holds no SandboxSpec, so a removed container cannot be
        rebuilt, turning a possibly transient failure into a permanent loss.
        """
        result = self._run(
            [self._docker, "start", name],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            # The most likely cause is the original host port having been
            # grabbed by another process (allocate's _reserved_ports only
            # protects against this machine's sandbox containers).
            raise DockerSandboxError(
                f"docker start failed for idle-stopped {name!r}: "
                f"{result.stderr.strip()}"
            )
        headers = StaticApiKeyAuth(self._api_key_env).connect_headers()
        try:
            self._await_ready(base_url, headers, name=name, reap_on_timeout=False)
        except DockerSandboxError:
            self._stop(name)
            raise
        _log.info("restarted idle-stopped sandbox %s at %s", name, base_url)

    # -- helpers ------------------------------------------------------------ #

    def _container_name(self, container_id: str) -> str:
        # Docker container names allow [a-zA-Z0-9][a-zA-Z0-9_.-]; container_id
        # is a ``task-<hex>`` string or an app session uuid-hex string,
        # already safe.
        return f"{self._prefix}{container_id}"

    def _mapped_port(self, name: str) -> Optional[int]:
        """Recover a running container's mapped host port (``docker port <name> 8080``).

        Used by the reuse path when there is no cached handle after a process
        restart. Output looks like ``127.0.0.1:32768`` (possibly multiple
        lines; take the first); returns None when unparseable (the caller
        tears down and rebuilds).
        """
        result = self._run(
            [self._docker, "port", name, str(_CONTAINER_PORT)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        line = result.stdout.strip().splitlines()[0]
        try:
            return int(line.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            return None

    def _run_argv(self, *, name: str, port: int, spec: SandboxSpec) -> list[str]:
        argv = [
            self._docker, "run", "-d",
            "--name", name,
            "-p", f"127.0.0.1:{port}:{_CONTAINER_PORT}",
        ]
        # Pass the key by NAME only (``-e SANDBOX_API_KEY``, no ``=value``):
        # docker reads the value from this process's environment (seeded in
        # :meth:`_run_env`) so the credential never lands in the ``docker run``
        # argv / host process table. It still appears in ``docker inspect`` —
        # the container genuinely needs it — but that is a container-scoped
        # surface, not the host command line.
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
        value read from ``self._api_key_env`` (which may be spelled
        differently), so the ``-e SANDBOX_API_KEY`` pass-through injects it
        without the value ever entering the argv. ``None`` when no key is
        configured (the run then inherits the ambient environment unchanged)."""
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            return None
        return {**os.environ, "SANDBOX_API_KEY": api_key}

    @staticmethod
    def _mount_arg(mount: MountSpec) -> str:
        # Local Docker expresses every mount as ``-v source:target[:ro]``. The
        # ``kind`` (local-path / volume) is transparent to ``-v``; nas / pvc
        # are a distributed-backend concern and never reach here.
        arg = f"{mount.source}:{mount.target}"
        if mount.mode == "ro":
            arg += ":ro"
        return arg

    def _await_ready(
        self,
        base_url: str,
        headers: Mapping[str, str],
        *,
        name: str,
        reap_on_timeout: bool = True,
    ) -> None:
        """Poll the readiness probe until it passes; raise on timeout.

        ``reap_on_timeout``: a failed allocate must reap the half-started
        container (it was created by this very call; keeping it is just
        litter); :meth:`_restart` bringing back an existing container must NOT
        rm — that is the session's only container, and removing it makes
        attach unrecoverable; it has its own stop-it-back cleanup.
        """
        deadline = self._monotonic() + self._health_timeout_s
        while self._monotonic() < deadline:
            if self._probe(base_url, headers):
                return
            self._sleep(self._health_interval_s)
        if reap_on_timeout:
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

    def _stop(self, name: str) -> bool:
        """``docker stop`` (container kept). Returns success; failure only logs, never raises."""
        try:
            result = self._run(
                [self._docker, "stop", name],
                capture_output=True, text=True, check=False,
            )
        except Exception as exc:  # docker missing / daemon down — best effort
            _log.warning("docker stop %s failed: %s", name, exc)
            return False
        if result.returncode != 0:
            _log.warning("docker stop %s failed: %s", name, result.stderr.strip())
            return False
        return True

    def _state(self, name: str) -> ContainerState:
        """The container's state on this machine — one ``docker inspect`` distinguishing all three.

        The ``-f {{.State.Running}}`` wire: container missing → non-zero
        exit; present → 0 + ``true`` / ``false``. attach needs the
        stopped-vs-absent distinction to decide between "bring it back" and
        "report unrecoverable cross-machine", so running-or-not alone is not
        enough.
        """
        result = self._run(
            [self._docker, "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return "absent"
        return "running" if result.stdout.strip() == "true" else "stopped"

    def _is_running(self, name: str) -> bool:
        return self._state(name) == "running"

    def _pick_unreserved_port(self) -> int:
        """Pick a host port that is neither bound nor statically reserved by an existing sandbox container.

        See the module docstring, "stop vs remove": an idle-stopped container
        binds no port, so the ``bind(0)`` probe cannot see it, yet ``docker
        start`` restores the original mapping — grab it and the stopped
        session can never come back up. Retries :data:`_PORT_PICK_ATTEMPTS`
        times (a collision is low-probability; exhausting the retries means
        the port space is abnormal — report clearly instead of silently
        squatting).
        """
        reserved = self._reserved_ports()
        port = self._pick_port()
        for _ in range(_PORT_PICK_ATTEMPTS):
            if port not in reserved:
                return port
            port = self._pick_port()
        raise DockerSandboxError(
            f"no free host port after {_PORT_PICK_ATTEMPTS} attempts "
            f"(reserved by existing sandbox containers: {sorted(reserved)})"
        )

    def _reserved_ports(self) -> set[int]:
        """Host ports statically held by this machine's existing sandbox containers (stopped included).

        A failed docker query is treated as "nothing reserved": better an
        unlikely port collision than one docker hiccup blocking allocate
        entirely.
        """
        names = self._sandbox_container_names()
        if not names:
            return set()
        result = self._run(
            [self._docker, "inspect", "-f", _PORT_BINDINGS_FMT, *names],
            capture_output=True, text=True, check=False,
        )
        # A container may be removed between ps and inspect (→ non-zero
        # exit): lines already parsed are used regardless.
        ports: set[int] = set()
        for token in result.stdout.split():
            try:
                ports.add(int(token))
            except ValueError:
                continue
        return ports

    def _sandbox_container_names(self) -> list[str]:
        """All container names on this machine under this prefix (``-a``: stopped ones count too)."""
        result = self._run(
            [self._docker, "ps", "-a",
             "--filter", f"name={self._prefix}", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return []
        return result.stdout.split()

    @staticmethod
    def _default_run(
        argv: list[str], **kwargs: object
    ) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(argv, **kwargs)  # noqa: S603 — operator-configured docker
