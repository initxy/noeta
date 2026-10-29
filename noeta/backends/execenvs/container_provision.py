"""Provisioning helpers for :class:`~noeta.backends.execenvs.container.ContainerExecEnv`
(the host decides when to call them): start, stop and remove a container,
its sandbox network and filtering proxy, and the ``docker`` CLI's own
environment."""

from __future__ import annotations

import contextlib
import os
import posixpath
import tempfile
import uuid
from collections.abc import Mapping, Sequence

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.execenvs.sandbox_proxy import Listeners, default_proxy
from noeta.core.execenv import RunResult
from noeta.toolkit.local_env import ENV_ALLOWLIST
from noeta.toolkit.sandbox import SandboxSettings, sandbox_settings

#: Host variables the ``docker`` CLI itself may need (which daemon, which
#: config). They reach the CLI only, never a command in the container.
DOCKER_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
              "DOCKER_TLS_VERIFY")
#: Variables the ``docker`` client's own environment carries; a command's
#: value for one of these is passed as ``-e KEY=VALUE`` instead.
CLIENT_ENV = frozenset({*ENV_ALLOWLIST, *DOCKER_ENV})

#: Budget for one file operation (a ``docker exec`` of ``cat``, ``stat``…).
FILE_OP_TIMEOUT_S = 120.0


class ContainerError(OSError):
    """``docker`` itself failed (no daemon, no such container)."""


async def _docker(docker: str, *args: str, timeout: float = FILE_OP_TIMEOUT_S,
                  env: Mapping[str, str] | None = None) -> RunResult:
    host = LocalExecEnv(tempfile.gettempdir(), inherit_env=DOCKER_ENV)
    try:
        return await host.run([docker, *args], timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise ContainerError(f"{docker!r} not found: {exc}") from exc


#: Capabilities a container keeps by default: Docker's default set without
#: ``MKNOD``, ``NET_RAW``, ``SETFCAP``, ``SETPCAP``, ``SYS_CHROOT`` and
#: ``AUDIT_WRITE`` — enough for root to own and write the mounted workspace.
DEFAULT_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID",
                "NET_BIND_SERVICE")
#: Default ``--pids-limit``: a fork bomb stops here.
DEFAULT_PIDS_LIMIT = 1024
#: Writable scratch on a read-only root (``--tmpfs``), executable.
_TMPFS = ("/tmp", "/var/tmp", "/run", "/root")
#: Docker labels a sandboxed container carries, so a later process can find
#: its network and serve its proxy again (:func:`ensure_container_proxy`).
_NET_LABEL, _PROXY_LABEL = "noeta.network", "noeta.proxy"
#: The filtering proxies this process serves, by container name.
_PROXIES: dict[str, Listeners] = {}


async def _gateway(docker: str, network: str) -> str:
    result = await _docker(docker, "network", "inspect", "-f",
                           "{{range .IPAM.Config}}{{.Gateway}} {{end}}", network)
    gateway = result.stdout.decode().split()
    if result.exit_code != 0 or not gateway:
        raise ContainerError(f"cannot read the gateway of docker network {network!r}")
    return gateway[0]


async def _open_proxy(name: str, settings: SandboxSettings, host: str,
                      ports: tuple[int, int] = (0, 0)) -> Listeners:
    proxy = default_proxy()
    old = _PROXIES.pop(name, None)
    if old is not None:
        await proxy.close(old)
    listeners = await proxy.open(
        allowed=settings.allowed_domains, denied=settings.denied_domains, tcp_host=host,
        tcp_ports=ports, upstream_http=settings.http_proxy_port,
        upstream_socks=settings.socks_proxy_port)
    _PROXIES[name] = listeners
    return listeners


def _confinement_args(workdir: str, *, mounted: bool, read_only: bool,
                      caps: Sequence[str] | None, pids_limit: int | None,
                      memory: str | None, cpus: str | None) -> list[str]:
    """``docker run`` arguments for the root, capabilities and limits."""
    argv: list[str] = []
    if read_only:
        argv.append("--read-only")
        scratch = [*_TMPFS, *(() if mounted else (workdir,))]
        for path in dict.fromkeys(scratch):
            argv += ["--tmpfs", f"{path}:rw,exec,nosuid"]
    if caps is not None:
        argv += ["--cap-drop", "ALL", *(x for cap in caps for x in ("--cap-add", cap))]
    argv += ["--security-opt", "no-new-privileges"]
    if pids_limit:
        argv += ["--pids-limit", str(pids_limit)]
    if memory:
        argv += ["--memory", memory]
    if cpus:
        argv += ["--cpus", cpus]
    return argv


async def _sandbox_network(name: str, settings: SandboxSettings, docker: str,
                           variables: dict[str, str]) -> list[str]:
    """Make container ``name``'s internal network and serve its filtering
    proxy on the network's gateway; the ``docker run`` arguments joining it,
    with the proxy variables set in ``variables`` (unless already there)."""
    own_network = f"{name}-net"
    made = await _docker(docker, "network", "create", "--internal", own_network)
    if made.exit_code != 0:
        raise ContainerError(f"docker network create failed for {own_network!r}: "
                             f"{made.stderr.decode('utf-8', errors='replace').strip()}")
    try:
        listeners = await _open_proxy(name, settings, await _gateway(docker, own_network))
    except BaseException:
        await _docker(docker, "network", "rm", own_network)
        raise
    (host, http_port), (_, socks_port) = listeners.http, listeners.socks
    http, socks = f"http://{host}:{http_port}", f"socks5h://{host}:{socks_port}"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        variables.setdefault(key, http)
    for key in ("ALL_PROXY", "all_proxy"):
        variables.setdefault(key, socks)
    return ["--network", own_network, "--label", f"{_NET_LABEL}={own_network}",
            "--label", f"{_PROXY_LABEL}={host}:{http_port}:{socks_port}"]


async def start_container(
    image: str,
    *,
    name: str | None = None,
    workdir: str = "/workspace",
    mounts: Sequence[tuple[str, str] | tuple[str, str, bool]] = (),
    env: Mapping[str, str] | None = None,
    args: Sequence[str] = (),
    command: Sequence[str] = ("sleep", "infinity"),
    docker: str = "docker",
    network: str | None = "none",
    read_only: bool = True,
    caps: Sequence[str] | None = DEFAULT_CAPS,
    pids_limit: int | None = DEFAULT_PIDS_LIMIT,
    memory: str | None = None,
    cpus: str | None = None,
    sandbox: SandboxSettings | Mapping[str, object] | None = None,
) -> str:
    """``docker run -d`` a long-lived container to exec into and return its
    name. ``mounts`` are ``(host_path, container_path[, read_only])``;
    ``args`` go to ``docker run`` as given. The container runs ``command``
    (default: sleep forever) under ``--init``, so stopping it is prompt and
    zombies are reaped. A failed start removes what it created.

    Secure by default: no network (``network="none"``; ``None`` is Docker's
    default bridge, or name one), a read-only root with writable scratch
    (``/tmp``, ``/var/tmp``, ``/run``, ``/root``, and ``workdir`` when nothing
    is mounted there), :data:`DEFAULT_CAPS` only (``caps=None`` keeps
    Docker's), no privilege gain through setuid programs, and at most
    ``pids_limit`` processes; ``memory`` / ``cpus`` cap the rest.

    ``sandbox`` — Claude Code's ``sandbox`` settings, mapped onto the
    container: with ``network.allowedDomains`` (or the user's own proxy
    port) the container joins an internal network of its own, which reaches
    only this process's filtering proxy on the network's gateway (the proxy
    variables point there), so only those hosts answer. The filesystem
    settings have no counterpart: the container sees only its mounts."""
    name = name or f"noeta-{uuid.uuid4().hex[:12]}"
    settings = sandbox_settings(sandbox)
    argv = ["run", "-d", "--init", "--name", name, "-w", workdir]
    mounted = False
    for mount in mounts:
        src, dst = os.path.abspath(mount[0]), mount[1]
        mount_ro = len(mount) > 2 and bool(mount[2])
        argv += ["-v", f"{src}:{dst}{':ro' if mount_ro else ''}"]
        mounted = mounted or posixpath.normpath(dst) == posixpath.normpath(workdir)
    argv += _confinement_args(workdir, mounted=mounted, read_only=read_only, caps=caps,
                              pids_limit=pids_limit, memory=memory, cpus=cpus)
    values: dict[str, str] = {}
    variables = dict(env or {})
    if settings is not None and settings.enabled and (
            settings.allowed_domains or settings.http_proxy_port or settings.socks_proxy_port):
        argv += await _sandbox_network(name, settings, docker, variables)
    elif network is not None:
        argv += ["--network", network]
    for key, value in variables.items():
        if key in CLIENT_ENV:
            argv += ["-e", f"{key}={value}"]
        else:  # the value travels in the client's environment, not argv
            argv += ["-e", key]
            values[key] = value
    argv += [*args, image, *command]
    result = await _docker(docker, *argv, env=values)
    if result.exit_code != 0:
        await remove_container(name, docker=docker)
        raise ContainerError(f"docker run failed for {name!r}: "
                             f"{result.stderr.decode('utf-8', errors='replace').strip()}")
    # The workdir may be missing from the image when nothing is mounted there.
    await _docker(docker, "exec", name, "mkdir", "-p", workdir)
    return name


async def ensure_container_proxy(name: str, sandbox: SandboxSettings | Mapping[str, object],
                                 *, docker: str = "docker") -> bool:
    """Serve the filtering proxy of a container :func:`start_container`
    sandboxed, from this process (after a restart): on the address its
    labels record. ``False`` when the container has none."""
    if name in _PROXIES:
        return True
    settings = sandbox_settings(sandbox)
    result = await _docker(docker, "inspect", "-f",
                           f'{{{{index .Config.Labels "{_PROXY_LABEL}"}}}}', name)
    label = result.stdout.decode().strip()
    if result.exit_code != 0 or settings is None or label.count(":") != 2:
        return False
    host, http_port, socks_port = label.split(":")
    await _open_proxy(name, settings, host, (int(http_port), int(socks_port)))
    return True


async def stop_container(name: str, *, docker: str = "docker", timeout: int = 10) -> bool:
    """``docker stop``: processes end, the container and its files stay
    (``docker start`` brings it back). ``False`` when it failed."""
    result = await _docker(docker, "stop", "-t", str(timeout), name, timeout=timeout + 30)
    return result.exit_code == 0


async def remove_container(name: str, *, docker: str = "docker") -> None:
    """``docker rm -f``, best effort, with the network and proxy
    :func:`start_container` made for its sandbox."""
    listeners = _PROXIES.pop(name, None)
    if listeners is not None:
        await default_proxy().close(listeners)
    with contextlib.suppress(ContainerError):
        await _docker(docker, "rm", "-f", name)
        await _docker(docker, "network", "rm", f"{name}-net")


async def container_state(name: str, *, docker: str = "docker") -> str:
    """``"running"``, ``"stopped"`` or ``"absent"``."""
    result = await _docker(docker, "inspect", "-f", "{{.State.Running}}", name)
    if result.exit_code != 0:
        return "absent"
    return "running" if result.stdout.strip() == b"true" else "stopped"
